from __future__ import annotations

import importlib
import json
import socket
import sqlite3
import sys
import threading
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

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


def _body(response):
    text = response.read().decode("utf-8")
    if "application/json" in response.headers.get("content-type", ""):
        return json.loads(text)
    return text


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
        assert client.request("/static/app.js")[0] == 200

        with sqlite3.connect(database_path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"users", "user_sessions", "login_attempts", "app_settings", "alert_rules"}.issubset(tables)
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
        initial = client.request("/api/settings/alerts")[2]
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
        triggered = main_module.alerts.process([{"label": "person", "confidence": 0.9}])
        assert any(alert["rule_name"] == "Person DB" for alert in triggered)

        status, _headers, deleted = client.request(f"/api/settings/alerts/{rule['id']}", method="DELETE", headers={"X-CSRF-Token": csrf})
        assert status == 200
        assert deleted["ok"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)
