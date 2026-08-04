"""Tests for the four round-4 fixes.

H1: scripts/update.sh + scripts/install_debian.sh refuse non-daygle/daygle-ai-camera origin remotes.
H2: AuthService enforces an absolute session-expiry cap.
H3: clear_runtime_media_directory refuses to follow symbolic links at any depth.
M1: middleware._is_same_origin identifies cross-origin / null / missing-origin requests.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.auth import AuthService
from app.middleware import _is_same_origin
from app.recording_extension import clear_runtime_media_directory


REPO_DIR = str(Path(__file__).resolve().parents[1])


def _hermetic_git_env():
    """Run git/update.sh isolated from the developer's ambient git config.

    A dev/CI machine may carry global ``url.<x>.insteadOf`` rewrite rules
    (e.g. an agent proxy that rewrites ``https://github.com/`` to a local
    mirror). ``git remote get-url`` applies those rewrites, which would make
    update.sh see a rewritten origin and spuriously fail its allowlist check.
    Neutralising the global/system config keeps this test deterministic and
    exercises update.sh against the true stored remote URL.
    """
    env = dict(os.environ)
    env['GIT_CONFIG_GLOBAL'] = os.devnull
    env['GIT_CONFIG_SYSTEM'] = os.devnull
    return env


# H1 ---------------------------------------------------------------------


class UpdateScriptOriginGuardTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='daygle_test_origin_')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_git_repo(self, origin_url):
        repo = Path(self.tmpdir) / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=str(repo), check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 't@t'], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 't'], check=True)
        (repo / 'README').write_text('test')
        subprocess.run(['git', '-C', str(repo), 'add', 'README'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-q', '-m', 'init'], check=True)
        subprocess.run(
            ['git', '-C', str(repo), 'remote', 'add', 'origin', origin_url],
            check=True,
        )
        return str(repo)

    def test_update_sh_rejects_non_allowlisted_origin(self):
        bad_repo = self._make_git_repo('git@github.com:evil/malicious-fork.git')
        result = subprocess.run(
            ['bash', REPO_DIR + '/scripts/update.sh'],
            cwd=bad_repo, capture_output=True, text=True, check=False,
            env=_hermetic_git_env(),
        )
        self.assertNotEqual(result.returncode, 0)
        combined = ((result.stdout or '') + (result.stderr or '')).lower()
        self.assertIn('non-allowlisted origin remote', combined)

    def test_update_sh_accepts_canonical_origin(self):
        good_repo = self._make_git_repo('https://github.com/daygle/daygle-ai-camera.git')
        # Run a *copy* of update.sh from inside the controlled repo so its
        # APP_DIR (derived from the script's own location) resolves to
        # good_repo -- which sits on a real ``main`` branch -- rather than the
        # surrounding checkout. On pull_request builds actions/checkout leaves
        # the real repo in a detached HEAD, which would trip update.sh's
        # detached-HEAD guard and abort before it ever reaches the origin
        # allowlist this test is asserting. GIT_ALLOW_PROTOCOL=file makes the
        # post-verification ``git fetch origin`` fail instantly offline; every
        # assertion below is already satisfied by the "Origin remote verified"
        # line printed beforehand, so no network is required.
        scripts_dir = Path(good_repo) / 'scripts'
        scripts_dir.mkdir()
        shutil.copy2(
            Path(REPO_DIR) / 'scripts' / 'update.sh',
            scripts_dir / 'update.sh',
        )
        env = _hermetic_git_env()
        env['GIT_ALLOW_PROTOCOL'] = 'file'
        result = subprocess.run(
            ['bash', str(scripts_dir / 'update.sh')],
            cwd=good_repo, capture_output=True, text=True, check=False,
            env=env,
        )
        combined = ((result.stdout or '') + (result.stderr or '')).lower()
        self.assertNotIn('non-allowlisted origin remote', combined)
        self.assertNotIn('refusing to update', combined)
        self.assertIn('origin remote verified', combined)

    def test_install_debian_sh_has_origin_guard_line(self):
        script = Path(REPO_DIR) / 'scripts' / 'install_debian.sh'
        text = script.read_text()
        self.assertIn(
            "EXPECTED_REMOTE_REGEX='github\\.com[:/]daygle/daygle-ai-camera(\\.git)?$'",
            text,
        )
        self.assertIn('refusing to install from non-allowlisted source repo', text)

    def test_update_script_provisions_cloudflared_and_managed_launcher(self):
        script = Path(REPO_DIR) / 'scripts' / 'update.sh'
        text = script.read_text()
        self.assertIn('install_cloudflared.sh', text)
        self.assertIn('20-daygle-launcher.conf', text)
        self.assertIn('ExecStart=${APP_DIR}/.venv/bin/python -m app.server', text)
        self.assertIn('systemctl daemon-reload', text)
        self.assertIn('DAYGLE_CLOUDFLARED_PATH="${APP_DIR}/.venv/bin/cloudflared"', text)
        self.assertIn('run_privileged', text)
        self.assertIn('systemd launcher migration requires root or passwordless sudo', text)

    def test_cloudflared_helper_is_shared_by_installer(self):
        script = Path(REPO_DIR) / 'scripts' / 'install_debian.sh'
        text = script.read_text()
        self.assertIn('scripts/install_cloudflared.sh', text)

    def test_cloudflared_helper_is_invoked_through_bash(self):
        """The helper must work even when an update loses its executable bit."""
        installer_text = (Path(REPO_DIR) / 'scripts' / 'install_debian.sh').read_text()
        updater_text = (Path(REPO_DIR) / 'scripts' / 'update.sh').read_text()
        self.assertIn('bash "${REPO_DIR}/scripts/install_cloudflared.sh"', installer_text)
        self.assertIn('bash "${APP_DIR}/scripts/install_cloudflared.sh"', updater_text)


# H2 ---------------------------------------------------------------------


class AuthServiceAbsoluteExpiryTests(unittest.TestCase):

    PASSWORD = 'Hunter2hunter2!'

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='daygle_test_auth_')
        self.db_path = str(Path(self.tmpdir) / 'auth.sqlite3')
        self.auth = AuthService(
            database_path=self.db_path,
            config={
                'session_timeout_hours': 12,
                'max_login_attempts': 5,
                'lockout_minutes': 15,
                'absolute_session_lifetime_seconds': 14 * 86400,
            },
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _table_info(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute('PRAGMA table_info(' + table + ')').fetchall()
        finally:
            conn.close()

    def test_schema_includes_absolute_expires_at(self):
        cols = [row[1] for row in self._table_info('user_sessions')]
        self.assertIn('absolute_expires_at', cols)

    def test_init_creates_index_for_absolute_expires_at(self):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("PRAGMA index_list('user_sessions')").fetchall()
            index_names = {row[1] for row in rows}
        finally:
            conn.close()
        self.assertTrue(any('absolute_expires' in n.lower() for n in index_names))

    def test_authenticate_binds_absolute_expires_at(self):
        self.auth.create_user('alice', self.PASSWORD, role='admin')
        with mock.patch.object(self.auth, 'verify_password', return_value=True):
            public_user, token, csrf, expires_at = self.auth.authenticate(
                'alice', self.PASSWORD, ip_address='127.0.0.1',
            )
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                'SELECT created_at, absolute_expires_at FROM user_sessions WHERE session_token = ?',
                (token,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        created_at_iso, absolute_expires_at_iso = row
        self.assertIsNotNone(absolute_expires_at_iso)
        dt = datetime.fromisoformat(created_at_iso)
        aet = datetime.fromisoformat(absolute_expires_at_iso)
        delta = aet - dt
        self.assertGreaterEqual(delta.total_seconds(), 14 * 86400 - 5)
        self.assertLessEqual(delta.total_seconds(), 14 * 86400 + 5)

    def test_get_session_rejects_when_absolute_expires_at_has_elapsed(self):
        self.auth.create_user('bob', self.PASSWORD, role='admin')
        with mock.patch.object(self.auth, 'verify_password', return_value=True):
            public_user, token, csrf, expires_at = self.auth.authenticate(
                'bob', self.PASSWORD, ip_address='127.0.0.1',
            )
        conn = sqlite3.connect(self.db_path)
        try:
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
            conn.execute(
                'UPDATE user_sessions SET expires_at = ?, absolute_expires_at = ? WHERE session_token = ?',
                (future, past, token),
            )
            conn.commit()
        finally:
            conn.close()
        result = self.auth.get_session(token)
        self.assertIsNone(result)

    def test_renew_session_does_not_extend_absolute_expires_at(self):
        self.auth.create_user('carol', self.PASSWORD, role='admin')
        with mock.patch.object(self.auth, 'verify_password', return_value=True):
            public_user, token, csrf, expires_at = self.auth.authenticate(
                'carol', self.PASSWORD, ip_address='127.0.0.1',
            )
        conn = sqlite3.connect(self.db_path)
        try:
            before_row = conn.execute(
                'SELECT expires_at, absolute_expires_at FROM user_sessions WHERE session_token = ?',
                (token,),
            ).fetchone()
        finally:
            conn.close()
        with self.auth.connect() as db:
            self.auth._renew_session_if_stale(
                db, token,
                before_row[0],
                datetime.now(timezone.utc),
            )
        conn = sqlite3.connect(self.db_path)
        try:
            after_row = conn.execute(
                'SELECT expires_at, absolute_expires_at FROM user_sessions WHERE session_token = ?',
                (token,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(before_row[1], after_row[1])

    def test_cleanup_expired_sessions_drops_expired_absolute(self):
        self.auth.create_user('dave', self.PASSWORD, role='admin')
        with mock.patch.object(self.auth, 'verify_password', return_value=True):
            public_user, token, csrf, expires_at = self.auth.authenticate(
                'dave', self.PASSWORD, ip_address='127.0.0.1',
            )
        conn = sqlite3.connect(self.db_path)
        try:
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
            conn.execute(
                'UPDATE user_sessions SET expires_at = ?, absolute_expires_at = ? WHERE session_token = ?',
                (future, past, token),
            )
            conn.commit()
        finally:
            conn.close()
        self.auth.cleanup_expired_sessions()
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                'SELECT COUNT(*) FROM user_sessions WHERE session_token = ?',
                (token,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)


# H3 ---------------------------------------------------------------------


class ClearRuntimeMediaDirectorySymlinkTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='daygle_test_clear_')
        self.storage = Path(self.tmpdir) / 'storage'
        self.storage.mkdir()
        self.target_dir = Path(self.tmpdir) / 'sentinel_target'
        self.target_dir.mkdir()
        (self.target_dir / 'must-survive.txt').write_text('precious-1')
        self.target_dir_subdir = self.target_dir / 'sub'
        self.target_dir_subdir.mkdir()
        (self.target_dir_subdir / 'must-survive-2.txt').write_text('precious-2')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _skip_if_symlinks_unavailable(self):
        """Skip symlink tests when the host can't create symlinks.

        On Windows, ``Path.symlink_to`` / ``os.symlink`` require Developer
        Mode or elevated privileges; without them the call raises OSError
        and the security behaviour under test (unlink the link, never follow
        it) can't be exercised. Probing in the test's own tmp dir keeps the
        check hermetic and lets the suite pass on unprivileged Windows
        runners instead of failing on test setup.
        """
        probe = Path(self.tmpdir) / '__symlink_probe__'
        probe_target = Path(self.tmpdir) / '__symlink_probe_target__'
        probe_target.write_text('probe')
        try:
            probe.symlink_to(probe_target)
        except (OSError, NotImplementedError):
            # OSError covers unprivileged Windows (winerror 1314);
            # NotImplementedError covers hosts where symlink isn't
            # implemented at all.
            self.skipTest('symlink creation not permitted on this host '
                          '(Windows Developer Mode / elevation required)')
        finally:
            probe.unlink(missing_ok=True)
            probe_target.unlink(missing_ok=True)

    def test_top_level_symlink_storage_root_unlinked_not_followed(self):
        self._skip_if_symlinks_unavailable()
        link_path = Path(self.tmpdir) / 'storage_link'
        link_path.symlink_to(self.target_dir)
        n = clear_runtime_media_directory(str(link_path))
        self.assertGreaterEqual(n, 1)
        self.assertFalse(link_path.exists())
        self.assertTrue((self.target_dir / 'must-survive.txt').exists())
        self.assertTrue((self.target_dir_subdir / 'must-survive-2.txt').exists())

    def test_inner_symlink_to_dir_unlinked_not_followed(self):
        self._skip_if_symlinks_unavailable()
        sub_link = self.storage / 'sneaky'
        sub_link.symlink_to(self.target_dir)
        n = clear_runtime_media_directory(str(self.storage))
        self.assertGreaterEqual(n, 1)
        self.assertFalse(sub_link.exists())
        self.assertTrue((self.target_dir / 'must-survive.txt').exists())

    def test_inner_symlink_file_unlinked_not_followed(self):
        self._skip_if_symlinks_unavailable()
        target_file = Path(self.tmpdir) / 'real_target.txt'
        target_file.write_text('precious-file')
        sym_file = self.storage / 'fake.txt'
        sym_file.symlink_to(target_file)
        n = clear_runtime_media_directory(str(self.storage))
        self.assertGreaterEqual(n, 1)
        self.assertFalse(sym_file.exists())
        self.assertTrue(target_file.exists())
        self.assertEqual(target_file.read_text(), 'precious-file')

    def test_real_directory_with_real_files_still_wiped(self):
        (self.storage / 'a.txt').write_text('a')
        (self.storage / 'b').mkdir()
        (self.storage / 'b' / 'c.txt').write_text('c')
        n = clear_runtime_media_directory(str(self.storage))
        self.assertGreaterEqual(n, 4)
        self.assertFalse((self.storage / 'a.txt').exists())
        self.assertFalse((self.storage / 'b').exists())


# M1 ---------------------------------------------------------------------


def _request_for(url, headers=None):
    from urllib.parse import urlsplit
    split = urlsplit(url)
    url_obj = SimpleNamespace(
        scheme=split.scheme,
        hostname=split.hostname,
        host=split.hostname or '',
        port=split.port,
        path=split.path,
    )
    hdr = dict(headers or {})
    return SimpleNamespace(headers=hdr, url=url_obj)


class IsSameOriginTests(unittest.TestCase):

    def test_match_origin_only(self):
        req = _request_for('https://app.example.com/api/foo', headers={'Origin': 'https://app.example.com'})
        ok, reason = _is_same_origin(request=req)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, '')

    def test_match_referer_only(self):
        req = _request_for('https://app.example.com/api/foo', headers={'Referer': 'https://app.example.com/dashboard'})
        ok, reason = _is_same_origin(request=req)
        self.assertTrue(ok, reason)

    def test_origin_preferred_over_referer(self):
        req = _request_for(
            'https://app.example.com/api/foo',
            headers={
                'Origin': 'https://app.example.com',
                'Referer': 'https://evil.example.com/steal',
            },
        )
        ok, _ = _is_same_origin(request=req)
        self.assertTrue(ok)

    def test_missing_both_headers_rejected(self):
        req = _request_for('https://app.example.com/api/foo')
        ok, reason = _is_same_origin(request=req)
        self.assertFalse(ok)
        self.assertIn('Missing Origin and Referer', reason)

    def test_different_scheme_rejected(self):
        req = _request_for('https://app.example.com/api/foo', headers={'Origin': 'http://app.example.com'})
        ok, reason = _is_same_origin(request=req)
        self.assertFalse(ok)
        self.assertIn('does not match', reason)

    def test_different_host_rejected(self):
        req = _request_for('https://app.example.com/api/foo', headers={'Origin': 'https://evil.example.com'})
        ok, reason = _is_same_origin(request=req)
        self.assertFalse(ok)

    def test_different_port_rejected(self):
        req = _request_for('https://app.example.com:8443/api/foo', headers={'Origin': 'https://app.example.com:8080'})
        ok, reason = _is_same_origin(request=req)
        self.assertFalse(ok)

    def test_origin_null_rejected(self):
        req = _request_for('https://app.example.com/api/foo', headers={'Origin': 'null'})
        ok, reason = _is_same_origin(request=req)
        self.assertFalse(ok)

    def test_origin_empty_rejected(self):
        req = _request_for('https://app.example.com/api/foo', headers={'Origin': ''})
        ok, reason = _is_same_origin(request=req)
        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
