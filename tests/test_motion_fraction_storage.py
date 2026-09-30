"""A motion detection's changed-pixel share is saved and read back.

The recordings and events pages show it instead of the motion confidence,
which is capped at 100% for most movement.
"""
from __future__ import annotations

import sqlite3

from app.database import EventDatabase
from app.detection_state import build_track_from_live_history, record_live_detection_history


def _motion(fraction):
    detection = {'label': 'motion', 'confidence': 1.0, 'zone_name': 'Yard',
                 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}
    if fraction is not None:
        detection['motion_fraction'] = fraction
    return detection


def test_event_detections_keep_the_motion_fraction(tmp_path):
    db = EventDatabase(str(tmp_path / 'm.sqlite3'))
    event_id = db.add_event(
        created_at='2026-09-30T00:00:00+00:00', source='rtsp', snapshot_path=None,
        detections=[_motion(0.0423), _motion(None), {**_motion(None), 'label': 'person', 'confidence': 0.9}],
    )
    event_with_alerts = db.add_event_with_alerts(
        created_at='2026-09-30T00:00:01+00:00', source='rtsp', snapshot_path=None, thumbnail_path=None,
        detections=[_motion(1.7)], alerts=[], alert_triggered=False, metadata={},
    )
    fractions = sorted(
        (row['motion_fraction'] for row in db.get_event(event_id)['detections']),
        key=lambda value: (value is None, value),
    )
    assert fractions == [0.0423, None, None]
    assert [row['motion_fraction'] for row in db.get_event(event_with_alerts)['detections']] == [1.0]


def test_old_detections_table_gains_the_column(tmp_path):
    path = tmp_path / 'old.sqlite3'
    EventDatabase(str(path))
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(detections)')}
    assert 'motion_fraction' in columns
    # Opening an already-migrated database again is a no-op.
    EventDatabase(str(path))


def test_live_history_track_keeps_the_motion_fraction():
    record_live_detection_history('cam-fraction', [_motion(0.05), _motion(None)], sample_ts=1000.0)
    track = build_track_from_live_history('cam-fraction', 999.0, 1001.0)
    assert track is not None
    assert [d.get('motion_fraction') for d in track[0]['detections']] == [0.05, None]
