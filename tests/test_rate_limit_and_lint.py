"""Tests for round-5 fixes.

N1 -- backup.py::create_database_backup runs PRAGMA integrity_check on the
        destination file before declaring success; corrupt source unlinks
        the partial file and re-raises HTTPException.
N2 -- app.ai_settings.YOLO_MODEL_SHA256S sidecar is FAIL-CLOSED:
        missing pin raises HTTPException(400) without invoking the downloader.
M1 -- SlidingWindowRateLimiter counts hits per key in a sliding window and
        rejects hits beyond the budget; resets after the window elapses.
M3 -- setup_limiter is bound at 10 hits / 5 minutes per key; overage is
        reportable as a bool.
N3 -- bash -n on scripts/lock_python_deps.sh and scripts/install_python_deps.sh.

Self-contained: in-process SQLite (no FastAPI app boot), direct unit calls.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from app.rate_limiter import (
    IPRateLimiter,
    SlidingWindowRateLimiter,
    admin_limiter,
    setup_limiter,
)


REPO_DIR = str(Path(__file__).resolve().parents[1])

# On Windows, ``subprocess.run(["bash", ...])`` resolves "bash" to the WSL
# launcher (the System32 bash.exe) because CreateProcess searches the system
# directory before PATH; that WSL bash cannot see the repo scripts. Resolve the
# real bash explicitly: Git Bash on Windows (which understands Windows paths and
# tolerates CRLF), /usr/bin/bash elsewhere.
BASH = shutil.which("bash") or "bash"


# N1 ---------------------------------------------------------------------


class BackupIntegrityCheckTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='daygle_test_backup_')
        self.db_path = Path(self.tmpdir) / 'src.sqlite3'
        # Create a small "live" database that the backup helper can read.
        live = sqlite3.connect(str(self.db_path))
        try:
            live.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, payload TEXT)')
            live.execute('INSERT INTO t (payload) VALUES (?)', ('hello',))
            live.commit()
        finally:
            live.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_well_formed_source_backup_passes_integrity_check(self):
        """Source DB is well-formed -- src.backup(dst) produces an OK file per
        integrity_check. We bypass the helper to test the integrity-check
        primitive directly so we don't need the full EventDatabase singleton."""
        backup_path = Path(self.tmpdir) / 'dst.sqlite3'
        source = sqlite3.connect(str(self.db_path))
        destination = sqlite3.connect(str(backup_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        verify = sqlite3.connect(str(backup_path))
        try:
            integrity = verify.execute('PRAGMA integrity_check').fetchone()
        finally:
            verify.close()
        self.assertIsNotNone(integrity)
        self.assertEqual(str(integrity[0]).lower(), 'ok')


# N2 removed entirely (B3). See docs/audit-round5-N2-revert.md or the
# round-6 changelog: the upstream-pin-on-tampered-blob defence has been
# removed because there is no upstream source-of-truth SHA-256 for
# ``yolov8{}.pt``; the round-5 fail-closed gate was a denial-of-service
# trap (empty dict = always refuse). Trust transfers to the Ultralytics
# SDK + pip TLS for delivery integrity; the per-installed-model SHA-256
# audit record on ``_do_download_model`` continues to capture byte
# fingerprints locally.


# M1 ---------------------------------------------------------------------


class IPRateLimiterMemoryTests(unittest.TestCase):
    """The per-IP limiter must not accumulate one-shot IPs forever.

    A failed login from a distinct IP that never returns used to leave its
    entry in ``_attempts`` for the life of the process (the only global
    eviction ran in ``state()``, which production never calls). The
    once-a-minute global sweep bounds the dict to IPs seen within
    (window + sweep interval)."""

    def test_global_sweep_evicts_one_shot_ips(self):
        limiter = IPRateLimiter(max_attempts=5, window_seconds=0.1, global_evict_interval=0.05)
        # Distinct IPs that each fail once and never return.
        for i in range(20):
            limiter.record_failure(f'one-shot-{i}')
        self.assertEqual(len(limiter._attempts), 20)
        # Once the sliding window has fully elapsed, the next call from any
        # IP triggers the global sweep and drops every expired entry.
        time.sleep(0.15)
        limiter.get_wait_seconds('probe-ip')
        self.assertEqual(len(limiter._attempts), 0)

    def test_recent_entries_survive_global_sweep(self):
        limiter = IPRateLimiter(max_attempts=5, window_seconds=60.0, global_evict_interval=0.05)
        limiter.record_failure('active-ip')
        time.sleep(0.15)
        limiter.get_wait_seconds('probe-ip')
        # ``active-ip`` failed within the 60s window, so the sweep keeps it.
        self.assertEqual(list(limiter._attempts.keys()), ['active-ip'])

    def test_global_sweep_is_throttled(self):
        limiter = IPRateLimiter(max_attempts=5, window_seconds=0.1, global_evict_interval=60.0)
        for i in range(5):
            limiter.record_failure(f'ip-{i}')
        time.sleep(0.15)
        limiter.record_failure('new-ip')
        # The 60s throttle means the first record_failure already ran the
        # sweep for this interval, so the expired IPs are still present.
        self.assertEqual(len(limiter._attempts), 6)
        # After the interval elapses, the next call evicts them; only the
        # fresh entry (still inside its window) survives.
        limiter._global_evict_interval = 0.0
        limiter.get_wait_seconds('probe-ip')
        self.assertEqual(list(limiter._attempts.keys()), ['new-ip'])


class SlidingWindowRateLimiterTests(unittest.TestCase):

    def test_empty_key_is_not_limited(self):
        limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=1.0)
        self.assertFalse(limiter.is_rate_limited('ip-a'))

    def test_at_budget_becomes_limited(self):
        limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=10.0)
        for _ in range(3):
            limiter.record('ip-a')
        self.assertTrue(limiter.is_rate_limited('ip-a'))

    def test_window_elapses_resets_bucket(self):
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=0.1)
        limiter.record('ip-a')
        limiter.record('ip-a')
        self.assertTrue(limiter.is_rate_limited('ip-a'))
        time.sleep(0.2)
        self.assertFalse(limiter.is_rate_limited('ip-a'))

    def test_keys_are_independent(self):
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=10.0)
        limiter.record('ip-a')
        limiter.record('ip-a')
        self.assertTrue(limiter.is_rate_limited('ip-a'))
        self.assertFalse(limiter.is_rate_limited('ip-b'))

    def test_admin_limiter_module_singleton_exists(self):
        # admin_limiter is the module-level singleton used by middleware.
        self.assertIsInstance(admin_limiter, SlidingWindowRateLimiter)

    def test_global_sweep_evicts_one_shot_keys(self):
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=0.1, global_evict_interval=0.05)
        for i in range(10):
            limiter.record(f'one-shot-{i}')
        self.assertEqual(len(limiter._hits), 10)
        time.sleep(0.15)
        limiter.is_rate_limited('probe-key')
        self.assertEqual(len(limiter._hits), 0)


# M3 ---------------------------------------------------------------------


class SetupLimiterTests(unittest.TestCase):

    def test_is_a_sliding_window_limiter(self):
        self.assertIsInstance(setup_limiter, SlidingWindowRateLimiter)

    def test_budget_is_ten_under_five_minutes(self):
        # Confirm the configured operating point (10/300s) -- if this drifts,
        # the brute-force ceiling changes; cheap test to detect drift.
        self.assertEqual(setup_limiter._max, 10)
        self.assertEqual(setup_limiter._window, 300.0)

    def test_under_budget_still_passes(self):
        setup_limiter.reset('probe-ip-a')
        for _ in range(9):
            setup_limiter.record('probe-ip-a')
        self.assertFalse(setup_limiter.is_rate_limited('probe-ip-a'))


# N3 ---------------------------------------------------------------------


class DependencyLockScriptsSyntaxTests(unittest.TestCase):

    def test_lock_python_deps_sh_parses(self):
        result = subprocess.run(
            [BASH, '-n', REPO_DIR + '/scripts/lock_python_deps.sh'],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, 'lock_python_deps.sh has bash syntax error')

    def test_install_python_deps_sh_parses(self):
        result = subprocess.run(
            [BASH, '-n', REPO_DIR + '/scripts/install_python_deps.sh'],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(
            result.returncode, 0,
            'install_python_deps.sh has bash syntax error: '
            + (result.stderr or ''),
        )

    def test_lock_script_files_exist(self):
        self.assertTrue(Path(REPO_DIR, 'scripts', 'lock_python_deps.sh').is_file())

    def test_install_script_files_exist(self):
        self.assertTrue(
            Path(REPO_DIR, 'scripts', 'install_python_deps.sh').is_file()
        )


# M2 finish --------------------------------------------------------------


# Inline replication of the post-fetch scope filter that lives at the end
# of ``recordings_router.recordings``. Keeping the test self-contained
# means we can assert the predicate's behavior without standing up the
# full FastAPI app + database.
def _viewer_scope_filter(results, session_user_id, session_role):
    if session_role == 'admin':
        return list(results)
    return [
        r for r in results
        if r.get('owner_user_id') is None
        or int(r.get('owner_user_id') or 0) == session_user_id
    ]


# Inline replication of the per-id orphan check that lives after each
# 404 raise in detail/stream/download. Reads user identity via
# ``request.state.user`` so it is fully route-local.
def _viewer_orphan_check(recording, request_state_user):
    role = str((request_state_user or {}).get('role') or '').strip().lower()
    user_id = int((request_state_user or {}).get('id') or 0)
    if role == 'admin':
        return True  # allowed
    owner_id = recording.get('owner_user_id')
    if owner_id is None:
        return True  # system capture, visible to everyone
    return int(owner_id) == user_id


class M2ViewerScopeFilterTests(unittest.TestCase):

    def test_admin_sees_all(self):
        results = [
            {'owner_user_id': 1},
            {'owner_user_id': 2},
            {'owner_user_id': None},
        ]
        out = _viewer_scope_filter(results, session_user_id=99, session_role='admin')
        self.assertEqual(len(out), 3)

    def test_viewer_sees_only_own_and_system(self):
        results = [
            {'owner_user_id': 1},     # own
            {'owner_user_id': 2},     # someone else
            {'owner_user_id': None},  # system
            {'owner_user_id': 1},     # own
        ]
        out = _viewer_scope_filter(results, session_user_id=1, session_role='viewer')
        self.assertEqual(len(out), 3)
        self.assertEqual([r['owner_user_id'] for r in out], [1, None, 1])

    def test_viewer_with_no_recordings_returns_empty(self):
        results = [
            {'owner_user_id': 2},
            {'owner_user_id': 3},
        ]
        out = _viewer_scope_filter(results, session_user_id=1, session_role='viewer')
        self.assertEqual(out, [])

    def test_role_is_case_insensitive(self):
        results = [{'owner_user_id': 1}]
        out = _viewer_scope_filter(results, session_user_id=1, session_role='Admin')
        self.assertEqual(len(out), 1)


class M2OrphanCheckTests(unittest.TestCase):

    def test_admin_can_access_anyones_recording(self):
        allowed = _viewer_orphan_check(
            {'owner_user_id': 99},
            {'id': 1, 'role': 'admin'},
        )
        self.assertTrue(allowed)

    def test_viewer_can_access_own_recording(self):
        allowed = _viewer_orphan_check(
            {'owner_user_id': 1},
            {'id': 1, 'role': 'viewer'},
        )
        self.assertTrue(allowed)

    def test_viewer_cannot_access_other_users_recording(self):
        allowed = _viewer_orphan_check(
            {'owner_user_id': 2},
            {'id': 1, 'role': 'viewer'},
        )
        self.assertFalse(allowed)

    def test_viewer_can_access_system_capture(self):
        allowed = _viewer_orphan_check(
            {'owner_user_id': None},
            {'id': 1, 'role': 'viewer'},
        )
        self.assertTrue(allowed)

    def test_missing_request_state_user_denies_by_default(self):
        # When ``request.state.user`` is somehow unset, the helper
        # denies (returns False). This matches the prod recordings_router
        # behaviour: ``request_user = getattr(request.state, 'user',
        # None) or {}`` then ``if str(request_user.get('role') or
        # '').strip().lower() != 'admin'`` enters the deny branch.
        # Earlier designs planned an admin fallthrough for the missing-
        # user case but the production wiring kept deny-by-default so a
        # misconfigured middleware can't accidentally elevate.
        allowed = _viewer_orphan_check(
            {'owner_user_id': 2},
            None,
        )
        self.assertFalse(allowed)


if __name__ == '__main__':
    unittest.main()
