"""API + settings tests: per-camera YOLO model assignment (/api/camera-models).

Covers the assignment normalizers (strict + tolerant), the detection-block
storage contract (persists through camera saves), and the assign / switch /
unassign endpoints. The shared harness (LocalClient, _load_app, _server,
_login, _setup_admin, …) lives in tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports

import uuid

import pytest
from fastapi import HTTPException

# ─── Normalizer unit tests ─────────────────────────────────────────────


def test_normalize_camera_model_path_rules():
    from app.camera_models import DEFAULT_LABELS_PATH, normalize_camera_model_path

    # Empty means "no assignment".
    assert normalize_camera_model_path(None) is None
    assert normalize_camera_model_path('') is None
    assert normalize_camera_model_path('   ') is None

    # Canonical project-relative form.
    assert normalize_camera_model_path('models/yolo11n.onnx') == 'models/yolo11n.onnx'
    assert normalize_camera_model_path(' models\\yolo11s.onnx ') == 'models/yolo11s.onnx'

    # Tolerant mode drops invalid values (settings-schema hygiene on read) ...
    assert normalize_camera_model_path('../evil.onnx') is None
    assert normalize_camera_model_path('models/coco.names') is None
    assert normalize_camera_model_path('models/yolov11n-face.onnx') is None

    # ... strict mode raises a clean 400 so API callers see the reason.
    for bad in ('../evil.onnx', '/etc/passwd', 'models/coco.names', 'models/yolov11n-face.onnx'):
        with pytest.raises(HTTPException) as excinfo:
            normalize_camera_model_path(bad, strict=True)
        assert excinfo.value.status_code == 400

    # Face models are rejected even as labels, and the default labels path is
    # declared for the assignment contract.
    assert DEFAULT_LABELS_PATH == 'models/coco.names'
    with pytest.raises(HTTPException):
        from app.camera_models import normalize_camera_labels_path
        normalize_camera_labels_path('models/face.names', strict=True)


def test_camera_model_assignment_reads_detection_block():
    from app.camera_models import camera_model_assignment

    assert camera_model_assignment({}) is None
    assert camera_model_assignment({'detection': {}}) is None
    assert camera_model_assignment({'detection': {'model_path': ''}}) is None
    assert camera_model_assignment({
        'detection': {'model_path': 'models/yolo11s.onnx'},
    }) == ('models/yolo11s.onnx', 'models/coco.names')
    assert camera_model_assignment({
        'detection': {'model_path': 'models/custom.onnx', 'labels_path': 'models/custom.names'},
    }) == ('models/custom.onnx', 'models/custom.names')
    # Invalid stored values self-heal to "no assignment".
    assert camera_model_assignment({'detection': {'model_path': '../../etc/passwd'}}) is None


def test_normalize_camera_settings_keeps_model_assignment():
    from app.camera_config import normalize_camera_settings

    camera = normalize_camera_settings({
        'id': 'front-door',
        'name': 'Front Door',
        'stream_url': 'rtsp://127.0.0.1:554/front-door',
        'detection': {'model_path': 'models/yolo11m.onnx', 'labels_path': 'models/coco.names'},
    })
    assert camera['detection']['model_path'] == 'models/yolo11m.onnx'
    assert camera['detection']['labels_path'] == 'models/coco.names'

    healed = normalize_camera_settings({
        'id': 'garage',
        'stream_url': 'rtsp://127.0.0.1:554/garage',
        'detection': {'model_path': 'models/yolov11n-face.onnx'},
    })
    assert 'model_path' not in healed['detection']
    assert 'labels_path' not in healed['detection']


def test_camera_detector_follows_assignment(monkeypatch):
    import app.state as _state
    from app.camera_models import clear_camera_model_cache, camera_detector

    sentinel_global = object()
    monkeypatch.setattr(_state, 'detector', sentinel_global)
    clear_camera_model_cache()
    ai_config = {'model_path': 'models/yolo11n.onnx', 'labels_path': 'models/coco.names'}

    # No assignment -> the shared global detector.
    assert camera_detector({'detection': {}}, ai_config) is sentinel_global
    # Assignment matching the global model -> still the shared detector.
    assert camera_detector({'detection': {'model_path': 'models/yolo11n.onnx'}}, ai_config) is sentinel_global
    # A different model builds (and caches) a per-model detector. The test
    # model file is absent, so the detector reports unavailable instead of
    # raising - exactly like the global create_detector contract.
    settings = {'detection': {'model_path': 'models/zz-test-per-camera.onnx'}}
    per_camera = camera_detector(settings, ai_config)
    assert per_camera is not sentinel_global
    assert getattr(per_camera, 'available', False) is False
    assert camera_detector(settings, ai_config) is per_camera
    clear_camera_model_cache()


# ─── API tests ─────────────────────────────────────────────────────────


def _install_test_models(tmp_path, monkeypatch):
    """Redirect the models directory to a tmp dir and create two fake ONNX
    files the assignment endpoints will accept (existence is all they check).
    Returns (models_dir, first, second)."""
    import app.ai_settings
    import app.camera_models
    import app.api.camera_models_router as camera_models_router

    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    for module in (app.ai_settings, app.camera_models, camera_models_router):
        monkeypatch.setattr(module, 'BASE_DIR', tmp_path)
        monkeypatch.setattr(module, 'MODELS_DIR', models_dir, raising=False)
    first = models_dir / f'zz-test-a-{uuid.uuid4().hex[:8]}.onnx'
    second = models_dir / f'zz-test-b-{uuid.uuid4().hex[:8]}.onnx'
    first.write_bytes(b'not really an onnx model')
    second.write_bytes(b'not really an onnx model')
    # The real deployment ships models/coco.names alongside the models; the
    # assignment endpoint validates the labels file exists.
    (models_dir / 'coco.names').write_text('person\ncat\ndog\n', encoding='utf-8')
    return models_dir, first, second


def _create_test_camera(client, csrf):
    status, _headers, body = client.request(
        '/api/cameras',
        method='PUT',
        json_body={'cameras': [{
            'id': 'front-door',
            'name': 'Front Door',
            'backend': 'onvif',
            'stream_url': 'rtsp://127.0.0.1:554/front-door',
        }]},
        headers={'X-CSRF-Token': csrf},
    )
    assert status == 200, body
    return body


def test_camera_model_assignment_api_round_trip(tmp_path, monkeypatch):
    # _load_app re-imports the app package fresh, so the models-dir patch must
    # be applied to the NEW module objects (after the load, before the server).
    app, _database_path = _load_app(tmp_path, monkeypatch)
    _models_dir, first, second = _install_test_models(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        _create_test_camera(client, csrf)

        # Fresh install: every camera follows the default model.
        status, _headers, payload = client.request('/api/camera-models')
        assert status == 200
        assert [row['id'] for row in payload['cameras']] == ['front-door']
        assert payload['cameras'][0]['source'] == 'default'
        assert payload['cameras'][0]['model_path'] is None
        model_paths = {row['path'] for row in payload['models']}
        assert f'models/{first.name}' in model_paths
        assert f'models/{second.name}' in model_paths

        # Assign.
        status, _headers, body = client.request(
            '/api/camera-models/front-door',
            method='PUT',
            json_body={'model_path': f'models/{first.name}'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200, body
        assert body['camera']['source'] == 'assigned'
        assert body['camera']['model_path'] == f'models/{first.name}'

        # Assignment is visible on the camera record itself.
        status, _headers, listed = client.request('/api/cameras')
        assert status == 200
        assert listed['cameras'][0]['detection']['model_path'] == f'models/{first.name}'

        # Switch.
        status, _headers, body = client.request(
            '/api/camera-models/front-door',
            method='PUT',
            json_body={'model_path': f'models/{second.name}'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200, body
        assert body['camera']['model_path'] == f'models/{second.name}'

        # A regular camera edit must preserve the assignment (the settings
        # validator merges detection rather than replacing it).
        status, _headers, listed = client.request('/api/cameras')
        assert status == 200
        edit = {**listed['cameras'][0], 'name': 'Front Door Renamed'}
        status, _headers, edited = client.request(
            '/api/cameras/front-door',
            method='PUT',
            json_body=edit,
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200, edited
        assert edited['detection']['model_path'] == f'models/{second.name}'

        # Unassign -> back to the default.
        status, _headers, body = client.request(
            '/api/camera-models/front-door',
            method='DELETE',
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200, body
        assert body['camera']['source'] == 'default'
        assert body['camera']['model_path'] is None
        status, _headers, listed = client.request('/api/cameras')
        assert 'model_path' not in listed['cameras'][0]['detection']
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_camera_model_assignment_api_validation(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    _models_dir, first, _second = _install_test_models(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        _create_test_camera(client, csrf)

        # Unknown camera -> 404.
        status, _headers, body = client.request(
            '/api/camera-models/nope',
            method='PUT',
            json_body={'model_path': f'models/{first.name}'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 404, body

        # Path traversal -> 400.
        status, _headers, body = client.request(
            '/api/camera-models/front-door',
            method='PUT',
            json_body={'model_path': '../../etc/passwd'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 400, body

        # Face models belong to the face pass -> 400.
        status, _headers, body = client.request(
            '/api/camera-models/front-door',
            method='PUT',
            json_body={'model_path': 'models/yolov11n-face.onnx'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 400, body
        assert 'face' in str(body.get('detail', '')).lower()

        # A well-shaped path whose file is missing -> 404.
        status, _headers, body = client.request(
            '/api/camera-models/front-door',
            method='PUT',
            json_body={'model_path': 'models/zz-test-missing.onnx'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 404, body

        # Empty model_path -> 400 (DELETE is the unassign verb).
        status, _headers, body = client.request(
            '/api/camera-models/front-door',
            method='PUT',
            json_body={'model_path': ''},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 400, body

        # The cameras bulk-save path validates an explicit assignment strictly
        # too, so a typo can't be smuggled in through /api/cameras.
        status, _headers, body = client.request(
            '/api/cameras',
            method='PUT',
            json_body={'cameras': [{
                'id': 'front-door',
                'name': 'Front Door',
                'backend': 'onvif',
                'stream_url': 'rtsp://127.0.0.1:554/front-door',
                'detection': {'model_path': 'models/yolov11n-face.onnx'},
            }]},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 400, body

        # Unassigning a camera without an assignment is a harmless no-op.
        status, _headers, body = client.request(
            '/api/camera-models/front-door',
            method='DELETE',
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200, body
        assert body['camera']['source'] == 'default'
    finally:
        server.should_exit = True
        thread.join(timeout=5)
