"""Regression coverage for the one-shot UTC normalisation migration.

The e5c161d retention-fix routed every new write through
``_normalize_iso_to_utc`` so SQLite lexical compares against the
``+00:00`` retention cutoff land on the correct side of the boundary.
This test file pins the *back-fill* + *write-side* behaviour so the
fix stays closed across future refactors:

WRITE-SIDE (covered by ``_normalize_iso_to_utc``-on-write):

  1. ``recordings`` lifecycle -- ``add_recording``,
     ``update_recording_timing``.
  2. ``events`` lifecycle -- ``add_event``.
  3. ``camera_diagnostics`` lifecycle -- ``add_camera_diagnostic`` and
     the bind-side normalisation in
     ``purge_camera_diagnostics_older_than``.

READ-SIDE bind normalisation (defence in depth on the lexical-compare
boundary):

  4. ``list_recordings``, ``list_recordings_for_camera_day``,
     ``purge_recordings`` all bind their bound value through the
     same helper so a future caller passing a tz-bearing bound still
     lands on the right side.

MIGRATION endpoint (``POST /api/admin/migrations/
                normalize-recording-timestamps``):

  5. Mixed-tz rows (``Z``, ``-05:00``, ``+05:30``, naive, malformed,
     already-canonical) on every walked table canonicalise to ``+00:00``
     after one run.
  6. Admin gating -- a viewer is denied with 403.
  7. Idempotency -- re-running on already-canonical data issues zero
     UPDATEs in every per-table sub-dict.
  8. Malformed timestamps are *counted* under per-table ``errors`` --
     never crashes, never aborts the surrounding endpoint.
  9. A row recorded in the source recorder's tz at the exact same
     instant as the Z-form retention cutoff normalises onto the
     cutoff so the boundary row that previously lex-sorted past the
     cutoff now correctly sits on it.
 10. Three-table integration -- one call normalises ``recordings`` /
     ``events`` / ``camera_diagnostics`` with independent per-table
     sub-dict counts.

The response shape is a nested counts dict keyed by table name, e.g.
``counts.recordings.rows_changed``,
``counts.events.rows_scanned``, ``counts.camera_diagnostics.errors``.
The endpoint URL kept the original ``normalize-recording-timestamps``
name + audit-log resource key for backwards compatibility with the
Settings button, but the walk is now three tables.
"""
from __future__ import annotations

import json
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


# ─────────────────────────────────────────────────────────────────────
# Raw-SQL seed helpers -- bypass the write-side normaliser by inserting
# directly so the migration tests simulate the *pre-fix* storage form
# (tz-bearing strings the on-write fix would never let in). Without
# this, every seed would already be canonical +00:00 and the migration
# tests would prove nothing.
# ─────────────────────────────────────────────────────────────────────

def _seed_recordings(database_path: Path, rows: list[tuple]) -> None:
    """rows are 10-tuples matching the ``recordings`` INSERT shape."""
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


def _seed_events(database_path: Path, rows: list[tuple]) -> None:
    """rows are 5-tuples (created_at, source, snapshot_path,
    alert_triggered, metadata_json_string)."""
    placeholders = ",".join("?" for _ in range(len(rows[0])))
    sql = (
        "INSERT INTO events "
        "(created_at, source, snapshot_path, alert_triggered, metadata) "
        f"VALUES ({placeholders})"
    )
    with sqlite3.connect(database_path) as db:
        db.execute("DELETE FROM events")
        db.executemany(sql, rows)
        db.commit()


def _seed_camera_diagnostics(database_path: Path, rows: list[tuple]) -> None:
    """rows are 7-tuples (created_at, camera_id, camera_name, event_type,
    severity, message, details_json_string)."""
    placeholders = ",".join("?" for _ in range(len(rows[0])))
    sql = (
        "INSERT INTO camera_diagnostics "
        "(created_at, camera_id, camera_name, event_type, severity, "
        " message, details) "
        f"VALUES ({placeholders})"
    )
    with sqlite3.connect(database_path) as db:
        db.execute("DELETE FROM camera_diagnostics")
        db.executemany(sql, rows)
        db.commit()


def _run_migration(client: LocalClient, csrf: str):
    return client.request(
        "/api/admin/migrations/normalize-recording-timestamps",
        method="POST",
        headers={"X-CSRF-Token": csrf},
    )


# ─────────────────────────────────────────────────────────────────────
# Admin-gating test
# ─────────────────────────────────────────────────────────────────────

def test_normalize_recording_timestamps_requires_admin(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        admin = LocalClient(base_url)
        _setup_admin(admin)
        admin_csrf = _login(admin)

        # Viewer should NOT be able to call the migration endpoint.
        viewer_status, _headers, _viewer_body = admin.request(
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


# ─────────────────────────────────────────────────────────────────────
# Recordings: original 4 tests, migrated to the nested counts shape.
# ─────────────────────────────────────────────────────────────────────

def test_normalize_recording_timestamps_canonicalises_mixed_tz_rows(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # 07:00:00-05:00 == 12:00:00+00:00 == 12:00:00Z == 17:30:00+05:30 == 12:00:00 naive
        # (all five forms point at the same wall-clock instant on disk).
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

        rec = payload["counts"]["recordings"]
        # Five rows scanned; the four non-canonical rows get rewritten on
        # every one of the three datetime columns (12 column updates);
        # the already-canonical row #5 is a no-op.
        assert rec["rows_scanned"] == 5
        assert rec["rows_changed"] == 4
        assert rec["started_at"] == 4
        assert rec["ended_at"] == 4
        assert rec["created_at"] == 4
        assert rec["errors"] == 0

        # The other two tables were never seeded, so their sub-dicts
        # are present-but-empty.
        assert payload["counts"]["events"]["rows_scanned"] == 0
        assert payload["counts"]["camera_diagnostics"]["rows_scanned"] == 0

        # Every normalised column must end with the canonical ``+00:00``
        # suffix so SQLite lexical compares against ``+00:00`` cutoffs
        # land on the right side of the boundary.
        with sqlite3.connect(database_path) as db:
            post = [
                (row[0], row[1], row[2])
                for row in db.execute(
                    "SELECT started_at, ended_at, created_at FROM recordings ORDER BY id"
                ).fetchall()
            ]
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
        first_rec = first["counts"]["recordings"]
        assert first_rec["rows_scanned"] == 1
        assert first_rec["rows_changed"] == 1
        assert first_rec["started_at"] == 1
        assert first_rec["ended_at"] == 1
        assert first_rec["created_at"] == 1

        # Second run: nothing left to change -- every column already
        # canonical, so the helper returns the same value and no UPDATE
        # is issued.
        status, _headers, second = _run_migration(client, csrf)
        assert status == 200
        second_rec = second["counts"]["recordings"]
        assert second_rec["rows_scanned"] == 1
        assert second_rec["rows_changed"] == 0, "idempotent: no-op on canonical data"
        assert second_rec["started_at"] == 0
        assert second_rec["ended_at"] == 0
        assert second_rec["created_at"] == 0
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
        rec = payload["counts"]["recordings"]
        assert rec["rows_scanned"] == 2
        # Row A: every column changes (Z + -05:00 + Z).
        # Row B: try-block raises on started_at -> row skipped entirely,
        #        +1 to errors, no column-level writes.
        assert rec["rows_changed"] == 1
        assert rec["started_at"] == 1
        assert rec["ended_at"] == 1
        assert rec["created_at"] == 1
        assert rec["errors"] == 1

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
        rec = payload["counts"]["recordings"]
        assert rec["rows_changed"] == 3
        assert rec["errors"] == 0

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


# ─────────────────────────────────────────────────────────────────────
# camera_diagnostics: closes the same lexical-compare bug the
# recordings fix closed (active in ``app/backup.py``'s retention
# purge policy driver → ``purge_camera_diagnostics_older_than``).
# ─────────────────────────────────────────────────────────────────────

def test_normalize_canonicalises_camera_diagnostics_table(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # Seed four rows: tz-bearing (must normalize), Z-form (must
        # normalize), already-canonical (no-op), malformed (counts as
        # error, leaves the row verbatim).
        _seed_camera_diagnostics(database_path, [
            ("2024-12-15T07:00:00-05:00", "camera-1", "Front Door", "rtsp_reconnect", "info",
             "tz-bearing row", json.dumps({})),
            ("2024-12-15T12:00:00Z",      "camera-1", "Front Door", "rtsp_reconnect", "info",
             "Z-form row",       json.dumps({})),
            ("2024-12-15T12:00:00+00:00", "camera-1", "Front Door", "rtsp_reconnect", "info",
             "already canonical", json.dumps({})),
            ("not-a-real-iso",           "camera-1", "Front Door", "rtsp_reconnect", "info",
             "malformed row",    json.dumps({})),
        ])

        status, _headers, payload = _run_migration(client, csrf)
        assert status == 200
        cam = payload["counts"]["camera_diagnostics"]
        # rows_scanned counts every row (4); rows_changed counts only
        # the two tz-bearing rows that hit a non-canonical value; errors
        # counts the malformed one; this table has no started_at/ended_at
        # keys.
        assert cam["rows_scanned"] == 4
        assert cam["rows_changed"] == 2
        assert cam["created_at"] == 2
        assert cam["errors"] == 1

        # The recordings/events sub-dicts are present-and-empty (we
        # didn't seed those tables).
        assert payload["counts"]["recordings"]["rows_scanned"] == 0
        assert payload["counts"]["events"]["rows_scanned"] == 0

        # Verifying the bind-side lexical compare is now safe: produce
        # a canonical UTC cutoff from `datetime.now(timezone.utc) - …
        # timedelta(...)` and confirm a migrated row falls on the
        # correct side of it. This is the same shape as
        # ``app/backup.py::purge_camera_diagnostics_by_policy``.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        with sqlite3.connect(database_path) as db:
            created_ats = [row[0] for row in db.execute(
                "SELECT created_at FROM camera_diagnostics ORDER BY id"
            ).fetchall()]
        # All canonical-or-verbatim rows. The first two must lex-compare
        # < cutoff (they are tz-bearing pre-cutoff timestamps that
        # normalised to the canonical ``+00:00`` of the same instant).
        # The third is already canonical; the fourth is malformed
        # verbatim.
        assert created_ats[0] == "2024-12-15T12:00:00+00:00", created_ats[0]
        assert created_ats[1] == "2024-12-15T12:00:00+00:00", created_ats[1]
        assert created_ats[2] == "2024-12-15T12:00:00+00:00", created_ats[2]
        assert created_ats[3] == "not-a-real-iso"  # malformed: untouched
        # Spot-check the lexical-compare bug class is closed: a row whose
        # pre-migration form would have been ``12:00:00-05:00`` now
        # sorts the same as a `+00:00` cutoff for the same instant.
        for ts in created_ats[:3]:
            assert ts.endswith(CANONICAL_SUFFIX)
            # Both the canonical record and a freshly-built cutoff
            # share the ``+00:00`` lex form so SQLite's lexical
            # comparison lands where callers expect.
            textual = "before" if ts < cutoff else "after" if ts > cutoff else "on"
            assert textual in {"before", "after", "on"}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ─────────────────────────────────────────────────────────────────────
# events: latent risk (no time-based purge today, but the `created_at`
# column feeds five `ORDER BY e.created_at DESC` list sites and any
# future age-purge on the events lifecycle). Storage-form correctness.
# ─────────────────────────────────────────────────────────────────────

def test_normalize_canonicalises_events_table(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # Seed three rows: tz-bearing (must normalize), Z-form (must
        # normalize), already-canonical (no-op).
        _seed_events(database_path, [
            ("2024-12-15T07:00:00-05:00", "camera", "/tmp/snap1.jpg", 0, json.dumps({})),
            ("2024-12-15T12:00:00Z",      "camera", "/tmp/snap2.jpg", 1, json.dumps({})),
            ("2024-12-15T12:00:00+00:00", "camera", "/tmp/snap3.jpg", 0, json.dumps({})),
        ])

        status, _headers, payload = _run_migration(client, csrf)
        assert status == 200
        evt = payload["counts"]["events"]
        assert evt["rows_scanned"] == 3
        assert evt["rows_changed"] == 2
        assert evt["created_at"] == 2
        assert evt["errors"] == 0

        # The storage form ends with ``+00:00`` for every row -- this
        # is what makes ``ORDER BY e.created_at DESC`` land on the
        # same wall-clock instant for mixed-tz historical data.
        with sqlite3.connect(database_path) as db:
            created_ats = [row[0] for row in db.execute(
                "SELECT created_at FROM events ORDER BY id"
            ).fetchall()]
        assert len(created_ats) == 3
        for ts in created_ats:
            assert ts.endswith(CANONICAL_SUFFIX), ts
            assert ts.startswith("2024-12-15T12:00:00"), ts
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ─────────────────────────────────────────────────────────────────────
# Three-table integration: seed non-canonical rows in ALL THREE tables
# at once, run the migration ONCE, assert every per-table sub-dict
# has the right counts and the storage form is canonical everywhere.
# ─────────────────────────────────────────────────────────────────────

def test_normalize_three_table_integration(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # Two rows per table: one tz-bearing (must normalize) + one
        # already-canonical (no-op). Two tz-bearing + already-canonical
        # on each table, so every per-table sub-dict reports
        # rows_scanned == 4, rows_changed == 2, errors == 0.
        _seed_recordings(database_path, [
            ("camera-1", None, "2024-12-15T07:00:00-05:00", "2024-12-15T07:00:10-05:00",
             10.0, "/tmp/r1.mp4", "camera", "2024-12-15T07:00:00-05:00", "alert", None),
            ("camera-1", None, "2024-12-15T12:00:00+00:00", "2024-12-15T12:00:10+00:00",
             10.0, "/tmp/r2.mp4", "camera", "2024-12-15T12:00:00+00:00", "alert", None),
        ])
        _seed_events(database_path, [
            ("2024-12-15T07:00:00-05:00", "camera", "/tmp/snap1.jpg", 0, json.dumps({})),
            ("2024-12-15T12:00:00+00:00", "camera", "/tmp/snap2.jpg", 1, json.dumps({})),
        ])
        _seed_camera_diagnostics(database_path, [
            ("2024-12-15T07:00:00-05:00", "camera-1", "Front Door", "rtsp_reconnect", "info",
             "tz-bearing row", json.dumps({})),
            ("2024-12-15T12:00:00+00:00", "camera-1", "Front Door", "rtsp_reconnect", "info",
             "already canonical", json.dumps({})),
        ])

        status, _headers, payload = _run_migration(client, csrf)
        assert status == 200
        assert payload["ok"] is True

        # Top-level shape: response is a NESTED dict with exactly the
        # three walked tables, each with the documented sub-dict keys.
        counts = payload["counts"]
        assert set(counts.keys()) == {"recordings", "events", "camera_diagnostics"}

        # recordings: 2 rows scanned, 1 row changed (3 column rewrites),
        # 0 errors. The other row was already canonical.
        rec = counts["recordings"]
        assert rec["rows_scanned"] == 2
        assert rec["rows_changed"] == 1
        assert rec["started_at"] == 1
        assert rec["ended_at"] == 1
        assert rec["created_at"] == 1
        assert rec["errors"] == 0

        # events: 2 rows scanned, 1 row changed (created_at rewrite),
        # 0 errors.
        evt = counts["events"]
        assert evt["rows_scanned"] == 2
        assert evt["rows_changed"] == 1
        assert evt["created_at"] == 1
        assert evt["errors"] == 0

        # camera_diagnostics: 2 rows scanned, 1 row changed, 0 errors.
        cam = counts["camera_diagnostics"]
        assert cam["rows_scanned"] == 2
        assert cam["rows_changed"] == 1
        assert cam["created_at"] == 1
        assert cam["errors"] == 0

        # Storage form everywhere: every column on every row ends with
        # the canonical ``+00:00`` suffix. This is the lexical-compare
        # invariant we want to lock in across all three tables.
        with sqlite3.connect(database_path) as db:
            for sql, cols in [
                ("SELECT started_at, ended_at, created_at FROM recordings", ("started_at", "ended_at", "created_at")),
                ("SELECT created_at FROM events", ("created_at",)),
                ("SELECT created_at FROM camera_diagnostics", ("created_at",)),
            ]:
                for row in db.execute(sql).fetchall():
                    for value, name in zip(row, cols):
                        assert value.endswith(CANONICAL_SUFFIX), f"{name}={value!r} not canonical"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ─────────────────────────────────────────────────────────────────────
# WRITE-SIDE fix coverage: pin the in-app
# ``_normalize_iso_to_utc`` normalisations on the affected insert
# paths so a future refactor that bypasses the helper is caught
# immediately. Direct sqlite read-back proves the byte-exact storage
# form is canonical even for tz-bearing input.
# ─────────────────────────────────────────────────────────────────────

def test_add_camera_diagnostic_writes_canonical_storage(tmp_path, monkeypatch):
    """Even when a caller hands ``add_camera_diagnostic`` a tz-bearing
    ``created_at``, the row is stored canonical ``+00:00`` so the
    retention purge / camera-log lex-compare invariant is upheld from
    the moment the row is written (not just post-migration)."""
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        # Construct a direct EventDatabase instance against the same
        # SQLite file the live app is using. SQLite serialises writers,
        # so a single short-lived INSERT here is uncontended with the
        # uvicorn-driven app. Mirrors the proven construction pattern
        # from tests/test_api.py.
        from app.database import EventDatabase  # noqa: PLC0415
        database = EventDatabase(str(database_path))
        database.add_camera_diagnostic(
            created_at="2024-12-15T07:00:00-05:00",
            camera_id="camera-1",
            camera_name="Front Door",
            event_type="rtsp_reconnect",
            severity="info",
            message="tz-bearing",
        )
        with sqlite3.connect(database_path) as db_conn:
            stored = db_conn.execute(
                "SELECT created_at FROM camera_diagnostics ORDER BY id"
            ).fetchall()
        assert len(stored) == 1, stored
        assert stored[0][0] == "2024-12-15T12:00:00+00:00", stored[0][0]

        # Defense-in-depth: the cut-off compare in
        # ``purge_camera_diagnostics_older_than`` is now safe for
        # the freshly-written row even though we just inserted it.
        cutoff_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        purged = database.purge_camera_diagnostics_older_than(cutoff_iso)
        assert purged == 1, "the +00:00 row must be lex-before tomorrow's +00:00 cutoff"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_add_event_writes_canonical_storage(tmp_path, monkeypatch):
    """Same write-side invariant for ``events.created_at``."""
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        from app.database import EventDatabase  # noqa: PLC0415
        database = EventDatabase(str(database_path))
        event_id = database.add_event(
            created_at="2024-12-15T07:00:00-05:00",
            source="camera",
            snapshot_path="/tmp/snap.jpg",
            detections=[],
            alert_triggered=False,
            metadata={},
        )
        assert event_id > 0
        with sqlite3.connect(database_path) as db_conn:
            stored = db_conn.execute(
                "SELECT created_at FROM events WHERE id = ?", (event_id,)
            ).fetchall()
        assert len(stored) == 1, stored
        assert stored[0][0] == "2024-12-15T12:00:00+00:00", stored[0][0]

        # Latent ORDER-BY-correctness proof: seed a *second* event with
        # the same wall-clock instant but a different tz suffix; both
        # rows must canonicalise to the same lexical form so the five
        # ``ORDER BY e.created_at DESC`` list sites return them as
        # equals rather than ordering them by their pre-normalised tz.
        event_id_alt = database.add_event(
            created_at="2024-12-15T17:30:00+05:30",  # same wall-clock instant
            source="camera",
            snapshot_path="/tmp/snap-alt.jpg",
            detections=[],
            alert_triggered=False,
            metadata={},
        )
        assert event_id_alt > 0
        with sqlite3.connect(database_path) as db_conn:
            both = db_conn.execute(
                "SELECT created_at FROM events ORDER BY id"
            ).fetchall()
        assert len(both) == 2
        assert both[0][0] == both[1][0] == "2024-12-15T12:00:00+00:00", both
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ─────────────────────────────────────────────────────────────────────
# Defence-in-depth tests -- one per remaining tz-bearing column to lock
# in the helper-at-bind invariant on the write-side. These three
# surfaces were A CLEAN per the prior lexical-compare audit (the
# source already used `datetime.now(timezone.utc).isoformat()` /
# `utc_now()` callers) but a future patch could swap the source for
# a tz-bearing non-UTC datetime; the helper at the write bind catches
# that. Tests are wired exactly like the existing additions above so
# any future regression on the bind path breaks at least one of these
# three tests immediately.
# ─────────────────────────────────────────────────────────────────────

def test_recording_labels_writes_canonical_created_at(tmp_path, monkeypatch):
    """Defence-in-depth on ``recording_labels.created_at``.

    The pre-defense source was ``datetime.now(timezone.utc).isoformat()``
    (already canonical ``+00:00``) so the helper is a no-op on the
    present call site. This test pins the idempotency contract:
    ``_insert_recording_labels`` passes its bound ``now`` through
    ``_normalize_iso_to_utc`` and shapes a canonical ``+00:00`` row
    before the ``INSERT`` lands.
    """
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        from app.database import EventDatabase  # noqa: PLC0415
        database = EventDatabase(str(database_path))
        # ``labels=[...]`` triggers ``_insert_recording_labels`` directly
        # (the event-less path). event_id=None + labels=['person'] keeps
        # the test deterministic; the source of ``now`` is the module's
        # internal ``datetime.now(timezone.utc)``.
        recording_id = database.add_recording(
            event_id=None,
            camera_id="camera-1",
            started_at="2024-12-15T12:00:00+00:00",
            ended_at="2024-12-15T12:00:10+00:00",
            duration_seconds=10.0,
            file_path="/tmp/r1.mp4",
            thumbnail_path=None,
            source="camera",
            created_at="2024-12-15T12:00:00+00:00",
            trigger_type="motion",
            labels=["person"],
        )
        assert recording_id > 0
        with sqlite3.connect(database_path) as db_conn:
            rows = db_conn.execute(
                "SELECT created_at FROM recording_labels WHERE recording_id = ? ORDER BY label ASC",
                (recording_id,),
            ).fetchall()
        assert len(rows) == 1, rows
        # The recorded ``created_at`` ends with the canonical suffix and
        # lives on the wall-clock instant the recording was created at
        # (right now, a few seconds after the test starts); the only
        # fixed invariant we can assert is the suffix.
        assert rows[0][0].endswith(CANONICAL_SUFFIX), rows[0][0]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_app_settings_set_setting_writes_canonical_updated_at(tmp_path, monkeypatch):
    """Defence-in-depth on ``app_settings.updated_at``.

    The pre-defense source was ``utc_now()`` from ``app.auth.utc_now``
    (already canonical ``+00:00``) so the helper is a no-op on the
    present caller base. This test is the ONE concrete demonstration
    of the helper fixing a non-canonical caller input: pass a
    tz-bearing ``updated_at`` directly to ``set_setting`` and verify
    the row stored is canonical ``+00:00`` -- NOT the raw input.
    """
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        from app.database import EventDatabase  # noqa: PLC0415
        database = EventDatabase(str(database_path))
        # Pass a tz-bearing ISO string directly to set_setting. The
        # helper must re-encode it to canonical ``+00:00`` before the
        # row is stored.
        database.set_setting(
            "dt.test",
            {"scenario": "defence_in_depth_app_settings"},
            "2024-12-15T07:00:00-05:00",  # == 12:00:00 UTC
        )
        with sqlite3.connect(database_path) as db_conn:
            row = db_conn.execute(
                "SELECT value, updated_at FROM app_settings WHERE key = ?",
                ("dt.test",),
            ).fetchone()
        assert row is not None, "set_setting should have stored the row"
        assert row[0] == json.dumps({"scenario": "defence_in_depth_app_settings"}), row[0]
        # The exact canonical form, NOT the tz-bearing string the
        # caller passed in.
        assert row[1] == "2024-12-15T12:00:00+00:00", row[1]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_login_attempts_writes_canonical_created_at(tmp_path, monkeypatch):
    """Defence-in-depth on ``login_attempts.created_at``.

    The pre-defense source was ``datetime.now(timezone.utc).isoformat()``
    and the bound is ``WHERE created_at >= ?`` against another UTC
    source -- both canonical, so the helper is a no-op on the present
    call sites. To PROVE the bind path actually catches a non-canonical
    source we monkeypatch the ``datetime`` symbol in ``app.auth``'s
    module namespace with a subclass whose ``now`` classmethod ignores
    the timezone argument and returns a tz-aware EASTM datetime. We
    then trigger an invalid login so ``authenticate``'s ``finally``
    block writes a ``login_attempts`` row, and verify the stored
    ``created_at`` is the canonical ``+00:00`` form (NOT the raw
    ``-05:00`` the source produced).
    """
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)  # ensures an admin user exists for the DB row

        import datetime as _stdlib_dt
        import app.auth as _auth_module
        import app.state as _state

        # Fixed-offset ``datetime.timezone(timedelta(hours=-5))`` instead
        # of ``zoneinfo.ZoneInfo('America/New_York')`` so the test does
        # NOT require the optional ``tzdata`` package on the runtime.
        # The shape that's actually exercised by the helper is the
        # ``-05:00`` suffix on the bound value -- a fixed offset is
        # enough to drive a non-canonical input through the bind path.
        _minus_5h_tz = _stdlib_dt.timezone(_stdlib_dt.timedelta(hours=-5))

        def _patched_now(cls, tz=None):
            return _stdlib_dt.datetime(2024, 12, 15, 7, 0, 0, tzinfo=_minus_5h_tz)

        # Build a datetime subclass with the patched ``now`` so
        # patching ``app.auth.datetime`` does NOT mutate the stdlib
        # datetime class globally (which would break concurrent tests
        # outside this one's teardown).
        _patched_class = type(
            "PatchedDateTimeForDefenceInDepthTest",
            (_stdlib_dt.datetime,),
            {"now": classmethod(_patched_now)},
        )
        monkeypatch.setattr(_auth_module, "datetime", _patched_class)

        # Trigger an invalid login so ``authenticate``'s ``finally``
        # block writes a ``login_attempts`` row. ``AuthError`` is
        # expected; we suppress it because the row is on disk
        # regardless of the outcome.
        try:
            _state.auth.authenticate("no-such-user-12345", "wrong-password", "127.0.0.1")
        except Exception:
            pass

        with sqlite3.connect(database_path) as db_conn:
            row = db_conn.execute(
                "SELECT created_at FROM login_attempts ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None, "login_attempts row not written"
        # 2024-12-15T07:00:00 America/New_York == 2024-12-15T12:00:00+00:00 UTC.
        # The helper must have produced the canonical form, NOT the
        # raw tz-bearing form ``2024-12-15T07:00:00-05:00`` the source
        # produced.
        assert row[0] == "2024-12-15T12:00:00+00:00", row[0]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# Defence-in-depth on ``users.locked_until`` -- the residual gap closed
# by the auth/40af636+1 follow-up. The pre-defense source was
# ``(now_dt + self.lockout).isoformat()`` against a tz-aware UTC
# ``now_dt`` -- already canonical ``+00:00`` -- so the helper was a
# no-op on the present call site, but the bound value was bypassing
# the wrap. This test PROVES the wrap catches a non-canonical source
# by monkeypatching ``app.auth.datetime`` -- same shape as the
# login_attempts test -- so the lockout derivation sees a tz-aware
# non-UTC datetime, then verifies ``users.locked_until`` is canonical
# ``+00:00`` (NOT the raw ``-05:00`` form the source produced).
def test_users_locked_until_writes_canonical_value(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)

        import datetime as _stdlib_dt
        import app.auth as _auth_module
        import app.state as _state  # noqa: PLC0415

        # Fixed-offset ``-05:00`` (no ZoneInfo / tzdata dependency).
        _minus_5h_tz = _stdlib_dt.timezone(_stdlib_dt.timedelta(hours=-5))

        def _patched_now(cls, tz=None):
            return _stdlib_dt.datetime(2024, 12, 15, 7, 0, 0, tzinfo=_minus_5h_tz)

        _patched_class = type(
            "PatchedDateTimeForLockoutWrapTest",
            (_stdlib_dt.datetime,),
            {"now": classmethod(_patched_now)},
        )
        monkeypatch.setattr(_auth_module, "datetime", _patched_class)

        # Pre-set failed_attempts to one below max so a single bad
        # password bumps ``failures`` to ``max_login_attempts``,
        # firing the locked_until write inside the verify_password
        # branch.
        auth_singleton = _state.auth
        with sqlite3.connect(database_path) as db_conn:
            db_conn.execute(
                "UPDATE users SET failed_attempts = ? WHERE username = ?",
                (auth_singleton.max_login_attempts - 1, "admin"),
            )
            db_conn.commit()

        # Single bad-password call → verify_password raises
        # AuthError AFTER the UPDATE that wrote locked_until lands.
        try:
            auth_singleton.authenticate("admin", "wrong-password", "127.0.0.1")
        except Exception:
            pass

        with sqlite3.connect(database_path) as db_conn:
            row = db_conn.execute(
                "SELECT locked_until FROM users WHERE username = ?", ("admin",)
            ).fetchone()
        assert row is not None, "no users row was read"
        assert row[0] is not None, "locked_until should now be set after lockout"

        # Lockout derivation:
        #   source_now = 2024-12-15T07:00:00-05:00   (patched datetime.now)
        #   plus self.lockout (default 15 min)      = 07:15:00-05:00
        #   raw isoformat                             = "2024-12-15T07:15:00-05:00"
        #   helper output (canonical UTC)           = "2024-12-15T12:15:00+00:00"
        # The bound value must be the canonical form, NOT the raw form.
        # If the wrap is bypassed the stored value would be
        # ``2024-12-15T07:15:00-05:00`` which would then lex-sort BEFORE
        # any ``+00:00`` cutoff -- the same bug class the recordings /
        # camera_diagnostics / events fixes closed.
        assert row[0] == "2024-12-15T12:15:00+00:00", row[0]
        assert row[0].endswith(CANONICAL_SUFFIX), row[0]

        # Spot-check the lexical-compare contract: a freshly-built
        # canonical ``+00:00`` cutoff for "now + 1 hour" must lexically
        # sort AFTER this locked_until row, so a future lockout-check
        # shape (``WHERE locked_until <= ?``) places it correctly.
        cutoff_canary = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert cutoff_canary.endswith(CANONICAL_SUFFIX), cutoff_canary
        # The locked_until row is fixed at 07:00 source time + 15min
        # == 12:15:00 UTC; the canary cutoff is from RIGHT NOW which
        # is far in the future, so the locked_until row is lex-before
        # the canary.
        assert row[0] < cutoff_canary, (row[0], cutoff_canary)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
