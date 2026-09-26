"""API integration tests: ONNX detector, AI settings, model library, and live-snapshot overlay endpoints.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_clean_install_does_not_download_a_model(tmp_path, monkeypatch):
    """First start must NOT install a detection model behind the operator's back.

    The app used to export a default model in a background thread on a clean
    install, which silently chose a model, needed the network, and burned CPU
    competing with the capture workers. Detection now starts OFF and the ONNX
    page tells the operator to pick a model, so nothing may download on
    startup. This pins that contract at the level that matters: whatever the
    models directory contains, booting the app never calls the download flow.
    """
    # ``_load_app`` re-imports the whole app package, so the module patch has
    # to land AFTER it or it would be thrown away with the old module objects.
    app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.model_management as mm

    calls = []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("startup must not download or export a model")

    monkeypatch.setattr(mm, '_do_download_model', _record)
    # The removed helper must not reappear under its old name either.
    assert not hasattr(mm, 'auto_download_default_model')

    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        # Give the lifespan's startup work time to run; a pre-download would
        # fire immediately on the first poll, not after a long delay.
        time.sleep(0.5)
        assert calls == [], 'a clean install triggered a model download on startup'
        # And the operator is told detection is off rather than left guessing:
        # the Status tab reads this to show the first-run call to action.
        status, _headers, settings = client.request("/api/settings/ai")
        assert status == 200
        assert settings['model_exists'] is False
        assert str(settings['mode']).lower() == 'model missing'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


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


def test_build_event_thumbnail_matches_the_on_the_fly_render_at_thumb_size(tmp_path, monkeypatch):
    """Capture-time thumbnails must equal what the snapshot endpoint would
    render on the fly, just at thumbnail size - the gallery serves the stored
    file, so any difference would make capture-time and legacy rows look
    unlike each other.
    """
    _load_app(tmp_path, monkeypatch)
    mods = _m()

    cv2 = pytest.importorskip('cv2')
    np = pytest.importorskip('numpy')
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode('.jpg', frame)
    assert ok
    image_bytes = encoded.tobytes()
    detections = [
        {
            'label': 'person',
            'confidence': 0.92,
            'box': {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.4},
            # Capture-time rows carry alert flags; they must not change which
            # boxes get drawn relative to the endpoint's flat DB rows.
            'alert_triggered': False,
            'alert_matched': False,
        }
    ]

    thumb = mods.live_snapshot.build_event_thumbnail(image_bytes, detections)
    assert thumb is not None
    assert len(thumb) < len(image_bytes), 'the thumbnail must be smaller than the full frame'

    def decode(jpeg_bytes):
        return cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)

    decoded = decode(thumb)
    assert decoded is not None
    assert decoded.shape[1] == mods.live_snapshot.SNAPSHOT_THUMB_MAX_WIDTH

    on_the_fly = mods.live_snapshot.render_live_snapshot_jpeg_overlay(
        image_bytes,
        mods.live_snapshot.filter_object_priority_detections(
            mods.live_snapshot.overlay_detection_rows(detections)
        ),
        max_width=mods.live_snapshot.SNAPSHOT_THUMB_MAX_WIDTH,
    )
    assert np.array_equal(decoded, decode(on_the_fly)), (
        'the capture-time thumbnail must be pixel-identical to the on-the-fly render'
    )


def test_build_event_thumbnail_returns_none_when_the_frame_cannot_be_rendered(tmp_path, monkeypatch):
    """An undecodable frame yields no thumbnail rather than a full-size copy:
    the endpoint keeps rendering those on the fly."""
    _load_app(tmp_path, monkeypatch)
    mods = _m()

    assert mods.live_snapshot.build_event_thumbnail(b'not-a-jpeg', []) is None
    assert mods.live_snapshot.build_event_thumbnail(b'', [{'label': 'person', 'confidence': 0.5}]) is None


def test_object_priority_hides_overlapping_motion_but_keeps_unrelated_motion(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    from app.live_snapshot import filter_object_priority_detections

    car = {
        'label': 'car',
        'confidence': 0.91,
        'box': {'x': 0.30, 'y': 0.30, 'width': 0.30, 'height': 0.25},
    }
    overlapping_motion = {
        'label': 'motion',
        'motion_event': True,
        'confidence': 0.82,
        'box': {'x': 0.30, 'y': 0.30, 'width': 0.30, 'height': 0.25},
    }
    unrelated_motion = {
        'label': 'motion',
        'motion_event': True,
        'confidence': 0.71,
        'box': {'x': 0.80, 'y': 0.10, 'width': 0.10, 'height': 0.10},
    }

    filtered = filter_object_priority_detections([car, overlapping_motion, unrelated_motion])

    assert filtered == [car, unrelated_motion]


def test_object_priority_leaves_motion_only_detections_unchanged(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    from app.live_snapshot import filter_object_priority_detections

    detections = [
        {
            'label': 'motion',
            'motion_event': True,
            'confidence': 0.82,
            'box': {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.2},
        }
    ]

    assert filter_object_priority_detections(detections) == detections


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
            # Every row -- including not-yet-downloaded base entries -- must
            # carry a ``family`` so the frontend can split the Object
            # Detection / Face Detection grids before any model is installed.
            assert model.get("family") in ("object", "face")
        model_ids = [m["id"] for m in models]
        assert "yolov8n" in model_ids
        face_ids = {m["id"] for m in models if m["family"] == "face"}
        assert {"yolo11n-face", "yolo11s-face", "yolo11l-face"} <= face_ids
        object_ids = {m["id"] for m in models if m["family"] == "object"}
        assert "yolo11n" in object_ids
        assert "yolov8n" in object_ids
        # Exactly one object model carries the "Recommended" badge. Nothing is
        # installed for the operator on a clean install any more, so this flag
        # is what points them at a starting model.
        recommended = [m for m in models if m.get("recommended")]
        assert {m["id"] for m in recommended} == {"yolo26n"}
        assert all(m["family"] == "object" for m in recommended)
        # Undownloaded face entries must be flagged as face-family so they
        # render under "Face Detection Models", not the object grid.
        undownloaded_face = next(
            (m for m in models if m["id"] == "yolo11n-face" and not m["installed"]),
            None,
        )
        assert undownloaded_face is not None
        assert undownloaded_face["family"] == "face"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_do_download_model_binds_face_labels_and_keypoints(tmp_path, monkeypatch):
    """Downloading a catalog entry that declares ``labels`` / ``keypoint_count``
    (a face detector) binds them onto the active AI settings, so the model is
    usable without hand-editing. A plain COCO entry resets both to defaults."""
    _load_app(tmp_path, monkeypatch)
    import app.model_management as mm

    models_dir = tmp_path / 'models'
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)

    catalog = dict(mm.YOLO_MODELS)
    catalog['facetest'] = {
        'pt': 'facetest.pt', 'onnx': 'facetest.onnx', 'label': 'Face Test',
        'approx_mb': 6, 'input_size': 640,
        'labels': 'models/face.names', 'keypoint_count': 5,
        'description': 'synthetic face entry',
    }
    monkeypatch.setattr(mm, 'YOLO_MODELS', catalog)

    active_settings = {
        'backend': 'onnx', 'model_path': 'models/yolo11n-640.onnx',
        'labels_path': 'models/coco.names', 'input_size': 640, 'keypoint_count': 0,
    }
    monkeypatch.setattr(mm, 'effective_ai_config', lambda: dict(active_settings))
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

    mm._do_download_model('facetest', True, 640)
    assert reload_calls[-1]['labels_path'] == 'models/face.names'
    assert reload_calls[-1]['keypoint_count'] == 5

    # Switching to a plain COCO model resets the labels file and clears the
    # pose/keypoint marker so a leftover face config can't leak onto it.
    mm._do_download_model('yolo11n', True, 640)
    assert reload_calls[-1]['labels_path'] == 'models/coco.names'
    assert reload_calls[-1]['keypoint_count'] == 0


def test_do_download_model_does_not_force_enable_face_pass(tmp_path, monkeypatch):
    """Downloading or updating a face-family model must not flip the secondary
    face pass on (or off): pointing the pass at a model and enabling it are
    separate decisions. The persisted ``face_enabled`` value must survive the
    download untouched, and the face detector rebuild must receive exactly
    those settings."""
    _load_app(tmp_path, monkeypatch)
    import app.model_management as mm

    models_dir = tmp_path / 'models'
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)

    catalog = dict(mm.YOLO_MODELS)
    catalog['facetest'] = {
        'pt': 'facetest.pt', 'onnx': 'facetest.onnx', 'label': 'Face Test',
        'approx_mb': 6, 'input_size': 640,
        'labels': 'models/face.names', 'keypoint_count': 5,
        'description': 'synthetic face entry',
    }
    monkeypatch.setattr(mm, 'YOLO_MODELS', catalog)
    monkeypatch.setattr(mm, '_installed_package_version', lambda _package: 'test-version')
    monkeypatch.setattr(mm, 'validate_ai_settings', lambda payload: dict(payload))
    monkeypatch.setattr(mm, 'detector_status', lambda settings: dict(settings))

    def fake_export(model_name, destination, imgsz, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f'{model_name}-{imgsz}'.encode())
        return destination.stat().st_size

    monkeypatch.setattr(mm, 'export_yolo_onnx', fake_export)

    persisted = {}
    monkeypatch.setattr(mm._state.database, 'set_setting',
                        lambda _key, value, _ts: persisted.update(dict(value)))
    rebuild_calls = []
    monkeypatch.setattr(mm._state, 'rebuild_face_detector',
                        lambda settings: rebuild_calls.append(dict(settings)))

    def run_download(face_enabled: bool) -> None:
        active_settings = {
            'backend': 'onnx', 'model_path': 'models/yolo11n-640.onnx',
            'labels_path': 'models/coco.names', 'input_size': 640, 'keypoint_count': 0,
            'face_enabled': face_enabled,
            'face_model_path': 'models/yolo11n-face-320.onnx' if face_enabled else '',
        }
        monkeypatch.setattr(mm, 'effective_ai_config', lambda: dict(active_settings))
        persisted.clear()
        rebuild_calls.clear()
        result = mm._do_download_model('facetest', False, 640, True)
        assert result['ok'] is True
        assert 'Enable Face Detection in AI Settings' in result['message'] or face_enabled

    # Download while the pass is DISABLED: it must stay disabled (the old
    # behaviour force-enabled it), but the new model must be wired in.
    run_download(face_enabled=False)
    assert persisted['face_enabled'] is False
    assert persisted['face_model_path'].endswith('facetest-640.onnx')
    assert len(rebuild_calls) == 1 and rebuild_calls[0]['face_enabled'] is False

    # Download while the pass is already ENABLED: the enabled choice survives
    # and the pass re-points at the freshly exported file.
    run_download(face_enabled=True)
    assert persisted['face_enabled'] is True
    assert persisted['face_model_path'].endswith('facetest-640.onnx')
    assert len(rebuild_calls) == 1 and rebuild_calls[0]['face_enabled'] is True


def test_update_ai_model_routes_face_updates_to_face_slot(tmp_path, monkeypatch):
    """Updating a face-family model must pass the face-routing flag through to
    the re-export helper; otherwise the new ONNX is downloaded but the active
    face detector still points at the stale file."""
    app, _ = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        import app.api.settings_ai_router as ai_router

        catalog = dict(ai_router.YOLO_MODELS)
        catalog['facetest'] = {
            'pt': 'facetest.pt', 'onnx': 'facetest.onnx', 'label': 'Face Test',
            'approx_mb': 6, 'input_size': 640,
            'labels': 'models/face.names', 'keypoint_count': 5,
            'description': 'synthetic face entry',
        }
        monkeypatch.setattr(ai_router, 'YOLO_MODELS', catalog)
        monkeypatch.setattr(ai_router, '_read_installed_models', lambda: {'facetest': {'imgsz': 640, 'path': 'models/facetest.onnx'}})
        monkeypatch.setattr(ai_router, 'write_audit_log', lambda *args, **kwargs: None)

        captured = {}

        def fake_download_model(model_name, switch_active, imgsz, configure_face=False):
            captured['args'] = (model_name, switch_active, imgsz, configure_face)
            return {'ok': True, 'status': {'face_enabled': True, 'face_model_path': f'models/{model_name}-{imgsz}.onnx'}}

        monkeypatch.setattr(ai_router, '_do_download_model', fake_download_model)

        status, _headers, body = client.request(
            '/api/settings/ai/update-model',
            method='POST',
            json_body={'model': 'facetest', 'imgsz': 640, 'is_face_model': True},
            headers={'X-CSRF-Token': csrf},
        )

        assert status == 200
        assert captured['args'] == ('facetest', False, 640, True)
        assert body['status']['face_enabled'] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_export_yolo_onnx_fetches_weights_url(tmp_path, monkeypatch):
    """A catalog entry with a ``weights_url`` (weights Ultralytics can't resolve
    by name) pre-fetches the ``.pt`` into ``models/`` before the export runs."""
    _load_app(tmp_path, monkeypatch)
    main = sys.modules['app.main']
    import app.model_management as mm

    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)

    catalog = dict(mm.YOLO_MODELS)
    catalog['facetest'] = {
        'pt': 'facetest.pt', 'onnx': 'facetest.onnx', 'label': 'Face Test',
        'approx_mb': 6, 'input_size': 640,
        'labels': 'models/face.names', 'keypoint_count': 5,
        'weights_url': 'https://example.invalid/facetest.pt',
        'description': 'synthetic face entry',
    }
    monkeypatch.setattr(mm, 'YOLO_MODELS', catalog)

    download_calls = []

    def fake_download(url, destination, **_kwargs):
        download_calls.append((url, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'fake weights')

    monkeypatch.setattr(mm, '_download_weights', fake_download)

    destination = models_dir / 'facetest.onnx'

    def fake_run(command, cwd, capture_output, text, timeout, check):  # noqa: ANN001
        destination.write_bytes(b'fake onnx')
        return subprocess.CompletedProcess(command, 0, stdout='exported', stderr='')

    monkeypatch.setattr(main.subprocess, 'run', fake_run)

    mm.export_yolo_onnx('facetest', destination)
    assert download_calls == [('https://example.invalid/facetest.pt', models_dir / 'facetest.pt')]
    assert (models_dir / 'facetest.pt').read_bytes() == b'fake weights'


def test_download_weights_rejects_non_https(tmp_path):
    import app.model_management as mm
    with pytest.raises(RuntimeError):
        mm._download_weights('http://example.invalid/x.pt', tmp_path / 'x.pt')
    with pytest.raises(RuntimeError):
        mm._download_weights('file:///etc/passwd', tmp_path / 'x.pt')


def test_download_weights_writes_atomically(tmp_path, monkeypatch):
    import app.model_management as mm

    class _FakeResponse:
        def __init__(self, payload):
            self._chunks = [payload]

        def read(self, _n):
            return self._chunks.pop(0) if self._chunks else b''

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(mm.urllib.request, 'urlopen', lambda *_a, **_k: _FakeResponse(b'weights-bytes'))
    dest = tmp_path / 'sub' / 'weights.pt'
    mm._download_weights('https://example.invalid/weights.pt', dest)
    assert dest.read_bytes() == b'weights-bytes'
    # No partial .download temp file is left behind on success.
    assert not any(p.name.startswith('weights.pt.download') for p in dest.parent.iterdir())


def test_download_weights_enforces_size_cap(tmp_path, monkeypatch):
    import app.model_management as mm

    class _FakeResponse:
        def __init__(self):
            self._left = 3

        def read(self, _n):
            if self._left <= 0:
                return b''
            self._left -= 1
            return b'A' * 64

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(mm.urllib.request, 'urlopen', lambda *_a, **_k: _FakeResponse())
    dest = tmp_path / 'weights.pt'
    with pytest.raises(RuntimeError):
        mm._download_weights('https://example.invalid/weights.pt', dest, max_bytes=100)
    # Failed download leaves no cached weight and no temp file behind.
    assert not dest.exists()
    assert not any(p.name.startswith('weights.pt.download') for p in tmp_path.iterdir())


def test_download_model_installs_without_switching_the_default(tmp_path, monkeypatch):
    """Downloading installs a model but must NOT promote it to the default.

    Picking the model every camera runs is an explicit operator action ("Use"
    here, or a per-camera assignment on /camera-models), so a download can
    never silently repoint the running detector. Mirrors ``update_ai_model``,
    which already passes ``switch_active=False``.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.model_management as mm
    import app.api.settings_ai_router as ai_router

    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    # ``settings_ai_router`` imported BASE_DIR/MODELS_DIR by value, so both
    # modules have to be pointed at the tmp models dir for the listing test.
    for module in (mm, ai_router):
        monkeypatch.setattr(module, 'BASE_DIR', tmp_path)
        monkeypatch.setattr(module, 'MODELS_DIR', models_dir, raising=False)
    monkeypatch.setattr(mm, '_installed_package_version', lambda _package: 'test-version')
    monkeypatch.setattr(mm, 'detector_status', lambda settings: dict(settings))

    def fake_export(model_name, destination, imgsz, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'fake onnx')
        return len(b'fake onnx')

    monkeypatch.setattr(mm, 'export_yolo_onnx', fake_export)

    # Seed a default model that is NOT the one downloaded below.
    mm._state.database.set_setting('ai', {
        'model_path': 'models/yolov8n-640.onnx',
        'labels_path': 'models/coco.names',
        'input_size': 640,
    }, '2026-01-01T00:00:00Z')
    persisted = []
    monkeypatch.setattr(
        mm._state.database,
        'set_setting',
        lambda key, value, _timestamp: persisted.append((key, value)),
    )

    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, body = client.request(
            '/api/settings/ai/download-model',
            method='POST',
            json_body={'model': 'yolo11n', 'imgsz': 640},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200, body
        assert body['ok'] is True
        assert 'not the default model' in body['message']
        # The AI settings were never rewritten, so nothing could have moved the
        # default (or triggered a detector reload).
        assert [entry for entry in persisted if entry[0] == 'ai'] == []
        status, _headers, settings = client.request('/api/settings/ai')
        assert status == 200
        assert settings['model_path'] == 'models/yolov8n-640.onnx'
        # ...while the downloaded model is installed and offered for assignment.
        status, _headers, models = client.request('/api/settings/ai/models')
        assert status == 200
        rows = [row for row in models if row['id'] == 'yolo11n' and row['installed']]
        assert rows, 'the downloaded model should be listed as installed'
        assert all(row['active'] is False for row in rows)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_download_message_reports_which_slot_changed(tmp_path, monkeypatch):
    """The response message must state plainly whether the default model was
    touched, so the operator is never left guessing."""
    _load_app(tmp_path, monkeypatch)
    import app.model_management as mm

    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)
    monkeypatch.setattr(mm, '_installed_package_version', lambda _package: 'test-version')
    monkeypatch.setattr(mm, 'detector_status', lambda settings: dict(settings))

    def fake_export(model_name, destination, imgsz, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'fake onnx')
        return destination.stat().st_size

    monkeypatch.setattr(mm, 'export_yolo_onnx', fake_export)
    monkeypatch.setattr(mm, 'validate_ai_settings', lambda payload: dict(payload))
    monkeypatch.setattr(mm._state.database, 'set_setting', lambda *_args: None)
    monkeypatch.setattr(mm._state, 'reload_detector', lambda _settings: (True, None))

    current = {'model_path': 'models/yolov8n-640.onnx', 'labels_path': 'models/coco.names'}
    monkeypatch.setattr(mm, 'effective_ai_config', lambda: dict(current))

    # Install-only: the default is untouched and the message says so.
    installed = mm._do_download_model('yolo11n', False, 640)
    assert 'not the default model' in installed['message']
    assert installed['reload_succeeded'] is False

    # Internal callers (the legacy-face repair) still activate, and the
    # message says the model became the default.
    activated = mm._do_download_model('yolo11s', True, 640)
    assert 'set it as the default model' in activated['message']
    assert activated['reload_succeeded'] is True

    # Re-exporting the model already running as default keeps it default.
    current['model_path'] = activated['model_path']
    refreshed = mm._do_download_model('yolo11s', False, 640)
    assert 'stays the default model' in refreshed['message']
    assert refreshed['reload_succeeded'] is True
