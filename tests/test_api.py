from __future__ import annotations

import importlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class LocalClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    @staticmethod
    def header(headers: dict[str, str], name: str) -> str | None:
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def cookie(self, name: str) -> str | None:
        for cookie in self.cookies:
            if cookie.name == name:
                return cookie.value
        return None

    def request(
        self,
        path: str,
        method: str = "GET",
        form: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ):
        request_data = data
        request_headers = dict(headers or {})
        if form is not None:
            request_data = urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if json_body is not None:
            request_data = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        opener = self.opener if follow_redirects else build_opener(HTTPCookieProcessor(self.cookies), NoRedirect)
        request = Request(f"{self.base_url}{path}", data=request_data, method=method, headers=request_headers)
        try:
            with opener.open(request, timeout=5) as response:  # noqa: S310 - local test server only
                return response.status, dict(response.headers), _body(response)
        except HTTPError as exc:
            return exc.code, dict(exc.headers), _error_body(exc)


class NoRedirect(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):  # noqa: ANN001
        fp.status = code
        fp.code = code
        fp.headers = headers
        return fp

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def _multipart_file(field_name: str, filename: str, content: bytes, content_type: str = 'application/octet-stream') -> tuple[bytes, str]:
    boundary = 'daygle-test-boundary'
    body = b''.join([
        f'--{boundary}\r\n'.encode('utf-8'),
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode('utf-8'),
        f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8'),
        content,
        f'\r\n--{boundary}--\r\n'.encode('utf-8'),
    ])
    return body, f'multipart/form-data; boundary={boundary}'


def _body(response):
    data = response.read()
    if "application/json" in response.headers.get("content-type", ""):
        return json.loads(data.decode("utf-8"))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _error_body(exc: HTTPError):
    text = exc.read().decode("utf-8")
    if "application/json" in exc.headers.get("content-type", ""):
        return json.loads(text)
    return text


def _load_app(tmp_path: Path, monkeypatch, extra_ai: str = ""):
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "data" / "daygle.sqlite3"
    config_path.write_text(
        f"""
server:
  host: 127.0.0.1
  port: 8080
auth:
  enabled: true
  session_timeout_hours: 12
  max_login_attempts: 5
  lockout_minutes: 15
ai:
  backend: mock
  confidence: 0.45
{extra_ai}
storage:
  data_dir: {tmp_path / 'data'}
  database: {database_path}
  snapshots_dir: {tmp_path / 'data' / 'snapshots'}
  events_dir: {tmp_path / 'data' / 'events'}
  recordings_dir: {tmp_path / 'data' / 'recordings'}
recording:
  enabled: true
  mode: motion
  continuous: false
  record_on_motion: true
  record_on_human: true
  record_on_objects:
    - cat
    - dog
    - package
    - parcel
  pre_event_seconds: 5
  post_event_seconds: 10
  max_clip_seconds: 60
  format: mp4
  retention_days: 14
  max_storage_gb: 20
  auto_purge_enabled: true
alerts:
  rules:
    - name: Cat alert
      object: cat
      min_confidence: 0.50
      cooldown_seconds: 0
      enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAYGLE_CONFIG", str(config_path))
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main").app, database_path


def _server(app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, thread, f"http://127.0.0.1:{port}"


def test_detector_backend_selection(tmp_path):
    from app.detector import MockDetector, OnnxYoloDetector, create_detector

    assert isinstance(create_detector({"backend": "mock", "categories": ["cat"]}), MockDetector)

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


def test_anpr_pipeline_extracts_vehicle_plate(tmp_path):
    from app.anpr import AnprPipeline
    from app.storage import Storage

    storage = Storage({'storage': {'data_dir': str(tmp_path), 'plates_dir': str(tmp_path / 'plates')}})
    pipeline = AnprPipeline({'enabled': True, 'backend': 'mock', 'min_confidence': 0.75, 'vehicle_labels': ['car']})
    results = pipeline.process_event(
        event_id=42,
        detections=[{'label': 'car', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}],
        image_path=None,
        storage=storage,
    )
    assert len(results) == 1
    assert results[0]['plate_number'].isalnum()
    assert results[0]['confidence'] >= 0.75
    assert Path(results[0]['image_path']).exists()


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
            "/api/detect/test-image",
            method="POST",
            data=b"not really an image",
            headers={"Content-Type": "image/jpeg", "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "ONNX model not found" in body["detail"] or "numpy is not installed" in body["detail"]
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


def test_test_image_upload_uses_onnx_backend_not_mock(tmp_path, monkeypatch):
    import app.detector as detector_module

    known_png = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
        b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04'
        b'\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'
    )

    class FakeOnnxYoloDetector:
        backend = 'onnx'

        def __init__(self, model_path, labels_path=None, **_kwargs):
            self.model_path = Path(model_path)
            self.labels_path = Path(labels_path or '')
            self.unavailable_reason = None

        @property
        def available(self):
            return True

        def detect_image(self, image_bytes: bytes):
            assert image_bytes == known_png
            return [
                {
                    'label': 'known_onnx_object',
                    'confidence': 0.991,
                    'box': {'x': 1.0, 'y': 2.0, 'width': 3.0, 'height': 4.0},
                }
            ]

    monkeypatch.setattr(detector_module, 'OnnxYoloDetector', FakeOnnxYoloDetector)
    app, _database_path = _load_app(
        tmp_path,
        monkeypatch,
        extra_ai=f"""  backend: onnx
  model_path: {tmp_path / 'fake.onnx'}
  labels_path: {tmp_path / 'labels.txt'}
""",
    )
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        Path(tmp_path / 'fake.onnx').write_bytes(b'fake')
        status, _headers, ai_status = client.request('/api/status/ai')
        assert status == 200
        assert ai_status['active_backend'] == 'onnx'
        assert ai_status['model_loaded'] is True
        assert ai_status['mode'] == 'ONNX ACTIVE'

        status, _headers, created = client.request(
            '/api/detect/test-image',
            method='POST',
            data=known_png,
            headers={'Content-Type': 'image/png', 'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert created['ai_backend'] == 'onnx'
        assert created['detections'] == [
            {
                'label': 'known_onnx_object',
                'confidence': 0.991,
                'box': {'x': 1.0, 'y': 2.0, 'width': 3.0, 'height': 4.0},
            }
        ]
        event = client.request(f"/api/events/{created['event_id']}")[2]
        assert event['metadata']['ai_backend'] == 'onnx'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_ai_settings_save_onnx_missing_keeps_previous_detector_and_errors_on_upload(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        missing_model = tmp_path / 'missing-from-ui.onnx'
        status, _headers, settings = client.request(
            '/api/settings/ai',
            method='PUT',
            json_body={'backend': 'onnx', 'model_path': str(missing_model), 'labels_path': 'models/coco.names'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert settings['configured_backend'] == 'onnx'
        assert settings['mode'] == 'MODEL MISSING'
        assert settings['reload_succeeded'] is False
        assert 'ONNX model not found' in settings['reload_error'] or 'numpy is not installed' in settings['reload_error']
        with sqlite3.connect(database_path) as db:
            value = db.execute("SELECT value FROM app_settings WHERE key = 'ai'").fetchone()[0]
        assert json.loads(value)['backend'] == 'onnx'

        status, _headers, body = client.request(
            '/api/detect/test-image',
            method='POST',
            data=b'not really an image',
            headers={'Content-Type': 'image/jpeg', 'X-CSRF-Token': csrf},
        )
        assert status == 400
        assert 'ONNX model not found' in body['detail'] or 'numpy is not installed' in body['detail']
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_live_snapshot_renderer_can_hide_object_overlay(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    frame = {'width': 1280, 'height': 720, 'frame_number': 7, 'timestamp': 1_700_000_000}
    detections = [
        {
            'label': 'person',
            'confidence': 0.92,
            'box': {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.4},
        }
    ]

    without_overlay = main.render_live_snapshot_svg(frame, detections, overlay=False)
    assert 'Overlay OFF' in without_overlay
    assert '<g class="detection-box"' not in without_overlay

    with_overlay = main.render_live_snapshot_svg(frame, detections, overlay=True)
    assert 'Overlay ON' in with_overlay
    assert '<g class="detection-box"' in with_overlay
    assert 'person · 92%' in with_overlay


def test_live_snapshot_jpeg_overlay_changes_frame_when_detections_exist(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

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

    overlaid = main.render_live_snapshot_jpeg_overlay(image_bytes, detections)

    assert overlaid != image_bytes
    decoded = cv2.imdecode(np.frombuffer(overlaid, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert int(decoded.sum()) > 0


def test_export_yolov8n_onnx_uses_ultralytics_export(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    main = sys.modules["app.main"]
    destination = tmp_path / "models" / "yolov8n.onnx"

    def fake_run(command, cwd, capture_output, text, timeout, check):  # noqa: ANN001
        assert command[0] == sys.executable
        assert "from ultralytics import YOLO" in command[2]
        assert "yolov8n.pt" in command[2]
        assert "export(format='onnx')" in command[2]
        assert cwd == destination.parent
        assert capture_output is True
        assert text is True
        assert timeout == 600
        assert check is False
        destination.write_bytes(b"fake onnx")
        return subprocess.CompletedProcess(command, 0, stdout="exported", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    assert main.export_yolov8n_onnx(destination) == len(b"fake onnx")
    assert destination.exists()


def test_favicon_is_served_publicly(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        status, headers, body = client.request("/favicon.ico")
        assert status == 200
        assert "image/svg+xml" in (LocalClient.header(headers, "Content-Type") or "")
        assert "<svg" in body
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_ai_model_status_and_action_endpoints(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, payload = client.request('/api/status/ai')
        assert status == 200
        assert {'current_backend', 'model_exists', 'onnx_runtime_installed', 'detector_loaded', 'active_config_source'} <= set(payload)
        assert payload['active_config_source'] == 'config.yaml'

        status, _headers, checked = client.request('/api/settings/ai/check-model', method='POST', headers={'X-CSRF-Token': csrf})
        assert status == 200
        assert checked['current_backend'] == 'mock'

        status, _headers, tested = client.request('/api/settings/ai/test-detector', method='POST', headers={'X-CSRF-Token': csrf})
        assert status == 200
        assert tested['backend_used'] == 'mock'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _setup_admin(client: LocalClient, username: str = "admin", password: str = "Admin123!") -> None:
    status, _headers, body = client.request("/setup")
    assert status == 200
    assert "Create administrator" in body
    csrf = client.cookie("daygle_csrf")
    status, headers, _body_text = client.request(
        "/setup",
        method="POST",
        form={"username": username, "password": password, "confirm_password": password, "csrf_token": csrf or ""},
        follow_redirects=False,
    )
    assert status == 303
    assert LocalClient.header(headers, "Location") == "/login"


def _login(client: LocalClient, username: str = "admin", password: str = "Admin123!") -> str:
    status, _headers, _body_text = client.request("/login")
    assert status == 200
    csrf = client.cookie("daygle_csrf")
    status, headers, _body_text = client.request(
        "/login",
        method="POST",
        form={"username": username, "password": password, "csrf_token": csrf or ""},
        follow_redirects=False,
    )
    assert status == 303
    assert LocalClient.header(headers, "Location") == "/"
    assert client.cookie("daygle_session")
    status, _headers, me = client.request("/api/auth/me")
    assert status == 200
    return me["csrf_token"]


def test_setup_login_success_session_validation_and_protected_routes(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        status, headers, _body = client.request("/favicon.ico")
        assert status == 200
        assert "image/svg+xml" in (LocalClient.header(headers, "Content-Type") or "")

        status, headers, _body_text = client.request("/", follow_redirects=False)
        assert status == 303
        assert LocalClient.header(headers, "Location") == "/setup"

        _setup_admin(client)

        status, headers, _body_text = client.request("/setup", follow_redirects=False)
        assert status == 303
        assert LocalClient.header(headers, "Location") == "/login"

        anonymous = LocalClient(base_url)
        status, headers, _body_text = anonymous.request("/", follow_redirects=False)
        assert status == 303
        assert LocalClient.header(headers, "Location") == "/login"
        status, _headers, _body_json = anonymous.request("/api/status")
        assert status == 401

        csrf = _login(client)
        status, _headers, root = client.request("/")
        assert status == 200
        assert "AI Camera Dashboard" in root

        status, _headers, payload = client.request("/api/status")
        assert status == 200
        assert payload["status"] == "online"

        status, _headers, _payload = client.request("/api/mock/detect", method="POST")
        assert status == 403
        status, _headers, created = client.request("/api/mock/detect", method="POST", headers={"X-CSRF-Token": csrf})
        assert status == 200
        assert created["created"] is True

        assert client.request("/api/events")[0] == 200
        assert client.request("/api/alerts")[0] == 200
        assert client.request("/api/stats")[2]["total_events"] == 1
        assert client.request("/api/config")[2]["auth"]["enabled"] is True
        assert client.request("/api/config")[2]["anpr"]["enabled"] is True
        assert client.request("/static/app.js")[0] == 200

        with sqlite3.connect(database_path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"users", "user_sessions", "login_attempts", "app_settings", "alert_rules", "vehicle_plates", "plate_events", "plate_alert_rules"}.issubset(tables)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_login_failure_and_account_lockout(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        for _ in range(5):
            client.request("/login")
            csrf = client.cookie("daygle_csrf")
            status, _headers, body = client.request("/login", method="POST", form={"username": "admin", "password": "wrong", "csrf_token": csrf or ""})
            assert status == 200
            assert "Invalid username or password" in body

        client.request("/login")
        csrf = client.cookie("daygle_csrf")
        status, _headers, body = client.request("/login", method="POST", form={"username": "admin", "password": "Admin123!", "csrf_token": csrf or ""})
        assert status == 200
        assert "temporarily locked" in body
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_logout_user_creation_and_password_reset(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, viewer = client.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert viewer["role"] == "viewer"

        status, _headers, updated = client.request(
            f"/api/users/{viewer['id']}",
            method="PATCH",
            json_body={"password": "Viewer456!", "role": "viewer", "is_active": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert updated["role"] == "viewer"

        status, _headers, payload = client.request("/logout", method="POST", headers={"X-CSRF-Token": csrf})
        assert status == 200
        assert payload["ok"] is True
        assert client.request("/api/status")[0] == 401

        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, "viewer", "Viewer456!")
        assert viewer_client.request("/api/status")[0] == 200
        assert viewer_client.request("/api/users")[0] == 403

        status, _headers, created = viewer_client.request(
            "/api/mock/detect", method="POST", headers={"X-CSRF-Token": viewer_csrf}
        )
        assert status == 200
        assert created["created"] is True
        assert created["event_id"] >= 1
        assert created["detections"]

        events = viewer_client.request("/api/events")[2]
        assert events[0]["source"] == "mock-camera"
        assert viewer_client.request(f"/api/events/{created['event_id']}")[2]["id"] == created["event_id"]
        assert viewer_client.request("/api/config")[2]["ai"]["backend"] == "mock"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_admin_ai_settings_viewer_denied_and_db_override(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        csrf = _login(admin)
        status, _headers, settings = admin.request(
            "/api/settings/ai",
            method="PUT",
            json_body={"backend": "mock", "confidence": 0.72, "iou_threshold": 0.33, "input_size": 320},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert settings["confidence"] == 0.72
        config_payload = admin.request("/api/config")[2]
        assert config_payload["ai"]["confidence"] == 0.72
        assert admin.request("/api/status/ai")[2]["active_config_source"] == "database"
        with sqlite3.connect(database_path) as db:
            value = db.execute("SELECT value FROM app_settings WHERE key = 'ai'").fetchone()[0]
        assert json.loads(value)["confidence"] == 0.72

        status, _headers, viewer = admin.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer2", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, viewer["username"], "Viewer123!")
        assert viewer_client.request("/api/settings/alerts")[0] == 200
        status, _headers, body = viewer_client.request(
            "/api/settings/ai",
            method="PUT",
            json_body={"confidence": 0.2},
            headers={"X-CSRF-Token": viewer_csrf},
        )
        assert status == 403
        assert body["detail"] == "Admin access required"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_admin_alert_rule_crud_and_alert_engine_uses_db_rules(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, alert_page = client.request("/alert-settings")
        assert status == 200
        assert 'select name="object" id="objectSelect"' in alert_page
        assert 'Choose Motion or a detector object label.' in alert_page
        assert 'id="testEmailRecipient"' in alert_page
        assert 'id="testEmailBtn"' in alert_page
        assert 'id="newAlertRuleBtn"' in alert_page
        assert 'Add alert rule' in alert_page
        assert 'class="list alert-rules-list"' in alert_page
        assert 'Alert trigger' in alert_page

        initial = client.request("/api/settings/alerts")[2]
        assert "person" in initial["available_labels"]
        for rule in initial["rules"]:
            client.request(f"/api/settings/alerts/{rule['id']}", method="DELETE", headers={"X-CSRF-Token": csrf})

        status, _headers, rule = client.request(
            "/api/settings/alerts",
            method="POST",
            json_body={"name": "Person DB", "object": "person", "min_confidence": 0.1, "cooldown_seconds": 0, "enabled": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert rule["object"] == "person"

        status, _headers, second_rule = client.request(
            "/api/settings/alerts",
            method="POST",
            json_body={"name": "Car DB", "object": "car", "min_confidence": 0.3, "cooldown_seconds": 10, "enabled": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert second_rule["id"] != rule["id"]

        status, _headers, motion_rule = client.request(
            "/api/settings/alerts",
            method="POST",
            json_body={"name": "Motion DB", "object": "motion", "min_confidence": 0.1, "cooldown_seconds": 0, "enabled": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert motion_rule["object"] == "motion"

        rules = client.request("/api/settings/alerts")[2]["rules"]
        assert {rule["name"] for rule in rules} == {"Person DB", "Car DB", "Motion DB"}

        status, _headers, edited = client.request(
            f"/api/settings/alerts/{rule['id']}",
            method="PUT",
            json_body={"min_confidence": 0.2, "enabled": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert edited["min_confidence"] == 0.2

        main_module = sys.modules["app.main"]
        main_module.alerts.rules = main_module.effective_alert_rules()
        triggered = main_module.alerts.process([{"label": "person", "confidence": 0.9}, {"label": "motion", "confidence": 0.8, "motion_event": True}])
        assert any(alert["rule_name"] == "Person DB" for alert in triggered)
        assert any(alert["rule_name"] == "Motion DB" for alert in triggered)

        status, _headers, deleted = client.request(f"/api/settings/alerts/{rule['id']}", method="DELETE", headers={"X-CSRF-Token": csrf})
        assert status == 200
        assert deleted["ok"] is True
        status, _headers, deleted = client.request(f"/api/settings/alerts/{second_rule['id']}", method="DELETE", headers={"X-CSRF-Token": csrf})
        assert status == 200
        assert deleted["ok"] is True
        status, _headers, deleted = client.request(f"/api/settings/alerts/{motion_rule['id']}", method="DELETE", headers={"X-CSRF-Token": csrf})
        assert status == 200
        assert deleted["ok"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_profile_update_and_password_change(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, profile = client.request(
            "/api/profile",
            method="PUT",
            json_body={"timezone": "UTC", "date_format": "iso", "time_format": "24h"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert profile["timezone"] == "UTC"
        assert profile["date_format"] == "iso"

        status, _headers, changed = client.request(
            "/api/profile/password",
            method="POST",
            json_body={"current_password": "Admin123!", "new_password": "Admin456!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert changed["ok"] is True

        client.request("/logout", method="POST", headers={"X-CSRF-Token": csrf})
        new_client = LocalClient(base_url)
        _login(new_client, "admin", "Admin456!")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_email_alert_settings_and_delivery(tmp_path, monkeypatch):
    sent: list[tuple[dict[str, object], int, list[str]]] = []

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_alert(self, alert, *, event_id, recipients):
            sent.append((alert, event_id, list(recipients)))

    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        main_module = sys.modules["app.main"]
        monkeypatch.setattr(main_module, "EmailAlertService", FakeEmailAlertService)

        status, _headers, email_settings = client.request(
            "/api/settings/alert-email",
            method="PUT",
            json_body={
                "enabled": True,
                "host": "smtp.example.com",
                "port": 587,
                "from_address": "alerts@example.com",
                "use_tls": True,
                "use_ssl": False,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert email_settings["enabled"] is True

        for rule in client.request("/api/settings/alerts")[2]["rules"]:
            client.request(f"/api/settings/alerts/{rule['id']}", method="DELETE", headers={"X-CSRF-Token": csrf})

        status, _headers, rule = client.request(
            "/api/settings/alerts",
            method="POST",
            json_body={
                "name": "Cat email",
                "object": "cat",
                "min_confidence": 0.1,
                "cooldown_seconds": 0,
                "enabled": True,
                "email_enabled": True,
                "email_recipients": ["user@example.com"],
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert rule["email_recipients"] == ["user@example.com"]

        main_module.detector.categories = ["cat"]
        main_module.mock_detector.categories = ["cat"]
        status, _headers, created = client.request("/api/mock/detect", method="POST", headers={"X-CSRF-Token": csrf})
        assert status == 200
        assert created["created"] is True
        assert sent
        assert sent[0][1] == created["event_id"]
        assert sent[0][2] == ["user@example.com"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_admin_can_send_test_alert_email(tmp_path, monkeypatch):
    sent: list[tuple[dict[str, object], str]] = []

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_test(self, recipient):
            sent.append((self.settings, recipient))

    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        main_module = sys.modules["app.main"]
        monkeypatch.setattr(main_module, "EmailAlertService", FakeEmailAlertService)

        status, _headers, payload = client.request(
            "/api/settings/alert-email/test",
            method="POST",
            json_body={
                "settings": {
                    "enabled": True,
                    "host": "smtp.example.com",
                    "port": 587,
                    "from_address": "alerts@example.com",
                    "use_tls": True,
                    "use_ssl": False,
                },
                "recipient": "admin@example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )

        assert status == 200
        assert payload == {"ok": True, "recipient": "admin@example.com"}
        assert sent == [(
            {
                "enabled": True,
                "host": "smtp.example.com",
                "port": 587,
                "username": "",
                "password": "",
                "from_address": "alerts@example.com",
                "use_tls": True,
                "use_ssl": False,
            },
            "admin@example.com",
        )]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_anpr_event_search_alerts_and_plate_status_api(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        admin_csrf = _login(admin)
        main_module = sys.modules["app.main"]
        main_module.detector.categories = ["car"]
        main_module.mock_detector.categories = ["car"]

        status, _headers, created = admin.request('/api/mock/detect', method='POST', headers={'X-CSRF-Token': admin_csrf})
        assert status == 200
        assert created['plate_events']
        plate_number = created['plate_events'][0]['plate_number']

        status, _headers, plates = admin.request('/api/plates')
        assert status == 200
        assert plates[0]['plate_number'] == plate_number
        assert plates[0]['sighting_count'] == 1

        status, _headers, search = admin.request(f'/api/plates/search?q={plate_number}')
        assert status == 200
        assert search[0]['event']['id'] == created['event_id']
        assert search[0]['event']['recordings']

        event = admin.request(f"/api/events/{created['event_id']}")[2]
        assert event['plate_events'][0]['plate_number'] == plate_number

        status, _headers, whitelisted = admin.request(
            '/api/plates/whitelist',
            method='POST',
            json_body={'plate_number': plate_number, 'notes': 'Family Car'},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200
        assert whitelisted['is_whitelisted'] is True
        assert whitelisted['notes'] == 'Family Car'

        status, _headers, blacklisted = admin.request(
            '/api/plates/blacklist',
            method='POST',
            json_body={'plate_number': 'BAD001', 'notes': 'Blacklisted'},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200
        assert blacklisted['is_blacklisted'] is True

        status, _headers, rule = admin.request(
            '/api/plate-alerts',
            method='POST',
            json_body={'rule_name': 'Watch plate', 'rule_type': 'plate', 'plate_pattern': plate_number, 'enabled': True, 'cooldown_seconds': 0},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200
        assert rule['plate_pattern'] == plate_number
        assert any(alert['rule_name'] == 'Watch plate' for alert in main_module.trigger_plate_alerts(created['plate_events']))

        status, _headers, edited = admin.request(
            f"/api/plate-alerts/{rule['id']}",
            method='PUT',
            json_body={'enabled': False},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200
        assert edited['enabled'] is False
        assert admin.request(f"/api/plate-alerts/{rule['id']}", method='DELETE', headers={'X-CSRF-Token': admin_csrf})[2]['ok'] is True

        status, _headers, viewer = admin.request(
            '/api/users',
            method='POST',
            json_body={'username': 'plateviewer', 'password': 'Viewer123!', 'role': 'viewer'},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200
        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, viewer['username'], 'Viewer123!')
        assert viewer_client.request('/api/plates')[0] == 200
        denied = viewer_client.request(
            '/api/plates/blacklist',
            method='POST',
            json_body={'plate_number': 'NOPE123'},
            headers={'X-CSRF-Token': viewer_csrf},
        )
        assert denied[0] == 403
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_system_settings_are_editable_from_api(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, camera = client.request(
            "/api/settings/system/camera",
            method="PUT",
            json_body={"backend": "mock", "width": 640, "height": 360, "fps": 12, "device": 0, "flip": "none"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert camera["width"] == 640
        assert client.request("/api/status")[2]["resolution"] == {"width": 640, "height": 360}

        status, _headers, anpr = client.request(
            "/api/settings/anpr",
            method="PUT",
            json_body={"enabled": True, "backend": "mock", "min_confidence": 0.8, "vehicle_labels": ["car", "truck"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert anpr["vehicle_labels"] == ["car", "truck"]

        status, _headers, recording = client.request(
            "/api/settings/system/recording",
            method="PUT",
            json_body={
                "enabled": True,
                "mode": "objects",
                "continuous": False,
                "record_on_motion": False,
                "record_on_human": True,
                "record_on_objects": ["cat", "dog", "package"],
                "pre_event_seconds": 2,
                "post_event_seconds": 3,
                "max_clip_seconds": 10,
                "format": "mp4",
                "retention_days": 7,
                "max_storage_gb": 5,
                "auto_purge_enabled": True,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert recording["mode"] == "objects"
        assert recording["record_on_objects"] == ["cat", "dog", "package"]

        status, _headers, storage = client.request(
            "/api/settings/system/storage",
            method="PUT",
            json_body={"data_dir": str(tmp_path / "runtime-data"), "snapshots_dir": str(tmp_path / "runtime-snaps"), "events_dir": str(tmp_path / "runtime-events"), "recordings_dir": str(tmp_path / "runtime-recordings")},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert storage["database"]
        assert Path(storage["snapshots_dir"]).exists()

        status, _headers, auth_settings = client.request(
            "/api/settings/system/auth",
            method="PUT",
            json_body={"session_timeout_hours": 6, "max_login_attempts": 4, "lockout_minutes": 10},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert auth_settings["max_login_attempts"] == 4

        system_settings = client.request("/api/settings/system")[2]
        assert system_settings["camera"]["width"] == 640
        assert system_settings["anpr"]["min_confidence"] == 0.8
        assert system_settings["recording"]["format"] == "mp4"
        assert system_settings["auth"]["lockout_minutes"] == 10
    finally:
        server.should_exit = True
        thread.join(timeout=5)



def test_onvif_camera_settings_build_rtsp_url(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    settings = main.validate_camera_settings({
        'backend': 'onvif',
        'host': '192.168.1.50',
        'port': 554,
        'path': '/stream1',
        'username': 'daygle user',
        'password': 'pa:ss',
        'width': 1920,
        'height': 1080,
        'fps': 15,
        'flip': 'none',
    })

    assert settings['backend'] == 'onvif'
    assert main.build_stream_url(settings) == 'rtsp://daygle%20user:pa%3Ass@192.168.1.50:554/stream1'
    camera = main.create_camera(settings)
    assert camera.stream_url == 'rtsp://daygle%20user:pa%3Ass@192.168.1.50:554/stream1'


def test_onvif_stream_url_uses_form_credentials_when_url_is_bare(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    settings = main.validate_camera_settings({
        'backend': 'onvif',
        'stream_url': 'rtsp://192.168.40.103:554/live/0/MAIN',
        'username': 'admin',
        'password': 'pa:ss',
        'width': 1280,
        'height': 720,
        'fps': 15,
        'flip': 'none',
    })

    assert main.build_stream_url(settings) == 'rtsp://admin:pa%3Ass@192.168.40.103:554/live/0/MAIN'


def test_opencv_stream_camera_reuses_rtsp_capture(monkeypatch):
    from app.mock_camera import OpenCvStreamCamera

    class FakeImage:
        shape = (720, 1280, 3)

    class FakeEncoded:
        def tobytes(self):
            return b'jpeg'

    class FakeCapture:
        instances = []

        def __init__(self, stream_url):
            self.stream_url = stream_url
            self.buffer_size = None
            self.grab_count = 0
            self.release_count = 0
            FakeCapture.instances.append(self)

        def set(self, prop, value):
            self.buffer_size = (prop, value)

        def isOpened(self):
            return True

        def grab(self):
            self.grab_count += 1
            return True

        def read(self):
            return True, FakeImage()

        def release(self):
            self.release_count += 1

    class FakeCv2:
        CAP_PROP_BUFFERSIZE = 38

        @staticmethod
        def VideoCapture(stream_url):
            return FakeCapture(stream_url)

        @staticmethod
        def imencode(_extension, _image):
            return True, FakeEncoded()

    monkeypatch.setitem(sys.modules, 'cv2', FakeCv2)
    monkeypatch.delenv('OPENCV_FFMPEG_CAPTURE_OPTIONS', raising=False)

    camera = OpenCvStreamCamera('rtsp://admin:password@192.168.40.103:554/live/0/MAIN')
    first_jpeg, first_frame = camera.read_jpeg()
    second_jpeg, second_frame = camera.read_jpeg()

    assert first_jpeg == b'jpeg'
    assert second_jpeg == b'jpeg'
    assert first_frame['frame_number'] == 1
    assert second_frame['frame_number'] == 2
    assert len(FakeCapture.instances) == 1
    assert FakeCapture.instances[0].buffer_size == (FakeCv2.CAP_PROP_BUFFERSIZE, 1)
    assert FakeCapture.instances[0].grab_count == 4
    assert FakeCapture.instances[0].release_count == 0
    assert 'rtsp_transport;tcp' in os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']
    assert 'fflags;nobuffer' in os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']


def test_live_stream_detection_triggers_email_alert(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    sent: list[tuple[dict[str, object], int, list[str]]] = []

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes):
            assert image_bytes == b'jpeg-frame'
            return [
                {
                    'label': 'person',
                    'confidence': 0.91,
                    'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360},
                }
            ]

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_alert(self, alert, *, event_id, recipients):
            sent.append((alert, event_id, list(recipients)))

    monkeypatch.setattr(main, 'detector', FakeDetector())
    monkeypatch.setattr(main, 'EmailAlertService', FakeEmailAlertService)
    main.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    main.database.set_setting(
        'alert_email',
        {'enabled': True, 'host': 'smtp.example.com', 'port': 587, 'from_address': 'alerts@example.com', 'use_tls': True, 'use_ssl': False},
        main.utc_now(),
    )
    for rule in main.database.list_alert_rules():
        main.database.delete_alert_rule(rule['id'])
    main.database.create_alert_rule(
        main.validate_alert_rule({
            'name': 'Person live',
            'object': 'person',
            'min_confidence': 0.5,
            'cooldown_seconds': 0,
            'enabled': True,
            'email_enabled': True,
            'email_recipients': ['owner@example.com'],
        }),
        main.utc_now(),
    )

    event_id = main.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        {'id': 'camera-1', 'name': 'Front Door', 'detection': {'object_detection_enabled': True, 'zones': []}},
    )

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert event['source'] == 'rtsp'
    assert event['detections'][0]['label'] == 'person'
    assert event['detections'][0]['x'] == 0.05
    assert event['recording_status'] == 'linked'
    assert event['recordings'][0]['source'] == 'rtsp'
    assert event['recordings'][0]['camera_id'] == 'camera-1'
    assert Path(event['recordings'][0]['file_path']).exists()
    assert sent
    assert sent[0][0]['rule_name'] == 'Person live'
    assert sent[0][2] == ['owner@example.com']
    status = main.live_detection_status_payload('camera-1')
    assert status['state'] == 'alerted'
    assert status['detected_labels'] == ['person']
    assert status['email_attempted'] is True
    assert status['recording_state'] == 'linked'
    assert status['recording_id'] == event['recordings'][0]['id']


def test_onvif_camera_settings_require_stream_source(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    try:
        main.validate_camera_settings({'backend': 'onvif', 'width': 640, 'height': 480, 'fps': 10, 'flip': 'none'})
    except Exception as exc:  # FastAPI raises HTTPException here.
        assert getattr(exc, 'status_code', None) == 400
        assert 'stream_url is required' in str(getattr(exc, 'detail', ''))
    else:
        raise AssertionError('Expected ONVIF camera validation to require a stream URL or host')

def test_admin_can_backup_and_restore_database_from_api(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, camera = client.request(
            '/api/settings/system/camera',
            method='PUT',
            json_body={'backend': 'mock', 'width': 640, 'height': 360, 'fps': 12, 'device': 'mock', 'flip': 'none'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert camera['width'] == 640

        status, headers, backup_bytes = client.request('/api/settings/system/database/backup')
        assert status == 200
        assert isinstance(backup_bytes, bytes)
        assert 'daygle-database-' in (LocalClient.header(headers, 'content-disposition') or '')
        with sqlite3.connect(database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0] == 1

        status, _headers, camera = client.request(
            '/api/settings/system/camera',
            method='PUT',
            json_body={'backend': 'mock', 'width': 800, 'height': 450, 'fps': 20, 'device': 'mock', 'flip': 'none'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert camera['width'] == 800

        multipart_body, content_type = _multipart_file('file', 'backup.sqlite3', backup_bytes, 'application/vnd.sqlite3')
        status, _headers, restored = client.request(
            '/api/settings/system/database/restore',
            method='POST',
            data=multipart_body,
            headers={'Content-Type': content_type, 'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert restored['ok'] is True
        assert Path(restored['safety_backup']).exists()

        system_settings = client.request('/api/settings/system')[2]
        assert system_settings['camera']['width'] == 640
        assert client.request('/api/status')[2]['resolution'] == {'width': 640, 'height': 360}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_recording_table_creation(tmp_path):
    from app.database import EventDatabase

    database_path = tmp_path / 'recordings.sqlite3'
    EventDatabase(str(database_path))
    with sqlite3.connect(database_path) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(recordings)').fetchall()}
    assert {
        'id',
        'event_id',
        'camera_id',
        'started_at',
        'ended_at',
        'duration_seconds',
        'file_path',
        'thumbnail_path',
        'source',
        'trigger_type',
        'trigger_label',
        'created_at',
    } <= columns


def test_alerted_only_event_and_recording_queries_use_enabled_rules(tmp_path):
    from app.database import EventDatabase

    database = EventDatabase(str(tmp_path / 'events.sqlite3'))
    now = '2026-06-06T00:00:00+00:00'
    enabled_rule = database.create_alert_rule(
        {'name': 'Person alert', 'object': 'person', 'min_confidence': 0.5, 'cooldown_seconds': 0, 'enabled': True},
        now,
    )
    disabled_rule = database.create_alert_rule(
        {'name': 'Dog alert', 'object': 'dog', 'min_confidence': 0.5, 'cooldown_seconds': 0, 'enabled': False},
        now,
    )
    events = [
        database.add_event(
            created_at=f'2026-06-06T00:0{index}:00+00:00',
            source='camera',
            snapshot_path=None,
            detections=[{'label': label, 'confidence': 0.9, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
            alert_triggered=bool(rule),
        )
        for index, (label, rule) in enumerate([('cat', None), ('dog', disabled_rule), ('person', enabled_rule)], start=1)
    ]
    for event_id, label, rule in zip(events, ['cat', 'dog', 'person'], [None, disabled_rule, enabled_rule]):
        database.add_recording(
            event_id=event_id,
            camera_id='front',
            started_at=f'2026-06-06T00:1{event_id}:00+00:00',
            ended_at=f'2026-06-06T00:1{event_id}:05+00:00',
            duration_seconds=5,
            file_path=str(tmp_path / f'{label}.mp4'),
            thumbnail_path=None,
            source='camera',
            created_at=now,
            trigger_type='object',
            trigger_label=label,
        )
        if rule:
            database.add_alert(now, rule['name'], event_id, label, 0.9, f'{label} matched')

    assert [event['id'] for event in database.search_events()] == list(reversed(events))
    assert [event['id'] for event in database.search_events(alerted_only=True)] == [events[2]]
    assert database.search_events(label='dog', alerted_only=True) == []
    assert [event['id'] for event in database.search_events(label='person', alerted_only=True)] == [events[2]]
    assert [recording['event_id'] for recording in database.list_recordings(alerted_only=True)] == [events[2]]
    assert [recording['event_id'] for recording in database.list_recordings(label='person', alerted_only=True)] == [events[2]]


def test_event_linked_recording_metadata_listing_stream_and_delete_permissions(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        admin_csrf = _login(admin)
        status, _headers, viewer = admin.request(
            '/api/users',
            method='POST',
            json_body={'username': 'clipviewer', 'password': 'Viewer123!', 'role': 'viewer'},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200

        status, _headers, created = admin.request('/api/mock/detect', method='POST', headers={'X-CSRF-Token': admin_csrf})
        assert status == 200
        assert created['recording_id'] is not None

        status, _headers, recordings = admin.request('/api/recordings')
        assert status == 200
        assert recordings[0]['id'] == created['recording_id']
        assert recordings[0]['event_id'] == created['event_id']
        assert recordings[0]['detections']
        assert recordings[0]['source'] == 'mock'
        assert recordings[0]['trigger_type'] in {'motion', 'human', 'object', 'continuous'}
        assert Path(recordings[0]['file_path']).exists()

        label = recordings[0]['detections'][0]['label']
        status, _headers, filtered = admin.request(f'/api/recordings?label={label}')
        assert status == 200
        assert any(recording['id'] == created['recording_id'] for recording in filtered)

        status, _headers, detail = admin.request(f"/api/recordings/{created['recording_id']}")
        assert status == 200
        assert detail['event']['id'] == created['event_id']
        event = admin.request(f"/api/events/{created['event_id']}")[2]
        assert event['recording_status'] == 'linked'
        assert event['recordings'][0]['id'] == created['recording_id']

        status, headers, _media = admin.request(f"/api/recordings/{created['recording_id']}/stream")
        assert status == 200
        assert headers['content-type'].startswith('video/mp4')

        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, viewer['username'], 'Viewer123!')
        assert viewer_client.request('/api/recordings')[0] == 200
        status, _headers, denied = viewer_client.request(
            f"/api/recordings/{created['recording_id']}", method='DELETE', headers={'X-CSRF-Token': viewer_csrf}
        )
        assert status == 403
        assert denied['detail'] == 'Admin access required'

        status, _headers, deleted = admin.request(
            f"/api/recordings/{created['recording_id']}", method='DELETE', headers={'X-CSRF-Token': admin_csrf}
        )
        assert status == 200
        assert deleted['ok'] is True
        assert admin.request(f"/api/recordings/{created['recording_id']}")[0] == 404
        assert not Path(recordings[0]['file_path']).exists()
        with sqlite3.connect(database_path) as db:
            count = db.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]
        assert count == 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_recording_retention_purge_deletes_metadata_and_files(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        admin_csrf = _login(admin)
        status, _headers, created = admin.request('/api/mock/detect', method='POST', headers={'X-CSRF-Token': admin_csrf})
        assert status == 200
        recording = admin.request(f"/api/recordings/{created['recording_id']}")[2]
        file_path = Path(recording['file_path'])
        assert file_path.exists()

        old_started = '2000-01-01T00:00:00+00:00'
        with sqlite3.connect(database_path) as db:
            db.execute("UPDATE recordings SET started_at = ?, ended_at = ? WHERE id = ?", (old_started, old_started, created['recording_id']))
            db.commit()

        status, _headers, purged = admin.request('/api/recordings/purge', method='POST', headers={'X-CSRF-Token': admin_csrf})
        assert status == 200
        assert purged['purged'] == 1
        assert purged['files_deleted'] == 1
        assert not file_path.exists()
        assert admin.request(f"/api/recordings/{created['recording_id']}")[0] == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_multiple_cameras_have_per_camera_detection_settings_and_zones(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        cameras = [
            {
                'id': 'front-door',
                'name': 'Front Door',
                'backend': 'mock',
                'width': 1280,
                'height': 720,
                'fps': 15,
                'detection': {
                    'motion_enabled': True,
                    'object_detection_enabled': True,
                    'zones': [
                        {'id': 'porch', 'name': 'Porch', 'x': 0.0, 'y': 0.0, 'width': 0.5, 'height': 0.5, 'monitor_motion': True, 'monitor_objects': True}
                    ],
                },
            },
            {
                'id': 'garage',
                'name': 'Garage',
                'backend': 'mock',
                'width': 640,
                'height': 480,
                'fps': 10,
                'detection': {'motion_enabled': False, 'object_detection_enabled': False, 'zones': []},
            },
        ]
        status, _headers, payload = client.request('/api/cameras', method='PUT', json_body={'cameras': cameras}, headers={'X-CSRF-Token': csrf})
        assert status == 200
        assert [camera['id'] for camera in payload['cameras']] == ['front-door', 'garage']
        assert payload['cameras'][0]['detection']['zones'][0]['name'] == 'Porch'

        status, _headers, listed = client.request('/api/cameras')
        assert status == 200
        assert len(listed['cameras']) == 2
        assert listed['cameras'][1]['detection']['object_detection_enabled'] is False

        status, _headers, status_payload = client.request('/api/status?camera_id=garage')
        assert status == 200
        assert status_payload['camera_id'] == 'garage'
        assert status_payload['resolution'] == {'width': 640, 'height': 480}

        status, _headers, no_detection = client.request('/api/mock/detect?camera_id=garage', method='POST', headers={'X-CSRF-Token': csrf})
        assert status == 200
        assert no_detection['created'] is False

        status, _headers, updated = client.request(
            '/api/cameras/front-door',
            method='PUT',
            json_body={
                **listed['cameras'][0],
                'detection': {
                    **listed['cameras'][0]['detection'],
                    'zones': [
                        {'id': 'driveway', 'name': 'Driveway', 'x': 0.25, 'y': 0.25, 'width': 0.5, 'height': 0.5, 'monitor_motion': True, 'monitor_objects': False}
                    ],
                },
            },
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert updated['detection']['zones'][0]['id'] == 'driveway'
        assert updated['detection']['zones'][0]['monitor_objects'] is False
    finally:
        server.should_exit = True
        thread.join(timeout=5)
