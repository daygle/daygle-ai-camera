"""Regression guard for the dashboard's Motion Detections stat card.

The dashboard used to compute this number in the browser by filtering the
fully drained event list (``events.filter(isMotionOnlyEvent).length``), which
meant the page had to fetch the entire history before one stat card could be
correct. The count now comes from ``db.stats()`` alongside the object and sound
cards, so the feed can paint from its first page.

The SQL must agree with the frontend's ``isMotionOnlyEvent()``: a motion-only
event is a non-sound event with at least one detection where EVERY detection
label sits inside GENERIC_TRIGGER_LABELS. Events with no detections, or whose
labels are all blank, are under-recorded samples and must not count.
"""

from app.database import EventDatabase


def _add_event(database, detections, *, source='camera', created_at='2026-08-01T10:00:00+00:00'):
    return database.add_event(
        created_at=created_at,
        source=source,
        snapshot_path=None,
        detections=detections,
        alert_triggered=False,
    )


def _det(label, confidence=0.9):
    return {'label': label, 'confidence': confidence, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}


def test_motion_stats_count_only_pure_motion_events(tmp_path):
    database = EventDatabase(str(tmp_path / 'motion-stats.sqlite3'))

    # Pure motion: one generic label.
    _add_event(database, [_det('motion')])
    # Pure motion: several generic labels together still count.
    _add_event(database, [_det('motion'), _det('human')])
    # Case / whitespace insensitive, like the JS normaliser.
    _add_event(database, [_det('  MOTION  ')])
    # The recording-mode placeholders are generic too.
    _add_event(database, [_det('object')])
    _add_event(database, [_det('continuous')])

    # A real object label present -> not motion-only.
    _add_event(database, [_det('motion'), _det('person')])
    _add_event(database, [_det('person')])

    # Sound events never count, even with a generic-looking label.
    _add_event(database, [_det('dog_bark')], source='sound')

    # No detections at all: an under-recorded sample, not a motion trigger.
    _add_event(database, [])
    # Blank-only labels are equally under-recorded.
    _add_event(database, [_det(''), _det('   ')])

    assert database.stats()['motion_detection_events'] == 5


def test_motion_stats_respect_dismissed_and_since(tmp_path):
    database = EventDatabase(str(tmp_path / 'motion-bounds.sqlite3'))

    _add_event(database, [_det('motion')], created_at='2026-08-01T10:00:00+00:00')
    dismissed = _add_event(database, [_det('motion')], created_at='2026-08-01T11:00:00+00:00')
    _add_event(database, [_det('motion')], created_at='2026-08-02T11:00:00+00:00')

    database.dismiss_event(dismissed)

    # The dismissed row drops out of every card, not just this one.
    assert database.stats()['motion_detection_events'] == 2

    # since bounds the same way it bounds total_events.
    assert database.stats(since='2026-08-02')['motion_detection_events'] == 1
    assert database.stats(since='2026-08-03')['motion_detection_events'] == 0


def test_motion_stats_is_advertised_on_the_stats_payload(tmp_path):
    database = EventDatabase(str(tmp_path / 'motion-payload.sqlite3'))
    _add_event(database, [_det('motion')])

    payload = database.stats()
    # The dashboard reads this key directly; a rename would silently zero the card.
    assert 'motion_detection_events' in payload
    assert payload['motion_detection_events'] == 1
