"""API integration tests: ONNX detector, AI settings, model library, and live-snapshot overlay endpoints.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_detector_backend_selection(tmp_path):
    from app.detector import OnnxYoloDetector, create_detector

    assert isinstance(create_detector({"backend": "onnx", "categories": ["cat"]}), OnnxYoloDetector)

    missing_model = tmp_path / "missing.onnx"
    detector = create_detector(
        {
            "backend": "onnx",
            "model_path": str(missing_model),
            "labels_path": "models/coco.names",
            "input_size": 640,
            "confidence": 0.25,
            "iou_threshold": 0.45,
        }
    )
    assert isinstance(detector, OnnxYoloDetector)
    assert detector.available is False
    assert "ONNX model not found" in (detector.unavailable_reason or "") or "numpy is not installed" in (
        detector.unavailable_reason or ""
    )


def test_onnx_missing_model_returns_clear_api_error(tmp_path, monkeypatch):
    app, _database_path = _load_app(
        tmp_path,
        monkeypatch,
        extra_ai=f"""  backend: onnx
  model_path: {tmp_path / 'missing.onnx'}
  labels_path: models/coco.names
""",
    )
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, body = client.request(
            "/api/detect/frame",
            method="POST",
            data=b"not really an image",
            headers={"Content-Type": "image/jpeg", "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert body.get('ai_error'), f"Expected 'ai_error' in response body, got: {body}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_status_ai_reports_model_missing_for_missing_onnx(tmp_path, monkeypatch):
    app, _database_path = _load_app(
        tmp_path,
        monkeypatch,
        extra_ai=f"""  backend: onnx
  model_path: {tmp_path / 'missing.onnx'}
  labels_path: {tmp_path / 'labels.txt'}
""",
    )
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _headers, payload = client.request('/api/status/ai')
        assert status == 200
        assert payload['active_backend'] == 'onnx'
        assert payload['model_loaded'] is False
        assert payload['inference_available'] is False
        assert payload['mode'] == 'MODEL MISSING'
        assert payload['model_exists'] is False
        assert payload['detector_loaded'] is False
        assert payload['active_config_source'] == 'config.yaml'
        assert str(tmp_path / 'missing.onnx') == payload['model_path']
        assert 'ONNX model not found' in payload['error'] or 'numpy is not installed' in payload['error']
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_ai_settings_save_missing_model_path_is_rejected_and_preserves_previous(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # An absolute path outside models/ is rejected by the containment guard
        # instead of being silently persisted and disabling detection.
        outside_model = tmp_path / 'missing-from-ui.onnx'
        status, _headers, body = client.request(
            '/api/settings/ai',
            method='PUT',
            json_body={'backend': 'onnx', 'model_path': str(outside_model), 'labels_path': 'models/coco.names'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 400
        assert 'models/' in body.get('detail', '')

        # A new, in-bounds but non-existent model file is rejected with a
        # helpful "not found" message (typo protection on the settings form).
        status, _headers, body = client.request(
            '/api/settings/ai',
            method='PUT',
            json_body={'backend': 'onnx', 'model_path': 'models/missing-from-ui.onnx', 'labels_path': 'models/coco.names'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 400
        assert 'not found' in body.get('detail', '').lower()

        # Neither rejected save may have persisted the bad path.
        with sqlite3.connect(database_path) as db:
            row = db.execute("SELECT value FROM app_settings WHERE key = 'ai'").fetchone()
        if row is not None:
            saved_model_path = json.loads(row[0]).get('model_path')
            assert saved_model_path != str(outside_model)
            assert saved_model_path != 'models/missing-from-ui.onnx'

        # The detector never became valid, so inference still reports an ai_error.
        status, _headers, body = client.request(
            '/api/detect/frame',
            method='POST',
            data=b'not really an image',
            headers={'Content-Type': 'image/jpeg', 'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert body.get('ai_error'), f"Expected 'ai_error' in response body, got: {body}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_live_snapshot_renderer_can_hide_object_overlay(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    frame = {'width': 1280, 'height': 720, 'frame_number': 7, 'timestamp': 1_700_000_000}
    detections = [
        {
            'label': 'person',
            'confidence': 0.92,
            'box': {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.4},
        }
    ]

    without_overlay = mods.live_snapshot.render_live_snapshot_svg(frame, detections, overlay=False)
    assert 'Overlay OFF' in without_overlay
    assert '<g class="detection-box"' not in without_overlay

    with_overlay = mods.live_snapshot.render_live_snapshot_svg(frame, detections, overlay=True)
    assert 'Overlay ON' in with_overlay
    assert '<g class="detection-box"' in with_overlay
    assert 'Person · 92%' in with_overlay


def test_live_snapshot_jpeg_overlay_changes_frame_when_detections_exist(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    cv2 = pytest.importorskip('cv2')
    np = pytest.importorskip('numpy')
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode('.jpg', frame)
    assert ok
    image_bytes = encoded.tobytes()
    detections = [
        {
            'label': 'person',
            'confidence': 0.92,
            'box': {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.4},
        }
    ]

    overlaid = mods.live_snapshot.render_live_snapshot_jpeg_overlay(image_bytes, detections)

    assert overlaid != image_bytes
    decoded = cv2.imdecode(np.frombuffer(overlaid, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert int(decoded.sum()) > 0


def test_export_yolo_onnx_uses_ultralytics_export(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    main = sys.modules["app.main"]
    mods = _m()
    destination = tmp_path / "models" / "yolov8n.onnx"

    def fake_run(command, cwd, capture_output, text, timeout, check):  # noqa: ANN001
        assert command[0] == sys.executable
        assert "from ultralytics import YOLO" in command[2]
        # Matches both the standard and the end2end/NMS-free export scripts,
        # which now also pass ``imgsz=int(sys.argv[2])``.
        assert "export(format='onnx'" in command[2]
        # Hardening: the weights name and image size are passed as argv
        # (command[3]/command[4]), NOT interpolated into the ``-c`` source, so
        # a quote/newline in either can no longer break out of the string
        # literal into arbitrary code.
        assert "yolov8n.pt" not in command[2]
        assert command[3] == "yolov8n.pt"
        assert command[4] == "640"
        assert cwd == destination.parent
        assert capture_output is True
        assert text is True
        assert timeout == 600
        assert check is False
        destination.write_bytes(b"fake onnx")
        return subprocess.CompletedProcess(command, 0, stdout="exported", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    assert mods.model_management.export_yolo_onnx("yolov8n", destination) == len(b"fake onnx")
    assert destination.exists()


def test_same_model_resolutions_coexist_and_switch_independently(tmp_path, monkeypatch):
    """A second export of one YOLO family must not replace the first size."""
    _load_app(tmp_path, monkeypatch)
    import app.model_management as mm
    import app.api.settings_ai_router as ai_router

    models_dir = tmp_path / 'models'
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)
    monkeypatch.setattr(ai_router, 'BASE_DIR', tmp_path)

    active_settings = {
        'backend': 'onnx',
        'model_path': 'models/yolo11n-768.onnx',
        'labels_path': 'models/coco.names',
        'input_size': 768,
    }
    monkeypatch.setattr(mm, 'effective_ai_config', lambda: dict(active_settings))
    monkeypatch.setattr(ai_router, 'effective_ai_config', lambda: dict(active_settings))
    monkeypatch.setattr(mm, 'validate_ai_settings', lambda payload: dict(payload))
    monkeypatch.setattr(mm, 'detector_status', lambda settings: dict(settings))
    monkeypatch.setattr(mm, '_installed_package_version', lambda _package: 'test-version')
    reload_calls = []

    def fake_reload(settings):
        active_settings.update(settings)
        reload_calls.append(dict(settings))
        return True, None

    monkeypatch.setattr(mm._state, 'reload_detector', fake_reload)
    monkeypatch.setattr(mm._state.database, 'set_setting', lambda *_args: None)

    def fake_export(model_name, destination, imgsz, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f'{model_name}-{imgsz}'.encode())
        return destination.stat().st_size

    monkeypatch.setattr(mm, 'export_yolo_onnx', fake_export)

    first = mm._do_download_model('yolo11n', True, 768)
    second = mm._do_download_model('yolo11n', True, 1024)

    first_path = models_dir / 'yolo11n-768.onnx'
    second_path = models_dir / 'yolo11n-1024.onnx'
    assert first_path.read_bytes() == b'yolo11n-768'
    assert second_path.read_bytes() == b'yolo11n-1024'
    assert first['model_path'] == 'models/yolo11n-768.onnx'
    assert second['model_path'] == 'models/yolo11n-1024.onnx'
    assert reload_calls[-1]['model_path'] == 'models/yolo11n-1024.onnx'

    listed = ai_router.list_ai_models()
    variants = [row for row in listed if row['id'] == 'yolo11n' and row['installed']]
    assert {row['exported_imgsz'] for row in variants} >= {768, 1024}
    assert any(row['active'] and row['path'] == 'models/yolo11n-1024.onnx' for row in variants)

    # Deleting one non-active variant leaves the other resolution usable.
    monkeypatch.setattr(mm, 'effective_ai_config', lambda: {
        **active_settings,
        'model_path': 'models/yolo11n-1024.onnx',
        'input_size': 1024,
    })
    mm.delete_model('yolo11n', imgsz=768)
    assert not first_path.exists()
    assert second_path.exists()
    metadata = mm._read_installed_models()['yolo11n']['variants']
    assert '768' not in metadata
    assert '1024' in metadata


def test_ai_model_status_and_action_endpoints(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, payload = client.request('/api/status/ai')
        assert status == 200
        assert {'active_backend', 'model_exists', 'onnx_runtime_installed', 'detector_loaded', 'active_config_source'} <= set(payload)
        assert payload['active_config_source'] == 'config.yaml'

        status, _headers, checked = client.request('/api/settings/ai/check-model', method='POST', headers={'X-CSRF-Token': csrf})
        assert status == 200
        assert checked['active_backend'] == 'onnx'

        status, _headers, tested = client.request('/api/settings/ai/test-detector', method='POST', headers={'X-CSRF-Token': csrf})
        assert status == 200
        assert tested['backend_used'] == 'onnx'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_fetch_models_manifest_uses_remote_ultralytics_version(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.model_management as _mm

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"info": {"version": "8.4.12"}}'

    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        assert timeout == 10
        return FakeResponse()

    # Patch the ``urllib.request`` singleton directly -- matches the
    # shape ``PushNotificationService._deliver`` uses (``urllib.request.urlopen``
    # read from its own module globals after a top-of-file ``import urllib.request``).
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    manifest = _mm._fetch_models_manifest()

    assert requested_urls == [_mm.PYPI_ULTRALYTICS_URL]
    assert manifest["source"] == "pypi:ultralytics"
    assert manifest["models"]
    assert all(model["version"] == "8.4.12" for model in manifest["models"].values())


def test_check_model_updates_endpoints(tmp_path, monkeypatch):
    app, _ = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)

        # #5 fixture: write a fake yolov8n.onnx into the test's
        # models dir so the endpoint's on-disk installed-model filter
        # has a registered entry. The BASE_DIR patch here also
        # isolates the filter from stray .onnx files in real
        # <project>/models/ (dev-install / prior-test contamination
        # defense). Scenario monkeypatches below use string-path
        # form because the endpoint body reads those names from
        # THIS module's globals, NOT via app.main -- same
        # antipattern as the #4 lesson.
        fake_models_dir = tmp_path / 'models'
        fake_models_dir.mkdir(parents=True, exist_ok=True)
        (fake_models_dir / 'yolov8n.onnx').write_bytes(b'fake onnx')
        monkeypatch.setattr('app.api.settings_ai_router.BASE_DIR', tmp_path)

        # All versions match - no updates
        monkeypatch.setattr('app.api.settings_ai_router._fetch_models_manifest', lambda: {
            "updated_at": "2026-06-08",
            "models": {mid: {"version": "1.0.0"} for mid in ["yolov8n", "yolov8s", "yolov8m", "yolov8l", "yolov8x"]},
        })
        monkeypatch.setattr('app.api.settings_ai_router._read_installed_models', lambda: {
            mid: {"version": "1.0.0", "installed_at": "2026-06-08T00:00:00Z", "sha256": "abc"}
            for mid in ["yolov8n", "yolov8s", "yolov8m", "yolov8l", "yolov8x"]
        })
        status, _, payload = client.request("/api/settings/ai/check-model-updates")
        assert status == 200
        assert payload["any_updates"] is False
        assert len(payload["models"]) == 5
        n_row = next(m for m in payload["models"] if m["id"] == "yolov8n")
        assert n_row["update_available"] is False
        assert n_row["installed_version"] == "1.0.0"
        assert n_row["latest_version"] == "1.0.0"

        # Manifest bumped to 2.0.0 - update available
        monkeypatch.setattr('app.api.settings_ai_router._fetch_models_manifest', lambda: {
            "updated_at": "2026-06-09",
            "source": "pypi:ultralytics",
            "models": {mid: {"version": "2.0.0"} for mid in ["yolov8n", "yolov8s", "yolov8m", "yolov8l", "yolov8x"]},
        })
        status, _, payload = client.request("/api/settings/ai/check-model-updates")
        assert status == 200
        assert payload["version_source"] == "pypi:ultralytics"
        assert payload["any_updates"] is True
        assert len(payload["models"]) == 5
        assert all(m["update_available"] is True for m in payload["models"])
        assert all(m["latest_version"] == "2.0.0" for m in payload["models"])
        n_row = next(m for m in payload["models"] if m["id"] == "yolov8n")
        assert n_row["update_available"] is True
        assert n_row["latest_version"] == "2.0.0"

        # Unknown installed version (legacy install) - treated as needing update
        monkeypatch.setattr('app.api.settings_ai_router._read_installed_models', lambda: {
            "yolov8n": {"version": "unknown", "installed_at": "2026-06-08T00:00:00Z", "sha256": "abc"},
        })
        status, _, payload = client.request("/api/settings/ai/check-model-updates")
        assert status == 200
        n_row = next(m for m in payload["models"] if m["id"] == "yolov8n")
        assert n_row["update_available"] is True

        # Manifest fetch failure - returns 200 with a sanitized error field, not
        # a 5xx. Per the R9 H4 fix, the raw exception message stays server-side
        # and only the exception TYPE name is exposed to the admin client, so we
        # raise a realistic network error (ConnectionRefusedError is an OSError,
        # which the endpoint catches) and assert on the type name.
        def _raise():
            raise ConnectionRefusedError("Connection refused")
        monkeypatch.setattr('app.api.settings_ai_router._fetch_models_manifest', _raise)
        status, _, payload = client.request("/api/settings/ai/check-model-updates")
        assert status == 200
        assert "error" in payload
        assert "ConnectionRefusedError" in payload["error"]
        assert payload["any_updates"] is False
        assert payload["models"] == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_get_ai_settings(tmp_path, monkeypatch):
    """GET /api/settings/ai returns the current AI configuration with status fields."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _headers, settings = client.request("/api/settings/ai")
        assert status == 200
        expected_keys = {"backend", "confidence", "active_backend", "configured_backend", "mode",
                         "available", "model_loaded", "detector_loaded", "model_exists",
                         "onnx_runtime_installed", "active_config_source", "error", "labels_path",
                         "model_path"}
        assert expected_keys <= set(settings), f"Missing keys: {expected_keys - set(settings)}"
        assert settings["backend"] == "onnx"
        assert settings["active_backend"] in ("onnx", "unknown")
        assert settings["active_config_source"] == "config.yaml"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_ai_reload_endpoint(tmp_path, monkeypatch):
    """POST /api/settings/ai/reload reloads the detector and returns status."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, payload = client.request(
            "/api/settings/ai/reload",
            method="POST",
            headers={"X-CSRF-Token": csrf},
        )
        assert status in (200, 400), f"Expected 200 or 400, got {status}"
        assert "reload_succeeded" in payload
        assert "reload_error" in payload
        assert "backend" in payload
        assert "active_backend" in payload
        assert "mode" in payload
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_ai_models_endpoint(tmp_path, monkeypatch):
    """GET /api/settings/ai/models lists available YOLO models with installation status."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _headers, models = client.request("/api/settings/ai/models")
        assert status == 200
        assert isinstance(models, list)
        assert len(models) >= 5
        for model in models:
            assert "id" in model
            assert "label" in model
            assert "description" in model
            assert "approx_mb" in model
            assert "installed" in model
            assert "active" in model
        model_ids = [m["id"] for m in models]
        assert "yolov8n" in model_ids
    finally:
        server.should_exit = True
        thread.join(timeout=5)
