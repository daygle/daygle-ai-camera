"""Tests for the recording <-> events relationship (recording : events = 1:many).

A recording spans many events, so ``events.recording_id`` links each detection
event to the clip it belongs to. These tests exercise, at the DB layer:

* ``add_event(recording_id=...)`` / ``set_event_recording`` linkage,
* ``list_recordings`` exposing every linked event under ``recording['events']``,
* ``get_event`` exposing ``recording_id`` and the 1:1 ``alert``,
* the non-destructive ``backfill_event_recording_links`` migration.
"""
from __future__ import annotations


def _det(label, confidence=0.9):
    return {'label': label, 'confidence': confidence, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}


def _add_recording(db, event_id, tmp_path, *, camera_id='front', label='person',
                   started='2026-06-06T00:00:00+00:00', ended='2026-06-06T00:00:20+00:00'):
    return db.add_recording(
        event_id=event_id,
        camera_id=camera_id,
        started_at=started,
        ended_at=ended,
        duration_seconds=20,
        file_path=str(tmp_path / f'{label}.mp4'),
        thumbnail_path=None,
        source='rtsp',
        created_at=started,
        trigger_type='object',
        trigger_label=label,
    )


def test_recording_lists_all_linked_events(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    e1 = db.add_event(created_at='2026-06-06T00:00:00+00:00', source='rtsp', snapshot_path=None,
                      detections=[_det('person')])
    rid = _add_recording(db, e1, tmp_path)
    # A second object appears mid-clip: a fresh event linked to the SAME clip.
    e2 = db.add_event(created_at='2026-06-06T00:00:05+00:00', source='rtsp', snapshot_path=None,
                      detections=[_det('dog', 0.8)], recording_id=rid)
    assert db.set_event_recording(e1, rid) is True

    recordings = db.list_recordings()
    assert len(recordings) == 1
    recording = recordings[0]
    assert sorted(e['id'] for e in recording['events']) == sorted([e1, e2])
    # Each linked event carries its own detections.
    labels = {d['label'] for e in recording['events'] for d in e['detections']}
    assert {'person', 'dog'} <= labels
    # get_recording (single) exposes the same events list.
    assert sorted(e['id'] for e in db.get_recording(rid)['events']) == sorted([e1, e2])


def test_event_payload_includes_recording_id_and_single_alert(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    e1 = db.add_event(created_at='2026-06-06T00:00:00+00:00', source='rtsp', snapshot_path=None,
                      detections=[_det('person')])
    rid = _add_recording(db, e1, tmp_path)
    db.set_event_recording(e1, rid)
    db.add_alert('2026-06-06T00:00:00+00:00', 'zone__obj__person', e1, 'person', 0.9, 'person matched', recording_id=rid)

    ev = db.get_event(e1)
    assert ev['recording_id'] == rid
    assert ev['alert'] is not None
    assert ev['alert']['label'] == 'person'

    # An event with no alert reports alert=None but still carries its recording.
    e2 = db.add_event(created_at='2026-06-06T00:00:05+00:00', source='rtsp', snapshot_path=None,
                      detections=[_det('dog', 0.7)], recording_id=rid)
    ev2 = db.get_event(e2)
    assert ev2['recording_id'] == rid
    assert ev2['alert'] is None


def test_deleting_recording_nulls_event_link(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    e1 = db.add_event(created_at='2026-06-06T00:00:00+00:00', source='rtsp', snapshot_path=None,
                      detections=[_det('person')])
    rid = _add_recording(db, e1, tmp_path)
    db.set_event_recording(e1, rid)
    assert db.get_event(e1)['recording_id'] == rid

    db.delete_recording(rid)
    # Event survives (it is not cascade-deleted) but its link is cleared.
    ev = db.get_event(e1)
    assert ev is not None
    assert ev['recording_id'] is None


def test_backfill_links_events_via_primary_alert_and_time_window(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    # Primary event of the recording.
    e_primary = db.add_event(created_at='2026-06-06T00:00:00+00:00', source='rtsp', snapshot_path=None,
                             detections=[_det('person')], metadata={'camera_id': 'front'})
    rid = _add_recording(db, e_primary, tmp_path)
    # Linked only via an alert row.
    e_alert = db.add_event(created_at='2026-06-06T00:00:03+00:00', source='rtsp', snapshot_path=None,
                           detections=[_det('car', 0.6)], metadata={'camera_id': 'front'})
    db.add_alert('2026-06-06T00:00:03+00:00', 'zone__obj__car', e_alert, 'car', 0.6, 'car matched', recording_id=rid)
    # Linked only by time-window overlap on the same camera.
    e_window = db.add_event(created_at='2026-06-06T00:00:10+00:00', source='rtsp', snapshot_path=None,
                            detections=[_det('dog', 0.7)], metadata={'camera_id': 'front'})
    # An unrelated event on a different camera must NOT be linked.
    e_other = db.add_event(created_at='2026-06-06T00:00:10+00:00', source='rtsp', snapshot_path=None,
                           detections=[_det('cat', 0.7)], metadata={'camera_id': 'back'})

    # Simulate the pre-link schema: clear every events.recording_id, then migrate.
    with db.connect() as conn:
        conn.execute('UPDATE events SET recording_id = NULL')
    linked = db.backfill_event_recording_links()
    assert linked >= 3

    assert db.get_event(e_primary)['recording_id'] == rid
    assert db.get_event(e_alert)['recording_id'] == rid
    assert db.get_event(e_window)['recording_id'] == rid
    assert db.get_event(e_other)['recording_id'] is None

    # Re-aggregation: the clip's labels now reflect every linked event's objects.
    recording = db.get_recording(rid)
    assert {'person', 'car', 'dog'} <= set(recording['labels'])
    assert 'cat' not in recording['labels']
