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
from datetime import datetime, timedelta, timezone
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


TEST_IMAGE_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04'
    b'\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'
)



def _post_frame_detection(client: LocalClient, csrf_token: str | None = None):
    headers = {'Content-Type': 'image/png'}
    if csrf_token:
        headers['X-CSRF-Token'] = csrf_token
    return client.request('/api/detect/frame', method='POST', data=TEST_IMAGE_PNG, headers=headers)


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
  backend: onnx
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


def test_anpr_pipeline_extracts_vehicle_plate(tmp_path):
    from app.anpr import AnprPipeline
    from app.storage import Storage

    storage = Storage({'storage': {'data_dir': str(tmp_path), 'plates_dir': str(tmp_path / 'plates')}})
    pipeline = AnprPipeline({'enabled': True, 'backend': 'paddleocr', 'min_confidence': 0.75, 'vehicle_labels': ['car']})
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
            "/api/detect/frame",
            method="POST",
            data=b"not really an image",
            headers={"Content-Type": "image/jpeg", "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert body.get('ai_error') or body.get('detail')
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
            '/api/detect/frame',
            method='POST',
            data=b'not really an image',
            headers={'Content-Type': 'image/jpeg', 'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert body.get('ai_error') or body.get('detail')
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
        assert checked['current_backend'] == 'onnx'

        status, _headers, tested = client.request('/api/settings/ai/test-detector', method='POST', headers={'X-CSRF-Token': csrf})
        assert status == 200
        assert tested['backend_used'] == 'onnx'
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
        assert "Dashboard" in root

        status, _headers, payload = client.request("/api/status")
        assert status == 200
        assert payload["status"] == "online"

        status, _headers, _frame_blocked = _post_frame_detection(client)
        assert status == 403
        status, _headers, frame_payload = _post_frame_detection(client, csrf)
        assert status == 200
        assert isinstance(frame_payload["detections"], list)
        assert frame_payload["count"] == len(frame_payload["detections"])

        assert client.request("/api/events")[0] == 200
        assert client.request("/api/alerts")[0] == 200
        assert client.request("/api/stats")[2]["total_events"] == 0
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

        assert viewer_client.request("/api/config")[2]["ai"]["backend"] == "onnx"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_user_account_name_email_fields(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # Create user with name/email fields
        status, _headers, user = client.request(
            "/api/users",
            method="POST",
            json_body={"username": "named", "password": "Named123!", "role": "viewer", "first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert user["first_name"] == "Jane"
        assert user["last_name"] == "Doe"
        assert user["email"] == "jane@example.com"

        # Create user with null name fields (must not 500)
        status, _headers, user2 = client.request(
            "/api/users",
            method="POST",
            json_body={"username": "nullfields", "password": "Null1234!", "role": "viewer", "first_name": None, "last_name": None, "email": None},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert user2["first_name"] == ""
        assert user2["last_name"] == ""
        assert user2["email"] == ""

        # Update profile name/email and verify /api/auth/me returns them (not blank)
        named_client = LocalClient(base_url)
        named_csrf = _login(named_client, "named", "Named123!")
        status, _headers, updated = named_client.request(
            "/api/profile",
            method="PUT",
            json_body={"username": "named", "first_name": "Janet", "last_name": "Smith", "email": "janet@example.com", "timezone": "UTC", "date_format": "iso", "time_format": "24h"},
            headers={"X-CSRF-Token": named_csrf},
        )
        assert status == 200
        assert updated["first_name"] == "Janet"
        assert updated["email"] == "janet@example.com"

        # /api/auth/me must return updated fields so the profile form pre-fills correctly
        status, _headers, me = named_client.request("/api/auth/me")
        assert status == 200
        assert me["user"]["first_name"] == "Janet"
        assert me["user"]["last_name"] == "Smith"
        assert me["user"]["email"] == "janet@example.com"
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
            json_body={"backend": "onnx", "confidence": 0.72, "iou_threshold": 0.33, "input_size": 320},
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
        status, _headers, alert_page = client.request("/settings")
        assert status == 200
        assert 'id="testEmailRecipient"' in alert_page
        assert 'id="testEmailBtn"' in alert_page
        assert 'Email delivery' in alert_page
        assert 'Object-specific alert rules are configured on the Zones page.' in alert_page
        assert 'id="newAlertRuleBtn"' not in alert_page

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
    sent: list[tuple[dict[str, object], int, list[str], bytes | None]] = []

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_alert(self, alert, *, event_id, recipients, camera_name=None, camera_id=None, snapshot_bytes=None):
            sent.append((alert, event_id, list(recipients), snapshot_bytes))

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

        detections = [{'label': 'cat', 'confidence': 0.9, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]
        main_module.alerts.rules = main_module.effective_alert_rules()
        triggered = main_module.alerts.process(detections)
        event_time = datetime.now(timezone.utc).isoformat()
        event_id = main_module.database.add_event(
            created_at=event_time,
            source='motion',
            snapshot_path=None,
            detections=detections,
            alert_triggered=bool(triggered),
            metadata={},
        )
        for alert in triggered:
            main_module.database.add_alert(
                created_at=datetime.now(timezone.utc).isoformat(),
                rule_name=alert['rule_name'],
                event_id=event_id,
                label=alert['label'],
                confidence=alert['confidence'],
                message=alert['message'],
            )
        main_module.deliver_email_alerts(triggered, event_id)
        assert sent
        assert sent[0][1] == event_id
        assert sent[0][2] == ["user@example.com"]
        assert sent[0][3] is None  # no snapshot_path on this event

        # Second pass: event with a real snapshot file — snapshot_bytes should be populated
        import cv2
        import numpy as np
        snap_file = tmp_path / 'snap.jpg'
        _, encoded = cv2.imencode('.jpg', np.zeros((10, 10, 3), dtype=np.uint8))
        snap_file.write_bytes(encoded.tobytes())
        sent.clear()
        main_module.alerts.rules = main_module.effective_alert_rules()
        triggered2 = main_module.alerts.process(detections)
        event_id2 = main_module.database.add_event(
            created_at=datetime.now(timezone.utc).isoformat(),
            source='motion',
            snapshot_path=str(snap_file),
            detections=detections,
            alert_triggered=bool(triggered2),
            metadata={},
        )
        main_module.deliver_email_alerts(triggered2, event_id2)
        assert sent
        assert sent[0][3] is not None  # annotated snapshot bytes attached
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
        detections = [{'label': 'car', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}]
        event_time = datetime.now(timezone.utc).isoformat()
        event_id = main_module.database.add_event(
            created_at=event_time,
            source='motion',
            snapshot_path=None,
            detections=detections,
            alert_triggered=False,
            metadata={},
        )
        plate_events = main_module.process_anpr_for_event(event_id, detections, None, event_time)
        assert plate_events
        plate_number = plate_events[0]['plate_number']

        status, _headers, plates = admin.request('/api/plates')
        assert status == 200
        assert plates[0]['plate_number'] == plate_number
        assert plates[0]['sighting_count'] == 1

        status, _headers, search = admin.request(f'/api/plates/search?q={plate_number}')
        assert status == 200
        assert search[0]['event']['id'] == event_id

        event = admin.request(f"/api/events/{event_id}")[2]
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
        assert any(alert['rule_name'] == 'Watch plate' for alert in main_module.trigger_plate_alerts(plate_events))

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
            json_body={"backend": "rtsp", "width": 640, "height": 360, "fps": 12, "device": "rtsp", "flip": "none", "stream_url": "rtsp://127.0.0.1:554/stream1"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert camera["width"] == 640
        assert client.request("/api/status")[2]["resolution"] == {"width": 640, "height": 360}

        status, _headers, anpr = client.request(
            "/api/settings/anpr",
            method="PUT",
            json_body={"enabled": True, "backend": "paddleocr", "min_confidence": 0.8, "vehicle_labels": ["car", "truck"]},
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


def test_runtime_data_reset_clears_operational_data_but_keeps_settings(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, updated_recording = client.request(
            '/api/settings/system/recording',
            method='PUT',
            json_body={
                'enabled': True,
                'mode': 'motion',
                'continuous': False,
                'record_on_motion': True,
                'record_on_human': True,
                'record_on_objects': ['cat'],
                'pre_event_seconds': 5,
                'post_event_seconds': 10,
                'max_clip_seconds': 60,
                'format': 'mp4',
                'retention_days': 21,
                'max_storage_gb': 8,
                'auto_purge_enabled': True,
            },
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert updated_recording['retention_days'] == 21

        import app.main as main_module

        file_path = tmp_path / 'data' / 'recordings' / 'reset-test.mp4'
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b'not-a-real-video')

        event_id = main_module.database.add_event(
            created_at='2026-06-07T00:00:00+00:00',
            source='camera',
            snapshot_path=None,
            detections=[{'label': 'dog', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}],
            metadata={'camera_id': 'camera-1', 'camera_name': 'Primary Camera'},
        )
        main_module.database.add_recording(
            event_id=event_id,
            camera_id='camera-1',
            started_at='2026-06-07T00:00:00+00:00',
            ended_at='2026-06-07T00:00:10+00:00',
            duration_seconds=10.0,
            file_path=str(file_path),
            thumbnail_path=None,
            source='camera',
            created_at='2026-06-07T00:00:00+00:00',
            trigger_type='alert',
            trigger_label='dog',
        )
        main_module.database.add_alert(
            created_at='2026-06-07T00:00:01+00:00',
            rule_name='Dog alert',
            event_id=event_id,
            label='dog',
            confidence=0.9,
            message='Alert triggered: dog detected',
        )

        status, _headers, reset_payload = client.request(
            '/api/system/runtime-data',
            method='DELETE',
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert reset_payload['deleted']['events'] >= 1
        assert reset_payload['deleted']['recordings'] >= 1
        assert reset_payload['deleted']['alerts'] >= 1
        assert reset_payload['deleted']['objects'] >= 1

        assert client.request('/api/events')[2] == []
        assert client.request('/api/recordings')[2] == []
        assert client.request('/api/alerts')[2] == []
        assert client.request('/api/stats')[2]['objects'] == []

        status, _headers, settings_payload = client.request('/api/settings/system')
        assert status == 200
        assert settings_payload['recording']['retention_days'] == 21
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
    from app.camera_backend import OpenCvStreamCamera

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
    assert camera._stale_frame_grabs() == 7
    assert FakeCapture.instances[0].grab_count == 14
    assert FakeCapture.instances[0].release_count == 0
    assert 'rtsp_transport;tcp' in os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']
    assert 'fflags;discardcorrupt' in os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']


def test_opencv_stream_camera_applies_ffmpeg_log_level_after_each_videocapture(monkeypatch):
    """_configure_ffmpeg_log_level must be called after every VideoCapture
    construction - including on reconnect - so FFmpeg's own init cannot reset
    the quiet level back to a noisy default."""
    import app.camera_backend as camera_backend
    from app.camera_backend import OpenCvStreamCamera

    log_level_call_counts = []  # records len(FakeCapture.instances) at each call

    class FakeImage:
        shape = (720, 1280, 3)

    class FakeEncoded:
        def tobytes(self):
            return b'jpeg'

    class FakeCapture:
        instances: list = []

        def __init__(self, _stream_url):
            FakeCapture.instances.append(self)
            self._reads = 0

        def set(self, _prop, _value):
            pass

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            self._reads += 1
            # First capture fails its first read to trigger a reconnect.
            if len(FakeCapture.instances) == 1 and self._reads == 1:
                return False, None
            return True, FakeImage()

        def release(self):
            pass

    class FakeCv2:
        CAP_PROP_BUFFERSIZE = 38

        @staticmethod
        def VideoCapture(stream_url):
            return FakeCapture(stream_url)

        @staticmethod
        def imencode(_ext, _img):
            return True, FakeEncoded()

    monkeypatch.setitem(sys.modules, 'cv2', FakeCv2)
    monkeypatch.delenv('OPENCV_FFMPEG_CAPTURE_OPTIONS', raising=False)
    monkeypatch.setattr(
        camera_backend,
        '_configure_ffmpeg_log_level',
        lambda: log_level_call_counts.append(len(FakeCapture.instances)),
    )

    camera = OpenCvStreamCamera('rtsp://example/stream')
    FakeCapture.instances.clear()
    camera.read_jpeg()

    # Initial open + reconnect should each create one VideoCapture.
    assert len(FakeCapture.instances) == 2, "expected reconnect to create a second VideoCapture"
    # _configure_ffmpeg_log_level must have been called once per VideoCapture.
    assert len(log_level_call_counts) == 2, f"expected 2 calls, got {len(log_level_call_counts)}"
    # Each call must have happened *after* the corresponding VideoCapture was built.
    assert log_level_call_counts[0] == 1, "first call must see 1 VideoCapture instance"
    assert log_level_call_counts[1] == 2, "second call must see 2 VideoCapture instances"


def test_live_stream_detection_triggers_email_alert(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    sent: list[tuple[dict[str, object], int, list[str]]] = []

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
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

        def send_alert(self, alert, *, event_id, recipients, camera_name=None, camera_id=None, snapshot_bytes=None):
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
        {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {
                'zones': [
                    {'id': 'porch', 'name': 'Porch', 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'monitor_motion': True, 'monitor_objects': True, 'monitor_anpr': True},
                ],
            },
        },
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


def test_background_live_alert_monitor_triggers_cat_email_without_live_page(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    sent: list[tuple[dict[str, object], int, list[str]]] = []

    class FakeCamera:
        def read_jpeg(self):
            return b'jpeg-frame', {'width': 1280, 'height': 720}

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            assert image_bytes == b'jpeg-frame'
            return [
                {
                    'label': 'cat',
                    'confidence': 0.92,
                    'box': {'x': 100, 'y': 100, 'width': 200, 'height': 220},
                }
            ]

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_alert(self, alert, *, event_id, recipients, camera_name=None, camera_id=None, snapshot_bytes=None):
            sent.append((alert, event_id, list(recipients)))

    camera_settings = {
        'id': 'camera-1',
        'name': 'Front Door',
        'backend': 'rtsp',
        'stream_url': 'rtsp://camera.example/stream1',
        'width': 1280,
        'height': 720,
        'fps': 15,
        'detection': {
            'zones': [
                {
                    'id': 'porch',
                    'name': 'Porch',
                    'x': 0,
                    'y': 0,
                    'width': 1,
                    'height': 1,
                    'monitor_motion': False,
                    'monitor_objects': True,
                    'object_labels': ['cat'],
                    'monitor_anpr': False,
                },
            ],
        },
    }

    monkeypatch.setattr(main, 'detector', FakeDetector())
    monkeypatch.setattr(main, 'EmailAlertService', FakeEmailAlertService)
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    main.database.set_setting('live', {'detection_interval_seconds': 0.2, 'background_detection_enabled': True}, main.utc_now())
    main.database.set_setting(
        'alert_email',
        {'enabled': True, 'host': 'smtp.example.com', 'port': 587, 'from_address': 'alerts@example.com', 'use_tls': True, 'use_ssl': False},
        main.utc_now(),
    )
    main.database.set_setting('cameras', [camera_settings], main.utc_now())
    main.apply_cameras_settings([main.normalize_camera_settings(camera_settings)])
    main.camera_instances['camera-1'] = FakeCamera()
    main.camera = main.camera_instances['camera-1']
    main.live_detection_last_checked.clear()
    for rule in main.database.list_alert_rules():
        main.database.delete_alert_rule(rule['id'])
    main.database.create_alert_rule(
        main.validate_alert_rule({
            'name': 'Cat live',
            'object': 'cat',
            'min_confidence': 0.5,
            'cooldown_seconds': 0,
            'enabled': True,
            'email_enabled': True,
            'email_recipients': ['owner@example.com'],
        }),
        main.utc_now(),
    )

    assert main.run_live_alert_monitor_once() == 1

    status = main.live_detection_status_payload('camera-1')
    assert status['state'] == 'alerted'
    assert status['detected_labels'] == ['cat']
    assert status['email_attempted'] is True
    assert sent
    assert sent[0][0]['rule_name'] == 'Cat live'
    assert sent[0][2] == ['owner@example.com']


def test_motion_min_confidence_filters_low_confidence_motion(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image_bytes, confidence=None):
            return [
                {
                    'label': 'person',
                    'confidence': 0.4,
                    'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360},
                }
            ]

    monkeypatch.setattr(main, 'detector', FakeDetector())
    main.live_detection_last_checked.clear()

    strict_settings = {
        'id': 'camera-1',
        'name': 'Front Door',
        'detection': {
            'object_labels': ['cat'],
            'zones': [
                {
                    'id': 'motion-zone',
                    'name': 'Motion Zone',
                    'x': 0,
                    'y': 0,
                    'width': 1,
                    'height': 1,
                    'object_rules': [{'label': 'motion', 'min_confidence': 0.45}],
                    'monitor_anpr': False,
                },
            ],
        },
    }

    blocked_event_id = main.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        strict_settings,
        enforce_interval=False,
    )
    assert blocked_event_id is None

    relaxed_settings = {
        'id': 'camera-1',
        'name': 'Front Door',
        'detection': {
            'object_labels': ['cat'],
            'zones': [
                {
                    'id': 'motion-zone',
                    'name': 'Motion Zone',
                    'x': 0,
                    'y': 0,
                    'width': 1,
                    'height': 1,
                    'object_rules': [{'label': 'motion', 'min_confidence': 0.35}],
                    'monitor_anpr': False,
                },
            ],
        },
    }

    allowed_event_id = main.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        relaxed_settings,
        enforce_interval=False,
    )
    assert allowed_event_id is not None
    event = main.database.get_event(allowed_event_id)
    assert event is not None
    assert any(detection['label'] == 'motion' for detection in event['detections'])


def test_extend_active_rtsp_recording_updates_trigger_label_to_specific_object(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    now = datetime.now(timezone.utc)
    started_at = (now - timedelta(seconds=5)).isoformat()
    ended_at = now.isoformat()
    file_path = tmp_path / 'data' / 'recordings' / 'extend-trigger.mp4'
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b'placeholder')

    recording_id = main.database.add_recording(
        event_id=None,
        camera_id='camera-1',
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=5.0,
        file_path=str(file_path),
        thumbnail_path=None,
        source='rtsp',
        created_at=started_at,
        trigger_type='motion',
        trigger_label='motion',
    )

    with main.active_rtsp_recordings_lock:
        main.active_rtsp_recordings['camera-1'] = {
            'recording_id': recording_id,
            'start_capture_ts': (now - timedelta(seconds=5)).timestamp(),
            'capture_deadline_ts': now.timestamp(),
            'max_capture_deadline_ts': (now + timedelta(seconds=20)).timestamp(),
        }

    extended_id = main.extend_active_rtsp_recording(
        camera_id='camera-1',
        event_time=now.isoformat(),
        recording_config={'enabled': True, 'mode': 'motion', 'record_on_alert': True, 'extension_step_seconds': 10},
        detections=[{'label': 'dog', 'confidence': 0.88, 'alert_triggered': True}],
    )

    assert extended_id == recording_id
    updated = main.database.get_recording(recording_id)
    assert updated is not None
    assert updated['trigger_label'] == 'dog'
    assert updated['trigger_type'] == 'alert'

    with main.active_rtsp_recordings_lock:
        main.active_rtsp_recordings.pop('camera-1', None)

def test_live_stream_detection_queue_runs_in_background_and_deduplicates(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    started = threading.Event()
    release = threading.Event()

    class SlowDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None
        calls = 0

        def detect_image(self, _image_bytes, confidence=None):
            self.calls += 1
            started.set()
            release.wait(timeout=2)
            return []

    detector = SlowDetector()
    monkeypatch.setattr(main, 'detector', detector)
    main.live_detection_last_checked.clear()
    main.active_live_detection_cameras.clear()
    # queue_live_stream_alerts is the frontend-triggered path and only runs detection
    # when background_detection_enabled=False (otherwise the background monitor handles it).
    main.database.set_setting('live', {'background_detection_enabled': False}, main.utc_now())
    settings = {'id': 'camera-1', 'name': 'Front Door', 'detection': {'zones': []}}

    main.queue_live_stream_alerts(b'jpeg-frame-1', {'width': 1280, 'height': 720}, settings)
    assert started.wait(timeout=2)
    main.queue_live_stream_alerts(b'jpeg-frame-2', {'width': 1280, 'height': 720}, settings)

    assert detector.calls == 1
    assert 'camera-1' in main.active_live_detection_cameras

    release.set()
    deadline = time.time() + 2
    while 'camera-1' in main.active_live_detection_cameras and time.time() < deadline:
        time.sleep(0.01)
    assert 'camera-1' not in main.active_live_detection_cameras


def test_live_stream_detection_without_alert_rule_does_not_record_by_default(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [
                {
                    'label': 'person',
                    'confidence': 0.91,
                    'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360},
                }
            ]

    monkeypatch.setattr(main, 'detector', FakeDetector())
    main.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    for rule in main.database.list_alert_rules():
        main.database.delete_alert_rule(rule['id'])

    event_id = main.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {
                'zones': [
                    {'id': 'porch', 'name': 'Porch', 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'monitor_motion': True, 'monitor_objects': True, 'monitor_anpr': True},
                ],
            },
            'recording': {'enabled': True, 'record_on_alert': True, 'continuous': False},
        },
    )

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert event['recording_status'] == 'none'
    status = main.live_detection_status_payload('camera-1')
    assert status['state'] == 'checked'
    assert status['recording_state'] == 'skipped'
    assert 'waiting for an enabled alert rule' in status['recording_reason']


def test_live_stream_detection_saves_only_allowed_zone_object_labels(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [
                {
                    'label': 'person',
                    'confidence': 0.91,
                    'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360},
                },
                {
                    'label': 'suitcase',
                    'confidence': 0.88,
                    'box': {'x': 500, 'y': 120, 'width': 180, 'height': 220},
                },
            ]

    monkeypatch.setattr(main, 'detector', FakeDetector())
    main.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    for rule in main.database.list_alert_rules():
        main.database.delete_alert_rule(rule['id'])
    main.database.create_alert_rule(
        main.validate_alert_rule({
            'name': 'Person live',
            'object': 'person',
            'min_confidence': 0.5,
            'cooldown_seconds': 0,
            'enabled': True,
        }),
        main.utc_now(),
    )

    event_id = main.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {
                'zones': [
                    {
                        'id': 'porch',
                        'name': 'Porch',
                        'x': 0,
                        'y': 0,
                        'width': 1,
                        'height': 1,
                        'monitor_motion': False,
                        'monitor_objects': True,
                        'object_labels': ['person', 'cat'],
                        'monitor_anpr': False,
                    },
                ],
            },
        },
    )

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert [detection['label'] for detection in event['detections']] == ['person']
    status = main.live_detection_status_payload('camera-1')
    assert [detection['label'] for detection in status['detections']] == ['person']


def test_live_stream_camera_continuous_recording_records_without_alert_rule(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [
                {
                    'label': 'person',
                    'confidence': 0.91,
                    'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360},
                }
            ]

    monkeypatch.setattr(main, 'detector', FakeDetector())
    main.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    for rule in main.database.list_alert_rules():
        main.database.delete_alert_rule(rule['id'])

    event_id = main.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {
                'zones': [
                    {'id': 'porch', 'name': 'Porch', 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'monitor_motion': True, 'monitor_objects': True, 'monitor_anpr': True},
                ],
            },
            'recording': {'enabled': True, 'record_on_alert': True, 'continuous': True},
        },
    )

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert event['recording_status'] == 'linked'
    assert event['recordings'][0]['trigger_type'] == 'continuous'
    status = main.live_detection_status_payload('camera-1')
    assert status['recording_state'] == 'linked'


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
            json_body={'backend': 'rtsp', 'width': 640, 'height': 360, 'fps': 12, 'device': 'rtsp', 'flip': 'none', 'stream_url': 'rtsp://127.0.0.1:554/stream1'},
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
            json_body={'backend': 'rtsp', 'width': 800, 'height': 450, 'fps': 20, 'device': 'rtsp', 'flip': 'none', 'stream_url': 'rtsp://127.0.0.1:554/stream1'},
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


def test_rtsp_recording_metadata_can_skip_generated_placeholder(tmp_path):
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'enabled': True, 'mode': 'motion', 'format': 'mp4'},
    })

    metadata = service.event_recording_metadata(
        42,
        '2026-06-06T00:00:00+00:00',
        'rtsp',
        [{'label': 'car', 'confidence': 0.8}],
        write_clip=False,
    )

    assert metadata is not None
    assert metadata['source'] == 'rtsp'
    assert metadata['file_path'].endswith('.mp4')
    assert not Path(metadata['file_path']).exists()


def test_alert_recording_prefers_specific_object_label_over_motion(tmp_path):
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {
            'enabled': True,
            'mode': 'motion',
            'record_on_alert': True,
            'format': 'mp4',
        },
    })

    metadata = service.event_recording_metadata(
        43,
        '2026-06-06T00:00:00+00:00',
        'rtsp',
        [
            {'label': 'person', 'confidence': 0.91, 'alert_triggered': False},
            {'label': 'motion', 'confidence': 0.99, 'alert_triggered': True},
        ],
        write_clip=False,
    )

    assert metadata is not None
    assert metadata['trigger_type'] == 'alert'
    assert metadata['trigger_label'] == 'person'


def test_rtsp_recording_errors_redact_stream_password():
    from app.recordings import RecordingService

    message = RecordingService.redact_stream_credentials(
        'Error opening input file rtsp://admin:secret-password@192.168.40.101:554/live/0/MAIN.'
    )

    assert 'secret-password' not in message
    assert 'rtsp://admin:***@192.168.40.101:554/live/0/MAIN' in message


def test_rtsp_recording_capture_falls_back_on_stream_error(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    class FakeRecordingService:
        def __init__(self):
            self.rtsp_calls = 0
            self.fallback_calls = 0

        def write_rtsp_clip(self, *_args):
            self.rtsp_calls += 1
            raise RuntimeError('Stream unavailable')

        def write_event_clip(self, file_path, *_args):
            self.fallback_calls += 1
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            Path(file_path).write_text('fallback', encoding='utf-8')

    service = FakeRecordingService()
    monkeypatch.setattr(main, 'recording_service', service)
    stream_url = 'rtsp://admin:secret-password@192.168.40.101:554/live/0/MAIN'
    main.active_rtsp_recordings.clear()

    file_path = tmp_path / 'recordings' / 'event_1.mp4'
    main.start_rtsp_recording_capture(
        stream_url,
        {'file_path': str(file_path), 'duration_seconds': 10, 'trigger_type': 'motion'},
        1,
        [{'label': 'person'}],
        recording_id=1,
    )

    deadline = time.time() + 3
    while not file_path.exists() and time.time() < deadline:
        time.sleep(0.05)

    assert service.rtsp_calls == 1
    assert service.fallback_calls == 1
    assert file_path.read_text(encoding='utf-8') == 'fallback'
    main.active_rtsp_recordings.clear()


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
    import app.main as main

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image_bytes, confidence=None):
            return [{'label': 'cat', 'confidence': 0.91, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]

    monkeypatch.setattr(main, 'detector', FakeDetector())
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

        detections = [{'label': 'cat', 'confidence': 0.91, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]
        event_time = datetime.now(timezone.utc).isoformat()
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'test.png')
        event_id = main.database.add_event(
            created_at=event_time,
            source='motion',
            snapshot_path=snapshot_path,
            detections=detections,
            alert_triggered=False,
            metadata={},
        )
        recording_id = main.attach_event_recording(event_id, event_time, 'upload', detections)
        assert recording_id is not None

        status, _headers, recordings = admin.request('/api/recordings')
        assert status == 200
        assert recordings[0]['id'] == recording_id
        assert recordings[0]['event_id'] == event_id
        assert recordings[0]['detections']
        assert recordings[0]['source'] == 'upload'
        assert recordings[0]['trigger_type'] in {'motion', 'human', 'object', 'continuous'}
        assert Path(recordings[0]['file_path']).exists()

        label = recordings[0]['detections'][0]['label']
        status, _headers, filtered = admin.request(f'/api/recordings?label={label}')
        assert status == 200
        assert any(recording['id'] == recording_id for recording in filtered)

        status, _headers, detail = admin.request(f"/api/recordings/{recording_id}")
        assert status == 200
        assert detail['event']['id'] == event_id
        event = admin.request(f"/api/events/{event_id}")[2]
        assert event['recording_status'] == 'linked'
        assert event['recordings'][0]['id'] == recording_id

        status, headers, _media = admin.request(f"/api/recordings/{recording_id}/stream")
        assert status == 200
        assert headers['content-type'].startswith('video/mp4')

        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, viewer['username'], 'Viewer123!')
        assert viewer_client.request('/api/recordings')[0] == 200
        status, _headers, denied = viewer_client.request(
            f"/api/recordings/{recording_id}", method='DELETE', headers={'X-CSRF-Token': viewer_csrf}
        )
        assert status == 403
        assert denied['detail'] == 'Admin access required'

        status, _headers, deleted = admin.request(
            f"/api/recordings/{recording_id}", method='DELETE', headers={'X-CSRF-Token': admin_csrf}
        )
        assert status == 200
        assert deleted['ok'] is True
        assert admin.request(f"/api/recordings/{recording_id}")[0] == 404
        assert not Path(recordings[0]['file_path']).exists()
        with sqlite3.connect(database_path) as db:
            count = db.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]
        assert count == 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_recording_retention_purge_deletes_metadata_and_files(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image_bytes, confidence=None):
            return [{'label': 'cat', 'confidence': 0.91, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]

    monkeypatch.setattr(main, 'detector', FakeDetector())
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        admin_csrf = _login(admin)
        detections = [{'label': 'cat', 'confidence': 0.91, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]
        event_time = datetime.now(timezone.utc).isoformat()
        event_id = main.database.add_event(
            created_at=event_time,
            source='motion',
            snapshot_path=None,
            detections=detections,
            alert_triggered=False,
            metadata={},
        )
        recording_id = main.attach_event_recording(event_id, event_time, 'upload', detections)
        assert recording_id is not None
        recording = admin.request(f"/api/recordings/{recording_id}")[2]
        file_path = Path(recording['file_path'])
        assert file_path.exists()

        old_started = '2000-01-01T00:00:00+00:00'
        with sqlite3.connect(database_path) as db:
            db.execute("UPDATE recordings SET started_at = ?, ended_at = ? WHERE id = ?", (old_started, old_started, recording_id))
            db.commit()

        status, _headers, purged = admin.request('/api/recordings/purge', method='POST', headers={'X-CSRF-Token': admin_csrf})
        assert status == 200
        assert purged['purged'] == 1
        assert purged['files_deleted'] == 1
        assert not file_path.exists()
        assert admin.request(f"/api/recordings/{recording_id}")[0] == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_recordings_timeline_returns_camera_day_segments(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        target_day = '2026-06-07'
        started_at = f'{target_day}T08:15:00+00:00'
        ended_at = f'{target_day}T08:15:12+00:00'
        _login(admin)

        import app.main as main_module

        file_path = tmp_path / 'data' / 'recordings' / 'timeline-test.mp4'
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b'not-a-real-video')

        event_id = main_module.database.add_event(
            created_at=started_at,
            source='camera',
            snapshot_path=None,
            detections=[{'label': 'person', 'confidence': 0.99, 'box': {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.4}}],
            metadata={'camera_id': 'camera-1', 'camera_name': 'Primary Camera'},
        )
        recording_id = main_module.database.add_recording(
            event_id=event_id,
            camera_id='camera-1',
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=12.0,
            file_path=str(file_path),
            thumbnail_path=None,
            source='camera',
            created_at=started_at,
            trigger_type='human',
            trigger_label='person',
        )

        motion_started_at = f'{target_day}T08:30:00+00:00'
        motion_ended_at = f'{target_day}T08:30:10+00:00'
        motion_event_id = main_module.database.add_event(
            created_at=motion_started_at,
            source='camera',
            snapshot_path=None,
            detections=[{'label': 'person', 'confidence': 0.88, 'box': {'x': 0.15, 'y': 0.25, 'width': 0.2, 'height': 0.25}}],
            metadata={'camera_id': 'camera-1', 'camera_name': 'Primary Camera'},
        )
        motion_recording_id = main_module.database.add_recording(
            event_id=motion_event_id,
            camera_id='camera-1',
            started_at=motion_started_at,
            ended_at=motion_ended_at,
            duration_seconds=10.0,
            file_path=str(file_path.with_name('timeline-motion-test.mp4')),
            thumbnail_path=None,
            source='camera',
            created_at=motion_started_at,
            trigger_type='motion',
            trigger_label='person',
        )
        Path(str(file_path.with_name('timeline-motion-test.mp4'))).write_bytes(b'not-a-real-video')

        status, _headers, payload = admin.request(f'/api/recordings/timeline?camera_id=camera-1&day={target_day}')
        assert status == 200
        assert payload['camera']['id'] == 'camera-1'
        assert payload['day'] == target_day
        assert payload['cameras']
        assert len(payload['recordings']) == 2

        segment = next(recording for recording in payload['recordings'] if recording['id'] == recording_id)
        assert segment['id'] == recording_id
        assert segment['timeline_start_seconds'] == 8 * 3600 + 15 * 60
        assert segment['timeline_end_seconds'] == 8 * 3600 + 15 * 60 + 12
        assert segment['timeline_duration_seconds'] == 12
        assert segment['color_key'] == 'person'
        assert segment['event']['metadata']['camera_id'] == 'camera-1'

        motion_segment = next(recording for recording in payload['recordings'] if recording['id'] == motion_recording_id)
        assert motion_segment['color_key'] == 'motion'
        assert motion_segment['color_label'] == 'motion'

        status, _headers, local_payload = admin.request(
            f'/api/recordings/timeline?camera_id=camera-1&day={target_day}&tz_offset_minutes=-120'
        )
        assert status == 200
        local_segment = next(recording for recording in local_payload['recordings'] if recording['id'] == recording_id)
        assert local_segment['timeline_start_seconds'] == 10 * 3600 + 15 * 60
        assert local_payload['timeline_timezone_offset_minutes'] == -120

        status, _headers, empty_payload = admin.request('/api/recordings/timeline?camera_id=camera-1&day=2026-06-08')
        assert status == 200
        assert empty_payload['recordings'] == []
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
                'backend': 'onvif',
                'stream_url': 'rtsp://127.0.0.1:554/front-door',
                'width': 1280,
                'height': 720,
                'fps': 15,
                'detection': {
                    'motion_enabled': True,
                    'object_detection_enabled': True,
                    'anpr_enabled': True,
                    'object_labels': ['person', 'cat'],
                    'zones': [
                        {'id': 'porch', 'name': 'Porch', 'x': 0.0, 'y': 0.0, 'width': 0.5, 'height': 0.5, 'monitor_motion': True, 'monitor_objects': True, 'object_labels': ['person'], 'monitor_anpr': True}
                    ],
                },
            },
            {
                'id': 'garage',
                'name': 'Garage',
                'backend': 'onvif',
                'stream_url': 'rtsp://127.0.0.1:554/garage',
                'width': 640,
                'height': 480,
                'fps': 10,
                'detection': {'motion_enabled': False, 'object_detection_enabled': False, 'anpr_enabled': False, 'zones': []},
            },
        ]
        status, _headers, payload = client.request('/api/cameras', method='PUT', json_body={'cameras': cameras}, headers={'X-CSRF-Token': csrf})
        assert status == 200
        assert [camera['id'] for camera in payload['cameras']] == ['front-door', 'garage']
        assert payload['cameras'][0]['detection']['object_labels'] == ['person', 'cat']
        assert payload['cameras'][0]['detection']['zones'][0]['name'] == 'Porch'
        assert payload['cameras'][0]['detection']['zones'][0]['object_labels'] == ['person']

        status, _headers, listed = client.request('/api/cameras')
        assert status == 200
        assert len(listed['cameras']) == 2
        assert listed['cameras'][1]['detection']['object_detection_enabled'] is False

        status, _headers, status_payload = client.request('/api/status?camera_id=garage')
        assert status == 200
        assert status_payload['camera_id'] == 'garage'
        assert status_payload['resolution'] == {'width': 640, 'height': 480}

        status, _headers, updated = client.request(
            '/api/cameras/front-door',
            method='PUT',
            json_body={
                **listed['cameras'][0],
                'detection': {
                    **listed['cameras'][0]['detection'],
                    'zones': [
                        {'id': 'driveway', 'name': 'Driveway', 'x': 0.25, 'y': 0.25, 'width': 0.5, 'height': 0.5, 'monitor_motion': True, 'monitor_objects': False, 'object_labels': 'cat, person, cat', 'monitor_anpr': False}
                    ],
                },
            },
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert updated['detection']['zones'][0]['id'] == 'driveway'
        assert updated['detection']['zones'][0]['monitor_objects'] is False
        assert updated['detection']['zones'][0]['object_labels'] == ['cat', 'person']
        assert updated['detection']['zones'][0]['monitor_anpr'] is False
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_polygon_monitoring_zones_are_normalized_and_filter_by_shape(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    main = sys.modules["app.main"]
    triangle = {
        'id': 'triangle',
        'name': 'Triangle',
        'points': [
            {'x': 0.1, 'y': 0.1},
            {'x': 0.8, 'y': 0.1},
            {'x': 0.1, 'y': 0.8},
        ],
        'monitor_motion': True,
        'monitor_objects': True,
        'monitor_anpr': True,
    }

    zones = main.normalize_monitoring_zones([triangle])

    assert zones[0]['x'] == 0.1
    assert zones[0]['y'] == 0.1
    assert zones[0]['width'] == 0.7
    assert zones[0]['height'] == 0.7
    assert zones[0]['points'] == triangle['points']

    settings = {'detection': {'zones': zones}}
    detections = [
        {'label': 'person', 'box': {'x': 0.25, 'y': 0.25, 'width': 0.1, 'height': 0.1}},
        {'label': 'car', 'box': {'x': 0.7, 'y': 0.7, 'width': 0.1, 'height': 0.1}},
    ]

    filtered = main.filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_objects', require_zones=True)

    assert [detection['label'] for detection in filtered] == ['person']


def test_monitoring_zones_filter_object_detections_by_label(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    main = sys.modules["app.main"]
    zones = main.normalize_monitoring_zones([
        {
            'id': 'porch',
            'name': 'Porch',
            'x': 0,
            'y': 0,
            'width': 1,
            'height': 1,
            'monitor_objects': True,
            'object_labels': ['person', 'cat'],
        }
    ])
    settings = {'detection': {'zones': zones}}
    detections = [
        {'label': 'person', 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}},
        {'label': 'suitcase', 'box': {'x': 0.2, 'y': 0.2, 'width': 0.1, 'height': 0.1}},
        {'label': 'cat', 'box': {'x': 0.3, 'y': 0.3, 'width': 0.1, 'height': 0.1}},
    ]

    filtered = main.filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_objects', require_zones=True)

    assert [detection['label'] for detection in filtered] == ['person', 'cat']


def test_monitoring_zones_normalize_object_rules(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    main = sys.modules["app.main"]
    zones = main.normalize_monitoring_zones([
        {
            'id': 'porch',
            'name': 'Porch',
            'x': 0,
            'y': 0,
            'width': 1,
            'height': 1,
            'monitor_motion': False,
            'object_rules': [
                {
                    'label': 'Cat',
                    'record_on_detect': False,
                    'alert_on_detect': True,
                    'min_confidence': 0.7,
                    'cooldown_seconds': 5,
                    'email_enabled': True,
                    'email_recipients': 'alerts@example.test, bad-address',
                    'active_start': '07:00',
                    'active_end': '18:00',
                }
            ],
        }
    ])

    rule = zones[0]['object_rules'][0]
    assert zones[0]['object_labels'] == ['cat']
    assert rule['label'] == 'cat'
    assert rule['record_on_detect'] is False
    assert rule['alert_on_detect'] is True
    assert rule['min_confidence'] == 0.7
    assert rule['cooldown_seconds'] == 5
    assert rule['email_recipients'] == ['alerts@example.test']
    assert rule['active_start'] == '07:00'
    assert rule['active_end'] == '18:00'


def test_zone_object_alert_rules_are_scoped_to_matching_zone(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    main = sys.modules["app.main"]
    zones = main.normalize_monitoring_zones([
        {
            'id': 'porch',
            'name': 'Porch',
            'x': 0,
            'y': 0,
            'width': 0.5,
            'height': 0.5,
            'monitor_motion': False,
            'object_rules': [{'label': 'cat', 'alert_on_detect': True, 'record_on_detect': False}],
        },
        {
            'id': 'driveway',
            'name': 'Driveway',
            'x': 0.5,
            'y': 0.5,
            'width': 0.5,
            'height': 0.5,
            'monitor_motion': False,
            'object_rules': [{'label': 'cat', 'alert_on_detect': False, 'record_on_detect': True}],
        },
    ])
    settings = {'id': 'front', 'name': 'Front Door', 'detection': {'zones': zones}}
    detections = [
        {'label': 'cat', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}},
        {'label': 'dog', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}},
        {'label': 'cat', 'confidence': 0.9, 'box': {'x': 0.8, 'y': 0.8, 'width': 0.1, 'height': 0.1}},
    ]

    rules = main.zone_object_alert_rules(settings)
    alert_detections = main.zone_alert_detections(settings, detections)

    assert [rule['name'] for rule in rules] == ['Front Door / Porch / cat']
    assert len(alert_detections) == 1
    assert alert_detections[0]['zone_id'] == 'porch'
    assert alert_detections[0]['box']['x'] == 0.1
    triggered = main.AlertEngine(rules).process(alert_detections + [{**detections[2], 'zone_id': 'driveway'}])
    assert [alert['rule_name'] for alert in triggered] == ['Front Door / Porch / cat']
    assert main.zone_record_on_detect(detections[0], settings) is False
    assert main.zone_record_on_detect(detections[2], settings) is True


def test_camera_object_labels_filter_without_monitoring_zones(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    main = sys.modules["app.main"]
    settings = {'detection': {'object_labels': ['person', 'cat'], 'zones': []}}
    detections = [
        {'label': 'person', 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}},
        {'label': 'suitcase', 'box': {'x': 0.2, 'y': 0.2, 'width': 0.1, 'height': 0.1}},
    ]

    filtered = main.filter_detections_for_camera(detections, settings)

    assert [detection['label'] for detection in filtered] == ['person']


def test_check_model_updates_endpoints(tmp_path, monkeypatch):
    app, _ = _load_app(tmp_path, monkeypatch)
    main_module = sys.modules["app.main"]
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # All versions match — no updates
        monkeypatch.setattr(main_module, "_fetch_models_manifest", lambda: {
            "updated_at": "2026-06-08",
            "models": {mid: {"version": "1.0.0"} for mid in ["yolov8n", "yolov8s", "yolov8m", "yolov8l", "yolov8x"]},
        })
        monkeypatch.setattr(main_module, "_read_installed_models", lambda: {
            "yolov8n": {"version": "1.0.0", "installed_at": "2026-06-08T00:00:00Z", "sha256": "abc"},
        })
        status, _, payload = client.request("/api/settings/ai/check-model-updates")
        assert status == 200
        assert payload["any_updates"] is False
        n_row = next(m for m in payload["models"] if m["id"] == "yolov8n")
        assert n_row["update_available"] is False
        assert n_row["installed_version"] == "1.0.0"
        assert n_row["latest_version"] == "1.0.0"

        # Manifest bumped to 2.0.0 — update available
        monkeypatch.setattr(main_module, "_fetch_models_manifest", lambda: {
            "updated_at": "2026-06-09",
            "models": {mid: {"version": "2.0.0"} for mid in ["yolov8n", "yolov8s", "yolov8m", "yolov8l", "yolov8x"]},
        })
        status, _, payload = client.request("/api/settings/ai/check-model-updates")
        assert status == 200
        assert payload["any_updates"] is True
        n_row = next(m for m in payload["models"] if m["id"] == "yolov8n")
        assert n_row["update_available"] is True
        assert n_row["latest_version"] == "2.0.0"

        # Unknown installed version (legacy install) — treated as needing update
        monkeypatch.setattr(main_module, "_read_installed_models", lambda: {
            "yolov8n": {"version": "unknown", "installed_at": "2026-06-08T00:00:00Z", "sha256": "abc"},
        })
        status, _, payload = client.request("/api/settings/ai/check-model-updates")
        assert status == 200
        n_row = next(m for m in payload["models"] if m["id"] == "yolov8n")
        assert n_row["update_available"] is True

        # Manifest fetch failure — returns 200 with readable error field, not 502
        def _raise():
            raise RuntimeError("Connection refused")
        monkeypatch.setattr(main_module, "_fetch_models_manifest", _raise)
        status, _, payload = client.request("/api/settings/ai/check-model-updates")
        assert status == 200
        assert "error" in payload
        assert "Connection refused" in payload["error"]
        assert payload["any_updates"] is False
        assert payload["models"] == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)



def test_audit_log_admin_access_and_viewer_denied(tmp_path, monkeypatch):
    app, _ = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    viewer = LocalClient(base_url)
    unauth = LocalClient(base_url)
    try:
        _setup_admin(admin)
        csrf = _login(admin)

        # Admin: login events should appear in the audit log
        status, _, payload = admin.request("/api/audit")
        assert status == 200
        assert "entries" in payload
        assert "total" in payload
        assert payload["total"] >= 1
        assert any(e["action"] == "login" for e in payload["entries"])

        # Pagination: limit and offset
        status, _, page = admin.request("/api/audit?limit=1&offset=0")
        assert status == 200
        assert len(page["entries"]) == 1

        # Filter by action
        status, _, filtered = admin.request("/api/audit?action=login")
        assert status == 200
        assert all(e["action"] == "login" for e in filtered["entries"])

        # Filter by username
        status, _, by_user = admin.request("/api/audit?username=admin")
        assert status == 200
        assert all(e["username"] == "admin" for e in by_user["entries"])

        # Viewer is denied (create one first via admin)
        admin.request("/api/users", method="POST", json_body={"username": "viewer1", "password": "Viewer123!", "role": "viewer"}, headers={"X-CSRF-Token": csrf})
        _login(viewer, username="viewer1", password="Viewer123!")
        status, _, _ = viewer.request("/api/audit")
        assert status == 403

        # Unauthenticated is denied
        status, _, _ = unauth.request("/api/audit")
        assert status == 401
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_audit_log_records_admin_actions(tmp_path, monkeypatch):
    app, _ = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        csrf = _login(admin)

        # Create a user -> should record a 'create' / 'user' entry
        admin.request("/api/users", method="POST", json_body={"username": "newuser", "password": "NewUser1!", "role": "viewer"}, headers={"X-CSRF-Token": csrf})

        status, _, payload = admin.request("/api/audit?action=create&resource=user")
        assert status == 200
        assert payload["total"] >= 1
        entry = payload["entries"][0]
        assert entry["action"] == "create"
        assert entry["resource"] == "user"
        assert entry["username"] == "admin"
        assert entry["status"] == "success"
        assert entry.get("details", {}).get("username") == "newuser"

        # Failed login -> should record a 'login' / 'failed' entry
        bad = LocalClient(base_url)
        bad.request("/login")
        bad_csrf = bad.cookie("daygle_csrf") or ""
        bad.request("/login", method="POST", form={"username": "admin", "password": "wrong", "csrf_token": bad_csrf}, follow_redirects=False)

        status, _, logins = admin.request("/api/audit?action=login")
        assert status == 200
        failed = [e for e in logins["entries"] if e["status"] == "failed"]
        assert len(failed) >= 1
        assert failed[0]["username"] == "admin"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
