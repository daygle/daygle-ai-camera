"""A motion detection's changed-pixel share is saved and read back.

The recordings and events pages show it instead of the motion confidence,
which is capped at 100% for most movement.
"""
from __future__ import annotations

import sqlite3

from app.database import EventDatabase
from app.detection_state import build_track_from_live_history, record_live_detection_history
from tests.support import add_event


def _motion(fraction):
    detection = {'label': 'motion', 'confidence': 1.0, 'zone_name': 'Yard',
                 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}
    if fraction is not None:
        detection['motion_fraction'] = fraction
    return detection


def test_event_detections_keep_the_motion_fraction(tmp_path):
    db = EventDatabase(str(tmp_path / 'm.sqlite3'))
    event_id = add_event(db, 
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


def test_live_history_track_keeps_the_motion_diagnostics():
    """Why a detection was judged moving rides into the recording's track, so
    a wrong verdict can be traced afterwards (event 62053)."""
    car = {'label': 'car', 'confidence': 0.55, 'box': {'x': 0.4, 'y': 0.03, 'width': 0.05, 'height': 0.02},
           'motion_state': 'moving', 'motion_basis': 'mask', 'track_id': 15, 'track_age': 7,
           'track_displacement': 0.0123456}
    record_live_detection_history('cam-diagnostics', [car], sample_ts=1000.0)
    track = build_track_from_live_history('cam-diagnostics', 999.0, 1001.0)
    assert track is not None
    sample = track[0]['detections'][0]
    assert (sample['motion_basis'], sample['track_age'], sample['track_displacement']) == ('mask', 7, 0.0123)


def test_live_history_track_keeps_the_auto_tracking_decision():
    car = {'label': 'car', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}
    ptz = {'state': 'tracking', 'event': 'move', 'label': 'car', 'track_id': 4, 'error': [0.22, -0.03],
           'move': {'pan': 0.5, 'tilt': 0.0, 'zoom': 0.0, 'seconds': 0.8}, 'resumes_in': None}
    record_live_detection_history('cam-ptz', [car], sample_ts=1000.0, ptz=ptz)
    record_live_detection_history('cam-ptz', [], sample_ts=1000.5, ptz={'state': 'idle'})
    record_live_detection_history('cam-ptz', [], sample_ts=1001.0, ptz={'state': 'idle', 'event': 'lost', 'lost_label': 'car'})
    track = build_track_from_live_history('cam-ptz', 999.0, 1002.0)
    assert track[0]['ptz'] == {k: v for k, v in ptz.items() if v is not None}
    assert 'ptz' not in track[1]
    assert track[2]['ptz'] == {'state': 'idle', 'event': 'lost', 'lost_label': 'car'}
