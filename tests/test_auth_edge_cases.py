"""Integration tests for hardened auth edge cases.

Covers every fix applied in the auth-hardening conversation:

1. **Stale-CSRF logout resilience** — POST /logout with a wrong or missing
   CSRF token must still delete the session and return ``{'ok': True}``
   instead of 403.

2. **Session deletion is permanent** — After a stale-CSRF logout, the old
   session cookie must not authenticate subsequent API calls.

3. **Normal logout still works** — POST /logout with a valid CSRF token
   behaves identically.

Tests start a real uvicorn server (same pattern as test_api.py's ``_load_app``
+ ``_server`` + ``LocalClient``).
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ── Test infrastructure (minimal copy from test_api.py) ────────────────
# These are intentionally kept local rather than imported, so the test
# file can be run standalone without a test_api.py dependency.

class LocalClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        import http.cookiejar
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

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
        if follow_redirects:
            opener = self.opener
        else:
            opener = build_opener(HTTPCookieProcessor(self.cookies), _NoRedirect())
        request = Request(
            f"{self.base_url}{path}",
            data=request_data,
            method=method,
            headers=request_headers,
        )
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, dict(response.headers), _body(response)
        except HTTPError as exc:
            return exc.code, dict(exc.headers), _error_body(exc)


class _NoRedirect(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        fp.status = code
        fp.code = code
        fp.headers = headers
        return fp

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def _body(response):
    data = response.read()
    if "application/json" in response.headers.get("content-type", ""):
        return json.loads(data.decode("utf-8"))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def _error_body(exc: HTTPError):
    text = exc.read().decode("utf-8")
    if "application/json" in exc.headers.get("content-type", ""):
        return json.loads(text)
    return text


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _load_app(tmp_path: Path, monkeypatch):
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
  pre_event_seconds: 5
  post_event_seconds: 10
  max_clip_seconds: 60
  format: mp4
  retention_days: 14
  max_storage_gb: 20
  auto_purge_enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAYGLE_CONFIG", str(config_path))
    for mod in list(sys.modules.keys()):
        if mod == "app" or mod.startswith("app."):
            sys.modules.pop(mod, None)
    main_mod = importlib.import_module("app.main")
    main_mod._startup()
    return main_mod.app, database_path


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


def _setup_admin(client: LocalClient) -> None:
    status, _headers, body = client.request("/setup")
    assert status == 200
    csrf = client.cookie("daygle_csrf")
    status, headers, _body_text = client.request(
        "/setup",
        method="POST",
        form={"username": "admin", "password": "Admin123!", "confirm_password": "Admin123!", "csrf_token": csrf or ""},
        follow_redirects=False,
    )
    assert status == 303


def _login(client: LocalClient) -> str:
    status, _headers, _body_text = client.request("/login")
    assert status == 200
    csrf = client.cookie("daygle_csrf")
    status, headers, _body_text = client.request(
        "/login",
        method="POST",
        form={"username": "admin", "password": "Admin123!", "csrf_token": csrf or ""},
        follow_redirects=False,
    )
    assert status == 303
    assert client.cookie("daygle_session")
    status, _headers, me = client.request("/api/auth/me")
    assert status == 200
    return me["csrf_token"]


# ── Auth edge case tests ───────────────────────────────────────────────


class TestStaleCsrfLogoutResilience:
    """POST /logout must be resilient to stale or missing CSRF tokens.

    The fix in auth_router.py replaced a hard 403 with graceful session
    deletion when the CSRF token doesn't match. These tests verify that
    behaviour at the HTTP level.
    """

    def test_logout_with_valid_csrf_succeeds(self, tmp_path, monkeypatch):
        """Logout with a correct CSRF token works as expected."""
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)

            status, _headers, payload = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": csrf}
            )
            assert status == 200
            assert payload["ok"] is True

            # Session should be gone — next API call gets 401.
            status, _headers, _body = client.request("/api/status")
            assert status == 401
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_logout_with_wrong_csrf_returns_ok(self, tmp_path, monkeypatch):
        """Logout with a WRONG CSRF token deletes the session and returns 200.

        This is the primary resilience fix: a stale token must not prevent
        the user from logging out.
        """
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            # Send a deliberately wrong CSRF token.
            status, _headers, payload = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "this-is-wrong"}
            )
            assert status == 200, (
                f"Expected 200 with stale CSRF, got {status}: {payload}"
            )
            assert payload["ok"] is True

            # Session must be deleted — subsequent API call gets 401.
            status, _headers, _body = client.request("/api/status")
            assert status == 401
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_logout_with_missing_csrf_header_returns_ok(self, tmp_path, monkeypatch):
        """Logout with NO CSRF header deletes the session and returns 200.

        The edge case: when window.daygleAuth.csrfToken is null (cleared by
        a concurrent handleSessionLoss), the frontend sends POST /logout
        with an empty token header. The server must accept this.
        """
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            # Logout WITHOUT an X-CSRF-Token header.
            status, _headers, payload = client.request(
                "/logout", method="POST"
            )
            assert status == 200, (
                f"Expected 200 with missing CSRF header, got {status}: {payload}"
            )
            assert payload["ok"] is True

            # Session must be deleted.
            status, _headers, _body = client.request("/api/status")
            assert status == 401
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_logout_with_wrong_csrf_still_clears_cookies(self, tmp_path, monkeypatch):
        """After stale-CSRF logout, the session cookie is gone."""
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            session_before = client.cookie("daygle_session")
            assert session_before is not None

            status, _headers, payload = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "stale-token"}
            )
            assert status == 200
            assert payload["ok"] is True

            # The session cookie should be cleared (deleted or empty).
            session_after = client.cookie("daygle_session")
            assert session_after is None or session_after == "", (
                f"Session cookie should be cleared after stale-CSRF logout, "
                f"got: {session_after!r}"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_double_logout_does_not_crash(self, tmp_path, monkeypatch):
        """Two rapid POST /logout calls with the same session must not 500.

        This guards against a rare race: the frontend dispatches two logout
        requests in quick succession (e.g., the nav.js click handler fires
        twice due to an event-duplication bug). Both should return 200.
        """
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            # First logout — succeeds.
            status1, _headers, payload1 = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "stale-token"}
            )
            assert status1 == 200
            assert payload1["ok"] is True

            # Second logout with the same session cookie (which was deleted
            # above). The middleware returns 401 before reaching the handler.
            status2, _headers, _body = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "stale-token"}
            )
            # 401 is acceptable — the session is already gone, no crash.
            assert status2 in (200, 401), (
                f"Rapid second logout should not 500, got {status2}"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_stale_csrf_logout_after_session_timeout(self, tmp_path, monkeypatch):
        """Simulate session expiry, then logout with stale token.

        When the session has expired server-side but the client still has the
        old cookie, a stale-CSRF logout should be handled gracefully (not
        crash with 500 or reveal internal errors).
        """
        app, database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            # Manually expire the session in the database to simulate
            # timeout while the user still has the old cookie.
            import sqlite3
            with sqlite3.connect(database_path) as db:
                db.execute("UPDATE user_sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
                db.commit()

            # Now logout with what looks like a valid token but the session
            # is expired server-side.
            status, _headers, payload = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "any-token"}
            )
            # The middleware sees the now-expired session and returns 401
            # before the logout handler runs. The frontend's handleSessionLoss
            # would have already redirected, so this is fine — no crash.
            assert status in (200, 401), (
                f"Logout after session expiry should not crash, got {status}: {payload}"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=5)
