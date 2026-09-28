"""Tests for the four HIGH-severity fixes (H1-H4).

Covers:

- H1: ``POST /api/settings/ai/download-model`` requires ``model_name`` in YOLO_MODELS.
- H2: ``GET /api/users`` is admin-only (viewers get 403).
- H3: ``_read_uploaded_image`` rejects requests with ``Content-Length > MAX_UPLOAD_BYTES``.
- H4: ``PUT /api/profile`` requires ``current_password`` when changing ``email`` or
  ``username``; non-sensitive fields still update without proof of possession.

The ``LocalClient`` + ``_load_app`` + ``_setup_admin`` + ``_login`` harness now
comes from ``tests/support.py``, the canonical shared harness for every
integration suite.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin


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
            _login(viewer, 'viewer', 'Viewer123!')
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
