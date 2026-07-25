"""Tests for the three CRITICAL round-3 fixes.

C1 -- ``app.auth.AuthManager.update_user``: invalidating user_sessions when an
ACTUAL role change is requested so a stolen viewer cookie cannot silently
elevate to admin.

C2 -- ``app.request_helpers.write_audit_log``: ensuring credentials
(password / secret / token / api_key / credential) never reach
``audit_log.details`` even when callers stash them in the payload.

C3 -- ``app.ptz._soap``: stripping Basic-Auth userinfo and embedded URLs
from any URL-bearing exception before re-raising, so camera credentials
cannot leak through 4xx response bodies or log lines.

The tests run without a live SQLite database or network by mocking the
DB context manager and ``urllib.request.urlopen``.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from app.request_helpers import _redact_audit_details, write_audit_log
from app.ptz import _safe_url_for_error, _sanitize_error_body, _soap


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class FakeCursor:
    """Minimal DB cursor that returns a configurable ``rowcount``."""

    def __init__(self, rowcount: int = 1):
        self.rowcount = rowcount


class FakeDb:
    """Minimal DB that records execute() calls for inspection."""

    def __init__(self, rowcount: int = 1):
        self.calls: list[tuple] = []
        self._rowcount = rowcount

    def execute(self, sql: str, params: tuple = ()) -> FakeCursor:
        self.calls.append((sql, tuple(params)))
        return FakeCursor(self._rowcount)


@contextmanager
def _fake_connect(db: FakeDb):
    """Context manager that yields ``db`` to mirror the ``with self.connect() as db`` idiom."""
    yield db


# ---------------------------------------------------------------------------
# C1 -- auth.update_user: invalidate sessions on ACTUAL role change
# ---------------------------------------------------------------------------


class UpdateUserSessionInvalidationTests(unittest.TestCase):
    """Auth.update_user must DELETE user_sessions iff the role actually changes."""

    AUTH_MODULE = 'app.auth'

    def _build_manager(self, *, current_role: str, rowcount: int = 1) -> mock.Mock:
        """Build a mock AuthManager pre-loaded with a user of role ``current_role``."""
        manager = mock.Mock()
        manager.get_user.return_value = {
            'id': 42,
            'username': 'alice',
            'role': current_role,
            'is_active': 1,
        }
        manager.validate_password_complexity.return_value = []
        manager.hash_password.return_value = 'hashed'
        manager.utc_now = mock.Mock(return_value='2026-07-25T00:00:00Z')
        manager.connect.return_value = _fake_connect(FakeDb(rowcount=rowcount))
        # .update_user on the real class -- attach to a Mock spec.
        return manager

    def _run_update_user(self, manager: mock.Mock, **kwargs) -> FakeDb:
        """Invoke the real AuthManager.update_user against a mock manager."""
        from app.auth import AuthManager  # late import so spec resolves correctly

        # Bind the method to our mock -- AuthManager.update_user is a normal method.
        method = AuthManager.update_user
        return_db = manager.connect.return_value.__enter__.return_value
        method(manager, 42, **kwargs)
        return return_db

    def test_role_change_viewer_to_admin_invalidates_sessions(self):
        """Promoting viewer -> admin must DELETE user_sessions for that user."""
        manager = self._build_manager(current_role='viewer', rowcount=1)
        db = self._run_update_user(manager, role='admin')

        session_deletes = [
            sql for (sql, _params) in db.calls
            if 'DELETE FROM user_sessions' in sql and 'user_id = ?' in sql
        ]
        self.assertEqual(
            len(session_deletes), 1,
            f'expected one DELETE FROM user_sessions, got calls: {db.calls}',
        )
        # The DELETE must target user_id 42; the `params` recorded alongside
        # the SQL is the second tuple element.
        delete_params = [
            params for (sql, params) in db.calls
            if 'DELETE FROM user_sessions' in sql
        ]
        self.assertEqual(delete_params, [(42,)])

    def test_role_change_admin_to_viewer_also_invalidates(self):
        """Demoting admin -> viewer must also INVALIDATE sessions (privilege loss)."""
        manager = self._build_manager(current_role='admin', rowcount=1)
        db = self._run_update_user(manager, role='viewer')

        session_deletes = [
            sql for (sql, _params) in db.calls
            if 'DELETE FROM user_sessions' in sql
        ]
        self.assertEqual(len(session_deletes), 1)

    def test_same_role_is_no_op_for_sessions(self):
        """Resubmitting the current role (e.g. role='admin' on an admin user) must NOT delete sessions."""
        manager = self._build_manager(current_role='admin', rowcount=1)
        db = self._run_update_user(manager, role='admin')

        session_deletes = [
            sql for (sql, _params) in db.calls
            if 'DELETE FROM user_sessions' in sql
        ]
        self.assertEqual(
            len(session_deletes), 0,
            'no-op role submission must not invalidate sessions',
        )

    def test_deactivate_user_still_invalidates_sessions(self):
        """is_active=False path remains untouched -- sessions still invalidated."""
        manager = self._build_manager(current_role='viewer', rowcount=1)
        db = self._run_update_user(manager, is_active=False)

        session_deletes = [
            sql for (sql, _params) in db.calls
            if 'DELETE FROM user_sessions' in sql
        ]
        self.assertEqual(len(session_deletes), 1)

    def test_nonexistent_user_short_circuits_without_session_delete(self):
        """get_user returning None must raise AuthError BEFORE any UPDATE/DELETE attempts."""
        from app.auth import AuthError

        manager = mock.Mock()
        manager.get_user.return_value = None
        manager.connect.return_value = _fake_connect(FakeDb())

        with self.assertRaises(AuthError):
            from app.auth import AuthManager
            AuthManager.update_user(manager, 42, role='admin')
        self.assertEqual(manager.connect.call_count, 0)

    def test_failed_update_does_not_delete_sessions(self):
        """If UPDATE returns 0 rows, raise AuthError BEFORE the session DELETE runs."""
        from app.auth import AuthError

        manager = self._build_manager(current_role='viewer', rowcount=0)
        with self.assertRaises(AuthError):
            from app.auth import AuthManager
            AuthManager.update_user(manager, 42, role='admin')

        # We never connected, so there must be no session delete recorded.
        # (the call to connect would have happened inside the try block, which
        # raised before DELETE.)
        db = manager.connect.return_value.__enter__.return_value
        session_deletes = [
            sql for (sql, _params) in db.calls
            if 'DELETE FROM user_sessions' in sql
        ]
        self.assertEqual(
            session_deletes, [],
            f'unexpected session deletes after a failed UPDATE: {db.calls}',
        )


# ---------------------------------------------------------------------------
# C2 -- request_helpers.write_audit_log: credential redaction
# ---------------------------------------------------------------------------


class RedactAuditDetailsTests(unittest.TestCase):
    """``_redact_audit_details`` must redact any sensitive key value at any depth."""

    def test_top_level_password_key(self):
        self.assertEqual(
            _redact_audit_details({'password': 'hunter2'}),
            {'password': '***'},
        )

    def test_case_insensitive_keys(self):
        redacted = _redact_audit_details({
            'PASSWORD': 'x',
            'Secret': 'y',
            'API_KEY': 'z',
            'access_key': 'a',
            'credential': 'b',
        })
        self.assertEqual(
            redacted,
            {'PASSWORD': '***', 'Secret': '***', 'API_KEY': '***',
             'access_key': '***', 'credential': '***'},
        )

    def test_token_substring_keys(self):
        """Substring (not whole-word) matching catches smtp_token, api_token, etc."""
        redacted = _redact_audit_details({
            'smtp_token': 'abc',
            'api_token': 'def',
            'tokenizer': 'ghi',  # substring match; intentionally over-redacted
            'plain_setting': 'preserve',
        })
        self.assertEqual(
            redacted,
            {'smtp_token': '***', 'api_token': '***', 'tokenizer': '***',
             'plain_setting': 'preserve'},
        )

    def test_nested_dict(self):
        redacted = _redact_audit_details({
            'alert_email': {
                'host': 'mail.example.com',
                'port': 587,
                'username': 'alert@example.com',
                'password': 's3cret',
            },
        })
        self.assertEqual(
            redacted,
            {'alert_email': {
                'host': 'mail.example.com',
                'port': 587,
                'username': 'alert@example.com',
                'password': '***',
            }},
        )

    def test_list_inside_value(self):
        redacted = _redact_audit_details([
            {'name': 'a', 'password': 'x'},
            {'name': 'b', 'token': 'y'},
            'unrelated-string',
        ])
        self.assertEqual(
            redacted,
            [
                {'name': 'a', 'password': '***'},
                {'name': 'b', 'token': '***'},
                'unrelated-string',
            ],
        )

    def test_non_string_values_preserved(self):
        """Int/bool/float/None scalars are returned unchanged."""
        for value in (1, True, False, 3.14, None, 'a_string_that_does_not_match'):
            self.assertEqual(_redact_audit_details(value), value)

    def test_dict_with_no_sensitive_keys_is_recursed(self):
        redacted = _redact_audit_details({
            'cameras': [
                {'id': 'cam1', 'username': 'admin', 'password': 'p1'},
                {'id': 'cam2', 'username': 'admin2'},
            ],
        })
        self.assertEqual(
            redacted,
            {'cameras': [
                {'id': 'cam1', 'username': 'admin', 'password': '***'},
                {'id': 'cam2', 'username': 'admin2'},
            ]},
        )

    def test_write_audit_log_strips_passwords_before_insert(self):
        """End-to-end: write_audit_log applies the redactor before calling add_audit_log."""
        captured: dict = {}

        class FakeEventDatabase:
            def add_audit_log(self, **kwargs):
                captured.update(kwargs)

        request = SimpleNamespace(state=SimpleNamespace(user={'id': 1, 'username': 'alice'}))
        write_audit_log(
            request=request,
            database=FakeEventDatabase(),
            action='update',
            resource='alert_email_settings',
            details={'host': 'smtp.example.com', 'password': 'hunter2', 'smtp_token': 'tok'},
            status='success',
        )

        self.assertEqual(captured['details'], {
            'host': 'smtp.example.com',
            'password': '***',
            'smtp_token': '***',
        })


# ---------------------------------------------------------------------------
# C3 -- ptz._soap: URL sanitization on every URL-bearing exception
# ---------------------------------------------------------------------------


class SafeUrlForErrorTests(unittest.TestCase):
    """``_safe_url_for_error`` strips Basic-Auth userinfo while preserving the host."""

    def test_basic_auth_userinfo_stripped(self):
        sanitized = _safe_url_for_error('http://admin:hunter2@192.168.1.20/onvif/ptz')
        self.assertNotIn('hunter2', sanitized)
        self.assertNotIn('admin', sanitized.split('192.168.1.20')[0])
        self.assertIn('192.168.1.20', sanitized)

    def test_url_without_userinfo_unchanged(self):
        url = 'http://192.168.1.20/onvif/ptz'
        self.assertEqual(_safe_url_for_error(url), url)

    def test_ipv6_host_kept_intact(self):
        # urlparse keeps IPv6 in brackets in netloc; sanitize must not strip the host.
        sanitized = _safe_url_for_error('http://user:pass@[::1]:8000/onvif')
        self.assertNotIn('user', sanitized)
        self.assertNotIn('pass', sanitized)
        # Host (with brackets) is preserved.
        self.assertIn('[::1]:8000', sanitized)

    def test_port_preserved(self):
        sanitized = _safe_url_for_error('https://user:pw@cam.example.com:8443/path')
        self.assertIn('cam.example.com:8443', sanitized)

    def test_rtsp_url_stripped(self):
        # Although _soap only does HTTP, _safe_url_for_error is the public helper
        # so it must also scrub rtsp://user:pass@ URLs in case future callers use it.
        sanitized = _safe_url_for_error('rtsp://user:pw@cam.local/stream')
        self.assertNotIn('user', sanitized)
        self.assertNotIn('pw', sanitized)
        self.assertIn('cam.local', sanitized)


class SoapExceptionScrubbingTests(unittest.TestCase):
    """``_soap`` must NEVER let the original URL or password appear in any raised exception.

    We mock ``urllib.request.urlopen`` to raise each exception class in turn,
    capturing the OSError that ``_soap`` re-raises and asserting the rendered
    string contains neither the userinfo bits nor the embedded password.
    """

    SECRET_USER = 'adminCAM'
    SECRET_PASSWORD = 'TopSecret2026!'
    TARGET_URL = f'http://{SECRET_USER}:{SECRET_PASSWORD}@192.168.1.20/onvif/ptz'
    SEND_URL = TARGET_URL

    def _assert_clean(self, message: str):
        # The exact userinfo must NOT appear anywhere in the rendered exception.
        self.assertNotIn(self.SECRET_USER, message)
        self.assertNotIn(self.SECRET_PASSWORD, message)
        # And the unsanitised URL form must be absent too.
        self.assertNotIn(f'{self.SECRET_USER}:{self.SECRET_PASSWORD}@', message)

    def test_http_error_sanitized(self):
        """HTTPError raised by urlopen -- body bytes and URL both scrubbed."""
        fake_resp = mock.Mock()
        fake_resp.fp = mock.Mock()
        fake_resp.fp.read.return_value = (
            b'<fault>camera blocked the request: '
            + f'{self.TARGET_URL} were credentials'.encode('utf-8')
        )
        fake_resp.code = 401
        error = mock.Mock()
        error.read = mock.Mock(return_value=fake_resp.fp.read.return_value)

        http_exc = type('HTTPError', (Exception,), {})(
            'HTTP Error 401', {'fp': fake_resp.fp, 'code': 401}
        )
        # Build a real urllib HTTPError-like object -- simpler to subclass.
        import urllib.error
        http_exc = urllib.error.HTTPError(
            self.TARGET_URL, 401, 'Unauthorized', {}, fake_resp.fp
        )

        with mock.patch('urllib.request.urlopen', side_effect=http_exc):
            with self.assertRaises(OSError) as ctx:
                _soap(self.TARGET_URL, '<s:Body/>', self.SECRET_USER, self.SECRET_PASSWORD)
        self._assert_clean(str(ctx.exception))

    def test_url_error_sanitized(self):
        """URLError raised by urlopen -- reason string is scrubbed."""
        import urllib.error
        url_exc = urllib.error.URLError(f'connection refused to {self.TARGET_URL}')

        with mock.patch('urllib.request.urlopen', side_effect=url_exc):
            with self.assertRaises(OSError) as ctx:
                _soap(self.TARGET_URL, '<s:Body/>', self.SECRET_USER, self.SECRET_PASSWORD)
        self._assert_clean(str(ctx.exception))

    def test_oserror_sanitized(self):
        """Generic OSError (e.g. ConnectionResetError) -- str(exc) is scrubbed."""
        os_exc = OSError(f'Reset by peer: {self.TARGET_URL}')

        with mock.patch('urllib.request.urlopen', side_effect=os_exc):
            with self.assertRaises(OSError) as ctx:
                _soap(self.TARGET_URL, '<s:Body/>', self.SECRET_USER, self.SECRET_PASSWORD)
        self._assert_clean(str(ctx.exception))

    def test_unexpected_exception_sanitized(self):
        """A ValueError or arbitrary exception -- the host and code are surfaced, no creds."""
        value_exc = ValueError(f'something broke at {self.TARGET_URL}')

        with mock.patch('urllib.request.urlopen', side_effect=value_exc):
            with self.assertRaises(OSError) as ctx:
                _soap(self.TARGET_URL, '<s:Body/>', self.SECRET_USER, self.SECRET_PASSWORD)
        self._assert_clean(str(ctx.exception))


class SanitizeErrorBodyTests(unittest.TestCase):
    """``_sanitize_error_body`` strips any embedded URL with userinfo."""

    def test_strips_userinfo_from_response_body(self):
        body = (
            'Camera returned 500: <detail>see request log at '
            f'http://admin:hunter2@192.168.1.50/onvif for details</detail>'
        )
        sanitized = _sanitize_error_body(body)
        self.assertNotIn('hunter2', sanitized)
        self.assertNotIn('admin', sanitized)
        # The host part is preserved.
        self.assertIn('192.168.1.50', sanitized)

    def test_leaves_non_url_strings_intact(self):
        body = 'Camera returned 500: <detail>generic error</detail>'
        self.assertEqual(_sanitize_error_body(body), body)


if __name__ == '__main__':
    unittest.main()
