"""Tests for the two MEDIUM defence-in-depth fixes (M1 + M2).

M1 - handler-level auth gates for /api/live/*, /api/recordings/*, and
``GET /api/settings/system``. Middleware already enforces session/admin
behaviour, so the behavioural test surface is small; the tests in this
file focus on the two-step delete flow (which is the user-facing
contract of M2) and a structural check that the new handler-level
gates exist in source.

M2 - preview-then-confirm flow for the destructive
``DELETE /api/system/runtime-data`` wipe. The preview issues a
single-use 30-second ``confirm_token``; the DELETE endpoint requires
``?confirm=true`` AND a matching ``X-Runtime-Data-Confirm`` header
issued to the same admin within the TTL. Tokens do not transfer
between admin sessions.

The ``LocalClient`` + ``_load_app`` + ``_setup_admin`` + ``_login`` harness
now comes from ``tests/support.py``, which is the canonical shared harness
for every integration suite.
"""

from __future__ import annotations

import inspect
import json
import sqlite3

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin


# ── M1: structural check of handler-level gates ─────────────────────────


class TestM1HandlerLevelGates:
    """Source-level check that each M1-fixed handler declares a
    ``request: Request`` parameter and calls one of
    ``require_user`` / ``require_admin`` early in its body.

    Behavioural coverage is light: middleware already enforces the
    same gate, so a positive integration test would pass even without
    the handler-level call. The structural check pins the fix so a
    future refactor that drops the call is caught.
    """

    def test_live_router_handlers_have_handler_level_user_gate(self):
        import app.api.live_router as lr
        for fn_name in ('live_detection_status_api', 'live_snapshot'):
            fn = getattr(lr, fn_name)
            src = inspect.getsource(fn)
            assert 'Request' in src or 'request' in src, (
                f'{fn_name} must declare request: Request for the M1 gate'
            )
            assert 'require_user' in src, (
                f'{fn_name} must call require_user at the handler level (M1)'
            )

    def test_recordings_router_handlers_have_handler_level_user_gate(self):
        import app.api.recordings_router as rr
        for fn_name in (
            'recordings', 'recordings_timeline', 'recording_detail',
            'stream_recording', 'download_recording',
        ):
            fn = getattr(rr, fn_name)
            src = inspect.getsource(fn)
            assert 'request' in src, (
                f'{fn_name} must declare request: Request for the M1 gate'
            )
            assert 'require_user' in src, (
                f'{fn_name} must call require_user at the handler level (M1)'
            )

    def test_settings_system_router_get_system_settings_have_admin_gate(self):
        import app.api.settings_system_router as sr
        src = inspect.getsource(sr.get_system_settings)
        assert 'request' in src, (
            'get_system_settings must declare request: Request for the M1 admin gate'
        )
        assert 'require_admin' in src, (
            'get_system_settings must call require_admin at the handler level (M1)'
        )


# ── M2: two-step delete for runtime-data ───────────────────────────────


class TestM2RuntimeDataTwoStepDelete:
    """``DELETE /api/system/runtime-data`` now requires:

    1. ``?confirm=true`` query param (literal "true")
    2. ``X-Runtime-Data-Confirm`` header matching the token issued to
       the same admin through the preview endpoint within
       ``_RUNTIME_DELETE_TOKEN_TTL_SECONDS``.

    Any missing/expired/wrong-owner token yields HTTP 400 with a short
    "what to do next" detail. Each preview issues a fresh token; a
    successfully consumed token cannot be re-used.
    """

    def _preview(self, admin_client: LocalClient, csrf: str):
        return admin_client.request(
            '/api/system/runtime-data/preview',
            method='POST',
            headers={'X-CSRF-Token': csrf},
        )

    def test_preview_returns_token_and_counts(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)
            status, _h, body = self._preview(client, csrf)
            assert status == 200, (status, body)
            assert body['ok'] is True
            assert 'confirm_token' in body
            assert isinstance(body['confirm_token'], str) and len(body['confirm_token']) >= 16
            assert body['expires_in'] == 30
            assert 'counts' in body
            assert set(body['counts']) == {
                'recordings', 'events', 'alerts', 'objects', 'camera_diagnostics',
            }
            assert body['preserved'] == ['settings', 'users', 'sessions', 'rules']
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_preview_is_audit_logged(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)
            self._preview(client, csrf)
            with sqlite3.connect(str(_db)) as conn:
                row = conn.execute(
                    "SELECT action, resource, details FROM audit_log "
                    "WHERE action = 'preview_delete_all' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                assert row is not None, 'Preview MUST be recorded in the audit log.'
                assert row[1] == 'runtime_data'
                # ``details`` is JSON text in this DB; just confirm it
                # contains the flag we wrote.
                details = json.loads(row[2]) if row[2] else {}
                assert details.get('confirm_challenge_issued') is True
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_delete_without_confirm_query_param_returns_400(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)
            status, _, body = client.request(
                '/api/system/runtime-data', method='DELETE',
                headers={'X-CSRF-Token': csrf},
            )
            assert status == 400, (status, body)
            assert '?confirm=true' in body['detail']
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_delete_without_token_header_returns_400(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)
            status, _, body = client.request(
                '/api/system/runtime-data?confirm=true', method='DELETE',
                headers={'X-CSRF-Token': csrf},
            )
            assert status == 400, (status, body)
            assert 'X-Runtime-Data-Confirm' in body['detail']
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_delete_with_invalid_token_returns_400(self, tmp_path, monkeypatch):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)
            status, _, body = client.request(
                '/api/system/runtime-data?confirm=true', method='DELETE',
                headers={
                    'X-CSRF-Token': csrf,
                    'X-Runtime-Data-Confirm': 'not-a-real-token',
                },
            )
            assert status == 400, (status, body)
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_full_preview_then_delete_with_valid_token_wipes_data(
        self, tmp_path, monkeypatch,
    ):
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)

            # Seed a couple of rows so the wipe actually has work to do.
            with sqlite3.connect(str(_db)) as conn:
                conn.execute(
                    "INSERT INTO events (created_at, source, snapshot_path, "
                    "alert_triggered, dismissed, metadata) VALUES "
                    "('2024-01-01T00:00:00+00:00', 'camera', '', 0, 0, '{}')"
                )
                conn.execute(
                    "INSERT INTO cameras (...) VALUES (...)"
                ) if False else None  # placeholder to keep schema consistent
                conn.commit()

            # Preview first.
            status, _h, preview_body = self._preview(client, csrf)
            assert status == 200
            counts_before = preview_body['counts']
            assert counts_before['events'] >= 1
            token = preview_body['confirm_token']

            # Now consume the token.
            status, _h, delete_body = client.request(
                '/api/system/runtime-data?confirm=true', method='DELETE',
                headers={
                    'X-CSRF-Token': csrf,
                    'X-Runtime-Data-Confirm': token,
                },
            )
            assert status == 200, (status, delete_body)
            assert delete_body['ok'] is True
            assert 'deleted' in delete_body

            # Verify the wipe actually happened events-side: post-delete
            # preview should report zero events.
            status, _h, post_body = self._preview(client, csrf)
            assert status == 200
            assert post_body['counts']['events'] == 0
            # users MUST survive (preserved).
            with sqlite3.connect(str(_db)) as conn:
                admin_rows = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1"
                ).fetchone()[0]
                assert admin_rows >= 1, 'admin login must survive the wipe'
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_consumed_token_cannot_be_reused(self, tmp_path, monkeypatch):
        """A token consumed by a successful DELETE is gone from the store,
        so an immediate second use of the same token must be rejected.
        """
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)

            status, _h, preview_body = self._preview(client, csrf)
            token = preview_body['confirm_token']

            # First DELETE: succeeds.
            status, _, _ = client.request(
                '/api/system/runtime-data?confirm=true', method='DELETE',
                headers={'X-CSRF-Token': csrf, 'X-Runtime-Data-Confirm': token},
            )
            assert status == 200
            # Second DELETE with the same token: rejected.
            status, _, body = client.request(
                '/api/system/runtime-data?confirm=true', method='DELETE',
                headers={'X-CSRF-Token': csrf, 'X-Runtime-Data-Confirm': token},
            )
            assert status == 400, (status, body)
            assert 'token' in body['detail'].lower()
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_cross_admin_token_is_rejected(self, tmp_path, monkeypatch):
        """Admin A's preview token cannot be redeemed by admin B even if A
        leaks the ``X-Runtime-Data-Confirm`` value to B. The token is
        keyed by user id; consume-by-different-user-id returns the
        generic ``not recognised`` error.
        """
        app, _db = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        admin_a = LocalClient(base_url)
        try:
            _setup_admin(admin_a)
            csrf_a = _login(admin_a)

            # Create a viewer-role second admin (using API since the
            # bootstrap user is admin-only).
            status, _, _ = admin_a.request(
                '/api/users', method='POST',
                json_body={'username': 'admin2', 'password': 'Admin123!', 'role': 'admin'},
                headers={'X-CSRF-Token': csrf_a},
            )
            assert status == 200

            # Admin A fetches a preview token.
            status, _h, preview_body = self._preview(admin_a, csrf_a)
            assert status == 200
            leaked_token = preview_body['confirm_token']

            # Admin B logs in and tries to use A's token.
            admin_b = LocalClient(base_url)
            csrf_b = _login(admin_b, 'admin2', 'Admin123!')

            status, _, body = admin_b.request(
                '/api/system/runtime-data?confirm=true', method='DELETE',
                headers={
                    'X-CSRF-Token': csrf_b,
                    'X-Runtime-Data-Confirm': leaked_token,
                },
            )
            assert status == 400, (status, body)
            # Confirm A's token is STILL VALID for A (it wasn't consumed).
            status, _, body = admin_a.request(
                '/api/system/runtime-data?confirm=true', method='DELETE',
                headers={
                    'X-CSRF-Token': csrf_a,
                    'X-Runtime-Data-Confirm': leaked_token,
                },
            )
            assert status == 200, (status, body)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
