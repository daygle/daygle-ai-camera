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
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ):
        data = None
        request_headers = dict(headers or {})
        if form is not None:
            data = urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        opener = self.opener if follow_redirects else build_opener(HTTPCookieProcessor(self.cookies), NoRedirect)
        request = Request(f"{self.base_url}{path}", data=data, method=method, headers=request_headers)
        try:
            with opener.open(request, timeout=5) as response:  # noqa: S310 - local test server only
                return response.status, dict(response.headers), _body(response)
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8")


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


def _load_app(tmp_path: Path, monkeypatch):
def _request(
    base_url: str,
    path: str,
    method: str = "GET",
    params: dict[str, str] | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
):
    if params:
        path = f"{path}?{urlencode(params)}"
    request = Request(f"{base_url}{path}", data=data, headers=headers or {}, method=method)
    with urlopen(request, timeout=5) as response:  # noqa: S310 - local test server only
        body = response.read().decode("utf-8")
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.status, json.loads(body)
        return response.status, body


def _load_app(tmp_path: Path, monkeypatch, extra_ai: str = ""):
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / 'data' / 'daygle.sqlite3'
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
    app = _load_app(
        tmp_path,
        monkeypatch,
        extra_ai=f"""  backend: onnx
  model_path: {tmp_path / 'missing.onnx'}
  labels_path: models/coco.names
""",
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    try:
        deadline = time.time() + 5
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started

        try:
            _request(
                base_url,
                "/api/detect/test-image",
                method="POST",
                data=b"not really an image",
                headers={"Content-Type": "image/jpeg"},
            )
        except HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            assert exc.code == 400
            assert "ONNX model not found" in body["detail"] or "numpy is not installed" in body["detail"]
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected missing ONNX model to return HTTP 400")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_dashboard_and_api_endpoints(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, thread, f"http://127.0.0.1:{port}"


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
        assert {"users", "user_sessions", "login_attempts"}.issubset(tables)
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
            json_body={"password": "Viewer456!", "role": "admin", "is_active": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert updated["role"] == "admin"

        status, _headers, payload = client.request("/logout", method="POST", headers={"X-CSRF-Token": csrf})
        assert status == 200
        assert payload["ok"] is True
        assert client.request("/api/status")[0] == 401

        viewer_client = LocalClient(base_url)
        _login(viewer_client, "viewer", "Viewer456!")
        assert viewer_client.request("/api/status")[0] == 200
        assert created["event_id"] >= 1
        assert created["detections"]

        status_code, upload = _request(
            base_url,
            "/api/detect/test-image",
            method="POST",
            data=b"mock image bytes",
            headers={"Content-Type": "image/jpeg"},
        )
        assert status_code == 200
        assert upload["created"] is True
        assert upload["event_id"] >= 2
        assert upload["detections"]

        status_code, events = _request(base_url, "/api/events")
        assert status_code == 200
        assert len(events) == 2
        assert events[0]["source"] == "test-image"
        assert events[0]["detections"]

        status_code, detail = _request(base_url, f"/api/events/{created['event_id']}")
        assert status_code == 200
        assert detail["id"] == created["event_id"]

        label = upload["detections"][0]["label"]
        status_code, search = _request(base_url, "/api/events", params={"label": label})
        assert status_code == 200
        assert search

        status_code, alerts = _request(base_url, "/api/alerts")
        assert status_code == 200
        assert isinstance(alerts, list)

        status_code, stats = _request(base_url, "/api/stats")
        assert status_code == 200
        assert stats["total_events"] == 2

        status_code, runtime_config = _request(base_url, "/api/config")
        assert status_code == 200
        assert runtime_config["camera"]["backend"] == "mock"
        assert runtime_config["ai"]["backend"] == "mock"

        status_code, static_js = _request(base_url, "/static/app.js")
        assert status_code == 200
        assert "refreshAll" in static_js
        assert "detect/test-image" in static_js
    finally:
        server.should_exit = True
        thread.join(timeout=5)
