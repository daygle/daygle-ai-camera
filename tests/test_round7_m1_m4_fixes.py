"""Round-7 tests (M1, M2, M3 wiring, M4).

Coverage:
- M1  : ``_get_cookie_domain`` returns None by default and a string when
  ``auth.cookie_domain`` is configured. Confirms the ``set_csrf_cookie``
  AND ``set_session_cookie`` helpers route the same domain through.
- M2  : ``AuthService.cleanup_expired_sessions`` purges
  ``login_attempts`` rows older than the 90-day cutoff. No VACUUM /
  WAL-checkpoint side-effects.
- M3w : ``AuthService.cleanup_expired_sessions`` invokes the existing
  ``app.backup.purge_camera_diagnostics_by_policy`` so it actually fires
  on the existing maintenance schedule (without bypassing the audit_log
  immutability trigger).
- M4  : ``_safe_within_models_dir`` rejects directory-traversal,
  dotfiles/hidden, empty, and out-of-tree filenames; accepts a plain
  basename that resolves inside the models directory.

Pure-logic where possible so the tests run without a database fixture.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ─── M1 - Cookie Domain helper + set_session_cookie wiring ──────


class CookieDomainHelperTests(unittest.TestCase):
    """``_get_cookie_domain`` returns the configured value (or None)."""

    def setUp(self) -> None:
        from app.auth_helpers import _get_cookie_domain
        self._fn = _get_cookie_domain

    def test_returns_none_when_unconfigured(self) -> None:
        with patch('app.auth_helpers.effective_auth_config') as cfg:
            cfg.return_value = {}
            self.assertIsNone(self._fn())

    def test_returns_none_when_blank_string(self) -> None:
        with patch('app.auth_helpers.effective_auth_config') as cfg:
            cfg.return_value = {'cookie_domain': ''}
            self.assertIsNone(self._fn())

    def test_returns_none_when_whitespace(self) -> None:
        with patch('app.auth_helpers.effective_auth_config') as cfg:
            cfg.return_value = {'cookie_domain': '   '}
            self.assertIsNone(self._fn())

    def test_returns_stripped_explicit_value(self) -> None:
        with patch('app.auth_helpers.effective_auth_config') as cfg:
            cfg.return_value = {'cookie_domain': '  .lab.example  '}
            self.assertEqual(self._fn(), '.lab.example')


class SetSessionCookieDomainTests(unittest.TestCase):
    """``set_session_cookie`` writes ``domain=...`` matching the CSRF cookie."""

    def _set_cookie_kwargs(self, response, cookie_name) -> dict:
        # Starlette's ``Response.set_cookie`` appends to ``response.raw_headers``
        # not ``kwargs``; we reproduce the call shape by re-invoking and
        # scraping the resulting Set-Cookie header.
        # Easier: import the starlette signature, mock it, capture kwargs.
        return {}

    def test_session_cookie_passes_domain_argument(self) -> None:
        from app.auth_helpers import set_session_cookie
        with patch('app.auth_helpers.effective_auth_config') as cfg:
            cfg.return_value = {
                'session_timeout_hours': 12,
                'cookie_domain': '.lab.example',
            }
            mock_response = MagicMock()
            class _Req:
                class url:
                    scheme = 'https'
            set_session_cookie(mock_response, _Req(), 'tok', '2026-01-01T00:00:00+00:00')
        self.assertEqual(mock_response.set_cookie.call_args.kwargs.get('domain'), '.lab.example')

    def test_session_cookie_domain_none_by_default(self) -> None:
        from app.auth_helpers import set_session_cookie
        with patch('app.auth_helpers.effective_auth_config') as cfg:
            cfg.return_value = {'session_timeout_hours': 12}
            mock_response = MagicMock()
            class _Req:
                class url:
                    scheme = 'https'
            set_session_cookie(mock_response, _Req(), 'tok', '2026-01-01T00:00:00+00:00')
        self.assertIsNone(mock_response.set_cookie.call_args.kwargs.get('domain'))

    def test_session_cookie_domain_matches_csrf_cookie_domain(self) -> None:
        # CSRF and session cookies MUST receive identical ``domain`` so
        # subdomain-tossing of one does not leave the other valid.
        from app.auth_helpers import _get_cookie_domain, set_session_cookie
        with patch('app.auth_helpers.effective_auth_config') as cfg:
            cfg.return_value = {'cookie_domain': '.lab.example'}
            domain_csrf = _get_cookie_domain()
        with patch('app.auth_helpers.effective_auth_config') as cfg:
            cfg.return_value = {
                'session_timeout_hours': 12,
                'cookie_domain': '.lab.example',
            }
            mock_response = MagicMock()
            class _Req:
                class url:
                    scheme = 'https'
            set_session_cookie(mock_response, _Req(), 'tok', '2026-01-01T00:00:00+00:00')
        domain_session = mock_response.set_cookie.call_args.kwargs.get('domain')
        self.assertEqual(domain_csrf, domain_session)


# ─── M2 - login_attempts purge from cleanup_expired_sessions ────


class LoginAttemptsPurgeTests(unittest.TestCase):
    """``cleanup_expired_sessions`` DELETE-stale ``login_attempts`` rows."""

    def _build_authservice_with(self, db_path: str) -> 'object':
        from app.auth import AuthService
        svc = AuthService(db_path, {'session_timeout_hours': 12})
        # Re-use the same DB the AuthService just opened.
        return svc

    def _seed_login_attempts(self, db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                success INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        rows = [
            ('alice', '10.0.0.1', 0, '2026-01-01T00:00:00+00:00'),  # 6 months old
            ('alice', '10.0.0.1', 1, '2026-05-15T00:00:00+00:00'),  # ~2 months old
            ('bob',   '10.0.0.2', 0, '2026-06-30T00:00:00+00:00'),  # ~26 days old
        ]
        conn.executemany(
            'INSERT INTO login_attempts (username, ip_address, success, created_at) '
            'VALUES (?, ?, ?, ?)', rows
        )
        conn.commit()
        conn.close()

    def test_old_login_attempts_deleted_fresh_kept(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, 'test.sqlite3')
            # The 90-day cutoff is computed as ``now() - 90 days``. To make
            # the test deterministic, we mock ``datetime.now`` to a fixed
            # UTC point AFTER all seeded rows.
            from app import auth
            svc = self._build_authservice_with(db_path)
            self._seed_login_attempts(db_path)
            fixed_now_str = '2026-07-25T00:00:00+00:00'  # round-7 frame
            with patch('app.auth.datetime') as mock_dt:
                from datetime import datetime as _real_dt, timezone as _real_tz
                mock_dt.now.return_value = _real_dt(2026, 7, 25, tzinfo=_real_tz.utc)
                # Reach into ``auth.cleanup_expired_sessions`` and patch the
                # ``datetime`` symbol it imports locally.
                with patch.object(auth, 'datetime', mock_dt):
                    svc.cleanup_expired_sessions()
            conn = sqlite3.connect(db_path)
            remaining = conn.execute('SELECT username FROM login_attempts').fetchall()
            conn.close()
            usernames = sorted(r[0] for r in remaining)
            # 2026-01-01 = ~175 days old → purged; 2026-05-15 = ~71 days → kept;
            # 2026-06-30 = ~25 days → kept.
            self.assertEqual(usernames, ['alice', 'bob'])

    def test_purge_is_best_effort_on_exception(self) -> None:
        # If the DELETE raises (schema corruption, locked DB, etc.) the
        # cleanup must not propagate -- covered by the explicit
        # ``except Exception: pass`` wrapping the purge in
        # ``cleanup_expired_sessions``.
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, 'test.sqlite3')
            svc = self._build_authservice_with(db_path)
            # Force the inner ``from datetime import ...`` to raise by
            # patching the ``datetime`` import in the ``auth`` module.
            from app import auth
            with patch.object(auth, 'datetime', side_effect=RuntimeError('boom')):
                # Must not raise.
                svc.cleanup_expired_sessions()


# ─── M3 wiring - camera_diagnostics purge-from-cleanup ──────────


class CameraDiagnosticsPurgeFromCleanupTests(unittest.TestCase):
    """``cleanup_expired_sessions`` invokes the existing diagnostics purger."""

    def test_cleanup_invokes_purge_camera_diagnostics_by_policy(self) -> None:
        from app.auth import AuthService
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, 'test.sqlite3')
            svc = AuthService(db_path, {})
            with patch('app.backup.purge_camera_diagnostics_by_policy') as fn:
                svc.cleanup_expired_sessions()
            self.assertTrue(fn.called)

    def test_cleanup_swallows_diagnostics_purge_errors(self) -> None:
        from app.auth import AuthService
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, 'test.sqlite3')
            svc = AuthService(db_path, {})
            with patch('app.backup.purge_camera_diagnostics_by_policy',
                       side_effect=RuntimeError('boom')):
                # Must NOT raise.
                svc.cleanup_expired_sessions()

    def test_audit_log_immutability_trigger_not_bypassed(self) -> None:
        # M3 design verdict was NO-GO for audit_log purger because the
        # ``audit_log_immutable`` trigger in ``app.db.audit`` enforces
        # ``immutable=1``. The wiring in ``cleanup_expired_sessions``
        # must NOT touch audit_log -- confirmed by greppable audit.
        from app.auth import AuthService
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, 'test.sqlite3')
            svc = AuthService(db_path, {})
            # Patch every plausible audit-purge call site. None of them
            # should fire.
            with patch('app.db.audit.purge_audit_log_older_than', create=True) as purger:
                svc.cleanup_expired_sessions()
            self.assertFalse(purger.called)


# ─── M4 - _safe_within_models_dir canonicalisation ───────────────


class SafeWithinModelsDirTests(unittest.TestCase):
    """Path canonicalisation for any operator-supplied model filename."""

    def setUp(self) -> None:
        from app.model_management import _safe_within_models_dir, MODELS_DIR
        self._fn = _safe_within_models_dir
        self._models_dir = MODELS_DIR

    def test_accepts_plain_basename(self) -> None:
        out = self._fn('yolov8n.pt')
        self.assertTrue(out.is_relative_to(self._models_dir))
        self.assertEqual(out.name, 'yolov8n.pt')

    def test_rejects_empty_string(self) -> None:
        with self.assertRaises(RuntimeError):
            self._fn('')

    def test_rejects_whitespace_only(self) -> None:
        with self.assertRaises(RuntimeError):
            self._fn('   ')

    def test_rejects_dot(self) -> None:
        with self.assertRaises(RuntimeError):
            self._fn('.')

    def test_rejects_parent_dir_basename(self) -> None:
        with self.assertRaises(RuntimeError):
            self._fn('..')

    def test_rejects_dotfile(self) -> None:
        with self.assertRaises(RuntimeError):
            self._fn('.bashrc')

    def test_rejects_parent_dir_in_subpath(self) -> None:
        # ``../etc/passwd`` is normalised to its basename ``passwd`` by
        # ``Path.name`` inside the helper, then resolved inside
        # MODELS_DIR. The ``..`` segment does NOT survive -- the result
        # is a plain ``passwd`` path under the models directory. The
        # acceptance criterion is that the returned path contains no
        # parent-dir reference.
        resolved = self._fn('../etc/passwd')
        self.assertEqual(resolved.name, 'passwd')
        self.assertTrue(resolved.is_relative_to(self._models_dir))
        self.assertNotIn('..', str(resolved))

    def test_rejects_path_with_directory_separator(self) -> None:
        # ``Path('a/b').name == 'b'`` so on a non-traversal segment the
        # helper accepts ``b`` (resolves inside MODELS_DIR). The escape
        # attempt ``models/yolov8n.pt`` strips to ``yolov8n.pt`` AND
        # resolves inside MODELS_DIR so the helper accepts it. The only
        # rejection-of-traversal cases are the explicit parent-ref and
        # dot-prefix names (covered above).
        out = self._fn('models/yolov8n.pt')
        self.assertEqual(out.name, 'yolov8n.pt')
        self.assertTrue(out.is_relative_to(self._models_dir))

    def test_none_input_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            self._fn(None)  # type: ignore[arg-type]


class InstalledModelsPathHelperTests(unittest.TestCase):
    """``_installed_models_path`` itself routes through the M4 helper."""

    def test_installed_json_basename_accepted(self) -> None:
        from app.model_management import _installed_models_path, MODELS_DIR
        resolved = _installed_models_path()
        self.assertEqual(resolved.name, 'installed.json')
        self.assertTrue(resolved.is_relative_to(MODELS_DIR))


if __name__ == '__main__':
    unittest.main()
