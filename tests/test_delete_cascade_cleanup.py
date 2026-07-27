"""Regression tests for the manual delete-cascade cleanup.

SQLite does not enforce foreign keys unless ``PRAGMA foreign_keys=ON`` is set
per connection (it is not, and enabling it globally is unsafe here because the
``recordings`` table cross-references the auth-owned ``users`` table). The
schema's declared ``ON DELETE CASCADE`` / ``ON DELETE SET NULL`` actions were
therefore inert, so deleting a recording/event orphaned its child rows -- and
an orphaned ``alert_history`` row (its event deleted) still surfaced in
``/api/alerts``. The DB delete paths now mirror those referential actions
explicitly, and ``init()`` cleans up rows orphaned before the fix.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.database import EventDatabase  # noqa: E402


def _seed(db: EventDatabase) -> tuple[int, int]:
    event_id = db.add_event(
        created_at='2026-01-01T00:00:00+00:00', source='camera', snapshot_path=None,
        detections=[{'label': 'dog', 'confidence': 0.9,
                     'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}],
    )
    recording_id = db.add_recording(
        event_id=event_id, camera_id='cam-1', started_at='2026-01-01T00:00:00+00:00',
        ended_at='2026-01-01T00:00:05+00:00', duration_seconds=5, file_path='/tmp/x.mp4',
        thumbnail_path=None, source='camera', created_at='2026-01-01T00:00:00+00:00', labels=['dog'],
    )
    db.add_alert(
        created_at='2026-01-01T00:00:01+00:00', rule_name='r', event_id=event_id,
        label='dog', confidence=0.9, message='m', recording_id=recording_id,
    )
    return event_id, recording_id


def _counts(path: str) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        return {
            't': con.execute("SELECT COUNT(*) FROM recording_labels").fetchone()[0],
            'det': con.execute("SELECT COUNT(*) FROM detections").fetchone()[0],
            'ah': con.execute("SELECT COUNT(*) FROM alert_history").fetchone()[0],
            'rec': con.execute("SELECT COUNT(*) FROM recordings").fetchone()[0],
        }
    finally:
        con.close()


def test_delete_recording_removes_labels_and_detaches_alerts(tmp_path):
    p = str(tmp_path / 'db.sqlite3')
    db = EventDatabase(p)
    _event_id, recording_id = _seed(db)
    assert _counts(p)['t'] == 1
    db.delete_recording(recording_id)
    after = _counts(p)
    assert after['t'] == 0, 'recording_labels should be removed with the recording'
    assert after['ah'] == 1, 'the alert row survives (only its recording link is detached)'
    con = sqlite3.connect(p)
    try:
        assert con.execute("SELECT recording_id FROM alert_history").fetchone()[0] is None
    finally:
        con.close()


def test_delete_event_removes_children_and_alert_no_longer_listed(tmp_path):
    p = str(tmp_path / 'db.sqlite3')
    db = EventDatabase(p)
    event_id, _recording_id = _seed(db)
    db.delete_event(event_id)
    after = _counts(p)
    assert after['det'] == 0, 'detections should be removed with the event'
    assert after['ah'] == 0, 'alert_history should be removed with the event'
    con = sqlite3.connect(p)
    try:
        assert con.execute(
            "SELECT event_id FROM recordings WHERE id IS NOT NULL"
        ).fetchone()[0] is None, 'the recording should be detached from the deleted event'
    finally:
        con.close()
    # The user-visible symptom: a deleted event's alert must not linger in the list.
    assert db.alerts() == []


def test_delete_all_recordings_and_events_clear_children(tmp_path):
    p = str(tmp_path / 'db.sqlite3')
    db = EventDatabase(p)
    _seed(db)
    db.delete_all_recordings()
    assert _counts(p)['t'] == 0
    db.delete_all_events()
    after = _counts(p)
    assert after['det'] == 0 and after['ah'] == 0


def test_init_cleans_up_preexisting_orphans(tmp_path):
    """A database carrying orphans from before the fix is made consistent on the
    next ``init()`` (simulating an upgrade of a deployed install)."""
    p = str(tmp_path / 'db.sqlite3')
    EventDatabase(p)  # create schema
    # Inject orphans directly (FK enforcement is off, so this is accepted).
    con = sqlite3.connect(p)
    try:
        con.execute(
            "INSERT INTO recording_labels (recording_id, label, source, created_at) "
            "VALUES (999, 'ghost', 'detection', '2026-01-01T00:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO detections (event_id, label, confidence, x, y, width, height) "
            "VALUES (999, 'ghost', 0.5, 0, 0, 0, 0)"
        )
        con.commit()
    finally:
        con.close()
    assert _counts(p)['t'] == 1 and _counts(p)['det'] == 1
    # Re-open: init() runs its one-time orphan cleanup.
    EventDatabase(p)
    after = _counts(p)
    assert after['t'] == 0, 'orphaned recording_labels should be cleaned on init'
    assert after['det'] == 0, 'orphaned detections should be cleaned on init'
