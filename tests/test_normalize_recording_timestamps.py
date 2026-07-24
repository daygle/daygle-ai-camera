"""Regression coverage for the one-shot UTC normalisation migration.

The e5c161d retention-fix routed every new write through
``_normalize_iso_to_utc`` so SQLite lexical compares against the
``+00:00`` retention cutoff land on the correct side of the boundary.
This test file pins the *back-fill* behaviour through the
``POST /api/admin/migrations/normalize-recording-timestamps`` admin
endpoint so the historical-data side of the same bug stays closed even
after future refactors:

  1. Mixed-tz rows (``Z``, ``-05:00``, ``+05:30``, naive, malformed,
     already-canonical) all canonicalise to ``+00:00`` after one run.
  2. The endpoint requires admin -- a viewer is denied with 403.
  3. Re-running on already-canonical data is a no-op
     (``rows_changed == 0``), confirming idempotency.
  4. Malformed timestamps are *counted* under ``errors`` -- never
     crashes the migration, never aborts the surrounding endpoint.
  5. A row recorded in the source recorder's tz at the exact same
     instant as the Z-form retention cutoff normalises to the same
     lexical form, so the boundary row that previously lex-sorted
     past the cutoff now correctly sits on it.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Mirror the proven fixtures from tests/test_api.py: _load_app creates a
# fully-isolated transient app/data dir; _server spins up a real
# uvicorn instance bound to a free loopback port; _setup_admin +
# _login establish an authenticated session with CSRF.
from tests.test_api import LocalClient, _load_app, _server, _setup_admin, _login  # noqa: E402


CANONICAL_SUFFIX = "+00:00"


def _seed_recordings(database_path: Path, rows: list[tuple]) -> None:
    """Bypass the write-side normaliser by inserting raw rows directly.

    ``add_recording`` (and friends) would normalise everything to
    canonical +00:00 on the way in -- which is exactly what we DON'T
    want here, because we're simulating pre-fix storage that the
    migration is meant to repair. Insert via raw sqlite so we control
    the byte-exact on-disk shape.
    """
    placeholders = ",".join("?" for _ in range(len(rows[0])))
    sql = (
        "INSERT INTO recordings "
        "(camera_id, event_id, started_at, ended_at, duration_seconds, "
        " file_path, source, created_at, trigger_type, trigger_label) "
        f"VALUES ({placeholders})"
    )
    with sqlite3.connect(database_path) as db:
        db.execute("DELETE FROM recordings")
        db.executemany(sql, rows)
        db.commit()


def _all_recordings(database_path: Path) -> list[tuple]:
    with sqlite3.connect(database_path) as db:
        return [
            (row[0], row[1], row[2])
            for row in db.execute(
                "SELECT started_at, ended_at, created_at FROM recordings ORDER BY id"
            ).fetchall()
        ]


def _run_migration(client: LocalClient, csrf: str):
    return client.request(
        "/api/admin/migrations/normalize-recording-timestamps",
        method="POST",
        headers={"X-CSRF-Token": csrf},
    )


def test_normalize_recording_timestamps_requires_admin(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        admin = LocalClient(base_url)
        _setup_admin(admin)
        admin_csrf = _login(admin)

        # Viewer should NOT be able to call the migration endpoint.
        viewer_status, _headers, viewer_body = admin.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer_admin", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert viewer_status == 200
        viewer = LocalClient(base_url)
        viewer_csrf = _login(viewer, "viewer_admin", "Viewer123!")
        status, _headers, body = viewer.request(
            "/api/admin/migrations/normalize-recording-timestamps",
            method="POST",
            headers={"X-CSRF-Token": viewer_csrf},
        )
        assert status == 403, f"viewer must be denied, got {status}: {body}"
        assert body["detail"] == "Admin access required"

        # Anonymous CSRF request without a session also denied.
        anon = LocalClient(base_url)
        status, _headers, _body = anon.request(
            "/api/admin/migrations/normalize-recording-timestamps",
            method="POST",
            headers={"X-CSRF-Token": "irrelevant"},
        )
        assert status in (401, 403)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_normalize_recording_timestamps_canonicalises_mixed_tz_rows(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # 07:00:00-05:00 == 12:00:00+00:00 == 12:00:00Z == 17:30:00+05:30 == 12:00:00 naive
        # (all five forms point at the same wall-clock instant on disk)
        wall_clock = "12:00:00"
        _seed_recordings(database_path, [
            ("camera-1", None, "2024-12-15T07:00:00-05:00", "2024-12-15T07:00:10-05:00",
             10.0, "/tmp/r1.mp4", "camera", "2024-12-15T07:00:00-05:00", "alert", None),
            ("camera-1", None, "2024-12-15T12:00:00Z",      "2024-12-15T12:00:10Z",
             10.0, "/tmp/r2.mp4", "camera", "2024-12-15T12:00:00Z",      "alert", None),
            ("camera-1", None, "2024-12-15T17:30:00+05:30", "2024-12-15T17:30:10+05:30",
             10.0, "/tmp/r3.mp4", "camera", "2024-12-15T17:30:00+05:30", "alert", None),
            ("camera-1", None, f"2024-12-15T{wall_clock}",   f"2024-12-15T{wall_clock}.000010",
             10.0, "/tmp/r4.mp4", "camera", f"2024-12-15T{wall_clock}",   "alert", None),
            ("camera-1", None, "2024-12-15T12:00:00+00:00", "2024-12-15T12:00:10+00:00",
             10.0, "/tmp/r5.mp4", "camera", "2024-12-15T12:00:00+00:00", "alert", None),
        ])

        status, _headers, payload = _run_migration(client, csrf)
        assert status == 200
        assert payload["ok"] is True

        counts = payload["counts"]
        # Five rows scanned; the four non-canonical rows get rewritten on
        # every one of the three datetime columns (12 column updates); the
        # already-canonical row #5 is a no-op.
        assert counts["rows_scanned"] == 5
        assert counts["rows_changed"] == 4
        assert counts["started_at"] == 4
        assert counts["ended_at"] == 4
        assert counts["created_at"] == 4
        assert counts["errors"] == 0

        # Every normalised column must end with the canonical ``+00:00``
        # suffix so SQLite lexical compares against ``+00:00`` cutoffs
        # land on the right side of the boundary.
        post = _all_recordings(database_path)
        assert len(post) == 5
        for started_at, ended_at, created_at in post:
            assert started_at.endswith(CANONICAL_SUFFIX), started_at
            assert ended_at.endswith(CANONICAL_SUFFIX), ended_at
            assert created_at.endswith(CANONICAL_SUFFIX), created_at

        # All five rows must now point at the same lexical form of the
        # wall-clock instant the seed encoded -- proving the helper
        # actually converted across tz offsets rather than just
        # re-coding the suffix.
        instant_prefix = "2024-12-15T12:00:00"
        for started_at, _ended_at, _created_at in post:
            assert started_at.startswith(instant_prefix), started_at
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_normalize_recording_timestamps_is_idempotent(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        _seed_recordings(database_path, [
            ("camera-1", None, "2024-12-15T07:00:00-05:00", "2024-12-15T07:00:10-05:00",
             10.0, "/tmp/r1.mp4", "camera", "2024-12-15T07:00:00-05:00", "alert", None),
        ])

        # First run: row changes.
        status, _headers, first = _run_migration(client, csrf)
        assert status == 200
        first_counts = first["counts"]
        assert first_counts["rows_scanned"] == 1
        assert first_counts["rows_changed"] == 1
        assert first_counts["started_at"] == 1
        assert first_counts["ended_at"] == 1
        assert first_counts["created_at"] == 1

        # Second run: nothing left to change -- every column already
        # canonical, so the helper returns the same value and no UPDATE
        # is issued.
        status, _headers, second = _run_migration(client, csrf)
        assert status == 200
        second_counts = second["counts"]
        assert second_counts["rows_scanned"] == 1
        assert second_counts["rows_changed"] == 0, "idempotent: no-op on canonical data"
        assert second_counts["started_at"] == 0
        assert second_counts["ended_at"] == 0
        assert second_counts["created_at"] == 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_normalize_recording_timestamps_counts_malformed_rows_without_crashing(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # Row A has a non-canonical ``Z``-form started_at/created_at and
        # a non-canonical ``-05:00`` ended_at -- three columns to rewrite.
        # Row B has a malformed ``not-a-real-iso`` started_at; the helper
        # raises ValueError, the migration try/except skips the WHOLE row
        # (started_at + ended_at + created_at untouched) and counts one
        # error. The endpoint still returns 200.
        _seed_recordings(database_path, [
            ("camera-1", None, "2024-12-15T12:00:00Z",      "2024-12-15T12:00:10-05:00",
             10.0, "/tmp/r1.mp4", "camera", "2024-12-15T12:00:00Z",      "alert", None),
            ("camera-1", None, "not-a-real-iso",           "2024-12-15T12:00:10+00:00",
             10.0, "/tmp/r2.mp4", "camera", "2024-12-15T12:00:00+00:00", "alert", None),
        ])

        status, _headers, payload = _run_migration(client, csrf)
        assert status == 200, "malformed row must not abort the endpoint"
        counts = payload["counts"]
        assert counts["rows_scanned"] == 2
        # Row A: every column changes (Z + -05:00 + Z).
        # Row B: try-block raises on started_at -> row skipped entirely,
        #        +1 to errors, no column-level writes.
        assert counts["rows_changed"] == 1
        assert counts["started_at"] == 1
        assert counts["ended_at"] == 1
        assert counts["created_at"] == 1
        assert counts["errors"] == 1

        # The malformed columns must remain verbatim on disk -- the helper
        # is not silently inventing values; it just refuses to touch them,
        # so the row's ended_at (= canonical) and created_at (= canonical)
        # are NOT partially rewritten.
        with sqlite3.connect(database_path) as db:
            all_cols = db.execute(
                "SELECT started_at, ended_at, created_at FROM recordings ORDER BY id"
            ).fetchall()
        assert all_cols[0] == ("2024-12-15T12:00:00+00:00",
                              "2024-12-15T17:00:10+00:00",
                              "2024-12-15T12:00:00+00:00")
        assert all_cols[1] == ("not-a-real-iso",
                              "2024-12-15T12:00:10+00:00",
                              "2024-12-15T12:00:00+00:00")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_normalize_recording_timestamps_closes_boundary_at_cutoff(tmp_path, monkeypatch):
    """Pin the lexical-compare root cause at the retention boundary.

    Before e5c161d + this migration, a row recorded as
    ``2024-12-15T07:00:00-05:00`` (== 12:00:00 UTC) was stored verbatim.
    A retention cutoff of ``2024-12-15T12:00:00.000Z`` (== 12:00:00 UTC
    too -- same canonical instant) lexically sorted BEFORE the row
    because ``-05:00`` < ``Z`` byte-for-byte. So ``started_at <= cutoff``
    missed it, but the inverse ``started_at <= cutoff`` could include
    rows that had already passed their retention deadline as text but
    not as wall-clock.

    After migration, both fields share the same canonical lex form, so
    a simple ``>=`` / ``<=`` does what callers expect. This test seeds
    that exact pre-fix shape and asserts the post-migration storage
    form on each side of the boundary.
    """
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # Three rows straddling the cutoff at 12:00:00+00:00.
        # Row 1: 06:59:59-05:00 == 11:59:59+00:00 (one minute BEFORE cutoff)
        # Row 2: 07:00:00-05:00 == 12:00:00+00:00 (exactly on the cutoff)
        # Row 3: 07:00:01-05:00 == 12:00:01+00:00 (just after)
        _seed_recordings(database_path, [
            ("camera-1", None, "2024-12-15T06:59:59-05:00", "2024-12-15T07:00:09-05:00",
             10.0, "/tmp/r1.mp4", "camera", "2024-12-15T06:59:59-05:00", "alert", None),
            ("camera-1", None, "2024-12-15T07:00:00-05:00", "2024-12-15T07:00:10-05:00",
             10.0, "/tmp/r2.mp4", "camera", "2024-12-15T07:00:00-05:00", "alert", None),
            ("camera-1", None, "2024-12-15T07:00:01-05:00", "2024-12-15T07:00:11-05:00",
             10.0, "/tmp/r3.mp4", "camera", "2024-12-15T07:00:01-05:00", "alert", None),
        ])

        status, _headers, payload = _run_migration(client, csrf)
        assert status == 200
        counts = payload["counts"]
        assert counts["rows_changed"] == 3
        assert counts["errors"] == 0

        cutoff = "2024-12-15T12:00:00+00:00"
        with sqlite3.connect(database_path) as db:
            rows = db.execute(
                "SELECT started_at FROM recordings ORDER BY id"
            ).fetchall()
        starts = [row[0] for row in rows]

        # Pre-migration these three rows had distinct lexical forms.
        # Post-migration they share the canonical lex shape, so a
        # textual ``>= cutoff`` now matches what callers actually mean.
        assert starts[0] == "2024-12-15T11:59:59+00:00", starts[0]
        assert starts[1] == cutoff, f"row 2 must hit the cutoff exactly, got {starts[1]}"
        assert starts[2] == "2024-12-15T12:00:01+00:00", starts[2]

        # And critically: pre-fix strsort would have ranked the
        # original ``-05:00`` strings *below* the cutoff; the fix
        # re-ranks them onto the correct side.
        for started in starts:
            textual_sort_compare = (
                "before" if started < cutoff else "after" if started > cutoff else "on"
            )
            if started == "2024-12-15T11:59:59+00:00":
                assert textual_sort_compare == "before"
            elif started == cutoff:
                assert textual_sort_compare == "on"
            elif started == "2024-12-15T12:00:01+00:00":
                assert textual_sort_compare == "after"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
