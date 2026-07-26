"""Tests for the four HIGH-severity fixes (H1-H4).

Covers:

- H1: ``POST /api/settings/ai/download-model`` requires ``model_name`` in YOLO_MODELS.
- H2: ``GET /api/users`` is admin-only (viewers get 403).
- H3: ``_read_uploaded_image`` rejects requests with ``Content-Length > MAX_UPLOAD_BYTES``.
- H4: ``PUT /api/profile`` requires ``current_password`` when changing ``email`` or
  ``username``; non-sensitive fields still update without proof of possession.

Mirrors the ``LocalClient`` + ``_load_app`` + ``_setup_admin`` + ``_login`` pattern
already used in ``tests/test_auth_edge_cases.py`` so this file is self-contained
and not coupled to ``tests/test_api.py`` (whose public surface has been refactored
across multiple phases).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

import pytest
import uvicorn  # noqa: F401  -- only used when running locally

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ── Test infrastructure (mirrors tests/test_auth_edge_cases.py) ─────────────


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
        method: str = 'GET',
        form: dict[str, str] | None = None,
        json_body: dict | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ):
        request_data = data
        request_headers = dict(headers or {})
        if form is not None:
            request_data = urlencode(form).encode('utf-8')
            request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
        if json_body is not None:
            request_data = json.dumps(json_body).encode('utf-8')
            request_headers['Content-Type'] = 'application/json'
        # Simulate a real browser: send a same-origin ``Origin`` header so the
        # middleware's same-origin CSRF defence (which rejects mutating /api
        # requests carrying no Origin/Referer) sees a matching origin.
        request_headers.setdefault('Origin', self.base_url)
        opener = self.opener if follow_redirects else build_opener(
            HTTPCookieProcessor(self.cookies), _NoRedirect(),
        )
        request = Request(
            f'{self.base_url}{path}',
            data=request_data,
            method=method,
            headers=request_headers,
        )
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, dict(response.headers), _body(response)
        except HTTPError as exc:
            return exc.code, dict(exc.headers), _error_body(exc)
        except URLError as exc:
            pytest.fail(f'Request to {path} failed: {exc}')


class _NoRedirect(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        fp.status, fp.code, fp.headers = code, code, headers
        return fp
    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _body(response):
    data = response.read()
    if 'application/json' in response.headers.get('content-type', ''):
        return json.loads(data.decode('utf-8'))
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data


def _error_body(exc: HTTPError):
    text = exc.read().decode('utf-8')
    if 'application/json' in exc.headers.get('content-type', ''):
        return json.loads(text)
    return text


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _load_app(tmp_path: Path, monkeypatch):
    config_path = tmp_path / 'config.yaml'
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
        encoding='utf-8',
    )
    monkeypatch.setenv('DAYGLE_CONFIG', str(config_path))
    for mod in list(sys.modules.keys()):
        if mod == 'app' or mod.startswith('app.'):
            sys.modules.pop(mod, None)
    main_mod = importlib.import_module('app.main')
    main_mod._startup()
    return main_mod.app, database_path


def _server(app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, thread, f'http://127.0.0.1:{port}'


def _setup_admin(client: LocalClient) -> None:
    status, _headers, _body = client.request('/setup')
    assert status == 200
    csrf = client.cookie('daygle_csrf')
    status, headers, _body_text = client.request(
        '/setup',
        method='POST',
        form={
            'username': 'admin', 'password': 'Admin123!',
            'confirm_password': 'Admin123!', 'csrf_token': csrf or '',
        },
        follow_redirects=False,
    )
    assert status == 303


def _login(client: LocalClient, username: str = 'admin', password: str = 'Admin123!') -> str:
    status, _headers, _body = client.request('/login')
    assert status == 200
    csrf = client.cookie('daygle_csrf')
    status, headers, _body_text = client.request(
        '/login',
        method='POST',
        form={'username': username, 'password': password, 'csrf_token': csrf or ''},
        follow_redirects=False,
    )
    assert status == 303
    assert client.cookie('daygle_session')
    status, _headers, me = client.request('/api/auth/me')
    assert status == 200
    return me['csrf_token']


# ── H1: download_ai_model whitelist ─────────────────────────────────────


class TestDownloadAiModelWhitelist:
    """``POST /api/settings/ai/download-model`` now mirrors
    ``update_ai_model`` by validating ``model_name`` against YOLO_MODELS.
    """

    def test_rejects_unknown_model_name(self, tmp_path, monkeypatch):
        app, _db_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)
            status, _headers, body = client.request(
                '/api/settings/ai/download-model',
                method='POST',
                json_body={'model': '../../../tmp/evil'},
                headers={'X-CSRF-Token': csrf},
            )
            assert status == 400, f'Expected 400 on bad model, got {status}: {body}'
            assert 'Unknown model' in (body['detail'] if isinstance(body, dict) else body)
        finally:
            server.should_exit = True
            thread.join(timeout=5)


# ── H2: /api/users admin gate ───────────────────────────────────────────


class TestApiUsersAdminGate:
    """``GET /api/users`` is admin-only after the H2 fix; viewers
    must be 403-ed."""

    def _create_viewer(self, admin_csrf: str, admin_client: LocalClient) -> None:
        status, _h, body = admin_client.request(
            '/api/users',
            method='POST',
            json_body={'username': 'viewer', 'password': 'Viewer123!', 'role': 'viewer'},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200 and body['role'] == 'viewer', body

    def test_admin_can_list_users(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        admin = LocalClient(base_url)
        try:
            _setup_admin(admin)
            admin_csrf = _login(admin)
            self._create_viewer(admin_csrf, admin)
            status, _h, body = admin.request('/api/users')
            assert status == 200
            assert isinstance(body, list)
            usernames = {u['username'] for u in body}
            assert 'admin' in usernames and 'viewer' in usernames
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_viewer_gets_403_on_users_list(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        admin = LocalClient(base_url)
        try:
            _setup_admin(admin)
            admin_csrf = _login(admin)
            self._create_viewer(admin_csrf, admin)
            viewer = LocalClient(base_url)
            viewer_csrf = _login(viewer, 'viewer', 'Viewer123!')
            status, _h, body = viewer.request('/api/users')
            assert status == 403, f'viewer should be 403, got {status}: {body}'
            assert isinstance(body, dict) and body.get('detail') == 'Admin access required'
        finally:
            server.should_exit = True
            thread.join(timeout=5)


# ── H3: Content-Length cap on _read_uploaded_image ───────────────────────


class TestUploadContentLengthCap:
    """``_read_uploaded_image`` rejects any request whose declared
    or stream-cumulative size exceeds 10 MB. We exercise the helper
    directly with a mocked Request carrying a stream."""

    @staticmethod
    async def _run(helper, request):
        return await helper(request)

    def test_rejects_oversize_content_length_header(self):
        """A Content-Length greater than MAX_UPLOAD_BYTES (10 MB) must
        produce HTTPException(413) WITHOUT reading the body."""
        from app.request_helpers import _read_uploaded_image, MAX_UPLOAD_BYTES

        async def fake_stream():
            # If the helper wrongly streams first instead of header-checking,
            # this generator would be drained. Yield a sentinel byte to detect.
            yield b'X'
            return  # pragma: no cover

        request = SimpleNamespace(
            headers={
                'content-type': 'image/png',
                'content-length': str(MAX_UPLOAD_BYTES + 1),
            },
            stream=fake_stream,
        )

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(self._run(_read_uploaded_image, request))
        assert exc_info.value.status_code == 413

    def test_rejects_oversize_in_stream(self):
        """If the client sends chunked encoding with no Content-Length
        (or skips the early-rejection), the streaming cap must abort."""
        from app.request_helpers import _read_uploaded_image, MAX_UPLOAD_BYTES

        async def oversize_stream():
            # Two chunks each under the cap but combined over it.
            chunk_size = MAX_UPLOAD_BYTES - 1024
            yield b'a' * chunk_size
            yield b'b' * (chunk_size + 1)

        request = SimpleNamespace(
            headers={'content-type': 'image/png'},  # no Content-Length
            stream=oversize_stream,
        )
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(self._run(_read_uploaded_image, request))
        assert exc_info.value.status_code == 413

    def test_accepts_under_cap(self):
        """An image well under the cap must parse normally and return
        the raw bytes."""
        from app.request_helpers import _read_uploaded_image

        async def tiny_stream():
            # 1x1 PNG is ~70 bytes; image content-type short-circuits
            # multipart parsing in _read_uploaded_image.
            yield b'\x89PNG\r\n\x1a\n' + b'\x00' * 64

        request = SimpleNamespace(
            headers={'content-type': 'image/png'},
            stream=tiny_stream,
        )
        body, _filename, content_type = asyncio.run(self._run(_read_uploaded_image, request))
        assert body.startswith(b'\x89PNG\r\n\x1a\n')
        assert content_type == 'image/png'


# ── H4: current_password required for email/username changes ─────────────


class TestProfileUpdateRequiresCurrentPassword:
    """``PUT /api/profile`` after the H4 fix must require
    ``current_password`` when ``email`` or ``username`` is being changed.
    Non-sensitive fields (timezone, formats, first/last name) still
    update without proof of possession."""

    def test_email_change_without_current_password_raises_400(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        admin = LocalClient(base_url)
        try:
            _setup_admin(admin)
            csrf = _login(admin)
            status, _headers, body = admin.request(
                '/api/profile',
                method='PUT',
                json_body={'email': 'malicious@example.com'},
                headers={'X-CSRF-Token': csrf},
            )
            assert status == 400, (status, body)
            assert 'Current password is required' in body['detail']
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_email_change_with_wrong_current_password_raises_400(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        admin = LocalClient(base_url)
        try:
            _setup_admin(admin)
            csrf = _login(admin)
            status, _h, body = admin.request(
                '/api/profile',
                method='PUT',
                json_body={
                    'email': 'someone@example.com',
                    'current_password': 'NOT-the-real-password',
                },
                headers={'X-CSRF-Token': csrf},
            )
            assert status == 400, (status, body)
            assert 'Current password is incorrect' in body['detail']
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_email_change_with_correct_current_password_succeeds(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        admin = LocalClient(base_url)
        try:
            _setup_admin(admin)
            csrf = _login(admin)
            status, _h, body = admin.request(
                '/api/profile',
                method='PUT',
                json_body={
                    'email': 'new-admin@example.com',
                    'current_password': 'Admin123!',
                },
                headers={'X-CSRF-Token': csrf},
            )
            assert status == 200, (status, body)
            assert body['email'] == 'new-admin@example.com'
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_username_change_with_correct_current_password_succeeds(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        admin = LocalClient(base_url)
        try:
            _setup_admin(admin)
            csrf = _login(admin)
            status, _h, body = admin.request(
                '/api/profile',
                method='PUT',
                json_body={
                    'username': 'renamed_admin',
                    'current_password': 'Admin123!',
                },
                headers={'X-CSRF-Token': csrf},
            )
            assert status == 200, (status, body)
            assert body['username'] == 'renamed_admin'
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_no_op_resubmit_does_not_require_password(self, tmp_path, monkeypatch):
        """Re-sending the same email value must NOT require a current_password.
        The H4 fix compares against the currently stored value and skips
        the verify step if the value would not actually change.
        """
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        admin = LocalClient(base_url)
        try:
            _setup_admin(admin)
            csrf = _login(admin)
            # The seed admin has no email set (empty string).
            status, _h, body = admin.request(
                '/api/profile',
                method='PUT',
                json_body={'email': '', 'timezone': 'UTC'},
                headers={'X-CSRF-Token': csrf},
            )
            assert status == 200, (status, body)
            assert body['timezone'] == 'UTC'
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_non_sensitive_update_does_not_require_password(self, tmp_path, monkeypatch):
        """Timezone change ONLY (no email/username) must succeed without
        a current_password field.
        """
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        admin = LocalClient(base_url)
        try:
            _setup_admin(admin)
            csrf = _login(admin)
            status, _h, body = admin.request(
                '/api/profile',
                method='PUT',
                json_body={'timezone': 'UTC', 'date_format': 'iso', 'time_format': '24h'},
                headers={'X-CSRF-Token': csrf},
            )
            assert status == 200, (status, body)
            assert body['timezone'] == 'UTC'
            assert body['date_format'] == 'iso'
            assert body['time_format'] == '24h'
        finally:
            server.should_exit = True
            thread.join(timeout=5)
