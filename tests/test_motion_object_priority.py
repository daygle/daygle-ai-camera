"""Objects take priority over motion across cycles, not only within one.

Night case this guards: a passing car's headlights sweep a motion zone a second
or more before the car is recognisable. Motion used to confirm and open a
``motion`` event (and alert) first, then the car opened a second event.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from app.motion_object_priority import MotionObjectArbiter, object_zone_keys
from tests.support import _load_app, _m

ZONE = {'id': 'drive', 'name': 'Drive', 'x': 0, 'y': 0, 'width': 1, 'height': 1,
        'monitor_motion': True, 'monitor_objects': True}
MOTION = {'confidence': 0.9, 'zone_id': 'drive', 'zone_name': 'Drive',
          'box': {'x': 0.1, 'y': 0.1, 'width': 0.5, 'height': 0.3}}


def _resolve(arbiter, now, motion=(), object_zones=(), image='frame', has_objects=None, grace=3.0):
    return arbiter.resolve(
        'cam', now=now, grace_seconds=grace, motion_detections=list(motion),
        object_zone_keys=set(object_zones), image=image, image_is_numpy=False,
        has_objects=bool(object_zones) if has_objects is None else has_objects,
    )


# ---------------------------------------------------------------------------
# Arbiter
# ---------------------------------------------------------------------------

def test_motion_only_is_held_then_released_at_its_start():
    arbiter = MotionObjectArbiter()
    first = _resolve(arbiter, 100.0, [MOTION], image='t0-frame')
    assert first.motion_detections == [] and first.held_zones == ['drive']
    assert arbiter.has_held('cam')
    assert _resolve(arbiter, 101.5, [MOTION]).motion_detections == []
    released = _resolve(arbiter, 103.0)  # motion has stopped; grace elapsed
    assert released.motion_detections == [MOTION]
    assert released.event_ts == 100.0 and released.image == 't0-frame'
    assert not arbiter.has_held('cam')


def test_held_motion_is_absorbed_when_an_object_appears():
    arbiter = MotionObjectArbiter()
    _resolve(arbiter, 100.0, [MOTION])
    car_cycle = _resolve(arbiter, 101.5, [MOTION], object_zones={'drive'})
    assert car_cycle.motion_detections == []
    assert car_cycle.attributed_zones == ['drive']
    assert not arbiter.has_held('cam')
    # ...and nothing is released later.
    assert _resolve(arbiter, 104.0).motion_detections == []


def test_trailing_motion_after_an_object_is_attributed_to_it():
    arbiter = MotionObjectArbiter()
    _resolve(arbiter, 100.0, object_zones={'drive'})
    trailing = _resolve(arbiter, 102.0, [MOTION])
    assert trailing.motion_detections == [] and trailing.attributed_zones == ['drive']
    # Well after the object left, motion is held (and later fires) as usual.
    late = _resolve(arbiter, 110.0, [MOTION])
    assert late.held_zones == ['drive']


def test_motion_in_a_zone_without_the_object_is_not_absorbed():
    arbiter = MotionObjectArbiter()
    other = {**MOTION, 'zone_id': 'garden', 'zone_name': 'Garden'}
    _resolve(arbiter, 100.0, [other])
    _resolve(arbiter, 101.0, [other], object_zones={'drive'})
    released = _resolve(arbiter, 103.0, object_zones={'drive'})
    assert released.motion_detections == [other]
    # It rides along with the object cycle's event: the cycle's own time applies.
    assert released.event_ts is None


def test_object_seen_after_an_expired_hold_does_not_absorb_it():
    # Review case: a gap between cycles - motion held at t=0, next cycle at
    # t=10 sees a car. The hold had expired; its motion is released, not
    # silently absorbed by an object that arrived long after.
    arbiter = MotionObjectArbiter()
    _resolve(arbiter, 100.0, [MOTION])
    late = _resolve(arbiter, 110.0, object_zones={'drive'})
    assert late.motion_detections == [MOTION]


def test_zero_grace_is_legacy_immediate_motion():
    arbiter = MotionObjectArbiter()
    result = _resolve(arbiter, 100.0, [MOTION], grace=0)
    assert result.motion_detections == [MOTION]
    assert not arbiter.has_held('cam')


def test_object_zone_keys_matches_objects_to_motion_zones():
    zones = [ZONE, {**ZONE, 'id': 'garden', 'x': 0.8, 'width': 0.2}]
    car = {'label': 'car', 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}
    keys = object_zone_keys(zones, [car], lambda det, zone: det['box']['x'] >= zone['x'])
    assert keys == {'drive'}


# ---------------------------------------------------------------------------
# Live pipeline
# ---------------------------------------------------------------------------

def _pipeline(tmp_path, monkeypatch, *, detector_available=True):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()
    plan = {'motion': [], 'objects': []}

    class FakeDetector:
        backend = 'onnx'
        available = detector_available
        unavailable_reason = None if detector_available else 'model missing'

        def detect_image(self, _image, confidence=None):
            return [dict(obj) for obj in plan['objects']]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    monkeypatch.setattr(mods.live_monitor, 'zone_motion_detections', lambda *_a, **_k: [dict(m) for m in plan['motion']])
    monkeypatch.setattr(mods.live_monitor, 'confirm_motion_detections', lambda _cam, detections, **_kw: detections)
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx'}, main.utc_now())
    main.database.set_setting('objects', {'default_mode': 'any', 'labels': {}, 'still_alerts': {}}, main.utc_now())
    # The shared test config pins the hold off for plumbing tests; this is the
    # behaviour under test, at the shipped default.
    main.database.set_setting('live', {'motion_object_grace_seconds': 3.0}, main.utc_now())
    settings = {'id': 'cam-night', 'name': 'Front Yard', 'detection': {'zones': [ZONE]},
                'recording': {'continuous': False}}
    base = time.time() - 30
    # The debounce windows read wall-clock time; keep it in step with the
    # frames so a multi-second scene plays out as it would live.
    clock = {'now': base}
    monkeypatch.setattr(mods.event_debounce, 'time', SimpleNamespace(time=lambda: clock['now']))

    def cycle(offset, *, motion=False, car=False):
        clock['now'] = base + offset
        plan['motion'] = [motion if isinstance(motion, dict) else MOTION] if motion else []
        plan['objects'] = [{'label': 'car', 'confidence': 0.8,
                            'box': {'x': 200, 'y': 120, 'width': 300, 'height': 160}}] if car else []
        return mods.live_monitor.process_live_stream_alerts(
            b'jpeg', {'timestamp': base + offset, 'width': 1280, 'height': 720},
            settings, enforce_interval=False,
        )

    def labels(event_id):
        return sorted(str(d.get('label')) for d in main.database.get_event(event_id)['detections'])

    return main, cycle, labels, base


def test_headlights_then_car_make_one_car_event(tmp_path, monkeypatch):
    main, cycle, labels, _base = _pipeline(tmp_path, monkeypatch)
    assert cycle(0.0, motion=True) is None   # headlights: held
    assert cycle(0.5, motion=True) is None   # still held
    event_id = cycle(1.5, motion=True, car=True)
    assert event_id is not None
    assert labels(event_id) == ['car']
    # No motion event follows once the hold would have expired.
    assert cycle(4.0) is None
    assert [event['id'] for event in main.database.search_events(limit=10)] == [event_id]


def test_motion_without_an_object_fires_after_the_hold_at_its_start(tmp_path, monkeypatch):
    main, cycle, labels, base = _pipeline(tmp_path, monkeypatch)
    assert cycle(0.0, motion=True) is None
    assert cycle(1.0, motion=True) is None
    event_id = cycle(3.2)
    assert event_id is not None
    assert labels(event_id) == ['motion']
    from datetime import datetime
    created = datetime.fromisoformat(main.database.get_event(event_id)['created_at']).timestamp()
    assert abs(created - base) < 0.01  # stamped when the motion began


def test_motion_beside_a_lingering_car_makes_no_motion_event(tmp_path, monkeypatch):
    # The camera-wide trailing window (5s after a non-motion event) used to
    # expire while the car was still in view: beam spill beside its box then
    # opened a separate motion event mid-scene.
    main, cycle, labels, _base = _pipeline(tmp_path, monkeypatch)
    # Beam spill on the road ahead of the car: same zone, outside the car's
    # box, so the same-cycle overlap filter does not explain it.
    spill = {**MOTION, 'box': {'x': 0.6, 'y': 0.5, 'width': 0.3, 'height': 0.2}}
    event_id = cycle(0.0, car=True)
    assert labels(event_id) == ['car']
    for second in range(1, 9):
        cycle(float(second), motion=spill, car=True)
    cycle(9.0, motion=spill)      # tail-light glow as it leaves
    cycle(13.0)
    assert [event['id'] for event in main.database.search_events(limit=10)] == [event_id]


def test_shipped_default_holds_motion_for_three_seconds():
    from app.config_facades import DEFAULT_LIVE_CONFIG
    from app.motion_object_priority import DEFAULT_GRACE_SECONDS
    assert DEFAULT_LIVE_CONFIG['motion_object_grace_seconds'] == DEFAULT_GRACE_SECONDS == 3.0


def test_held_motion_still_fires_without_an_object_detector(tmp_path, monkeypatch):
    # Review case: with no detector, a quiet cycle used to return before the
    # arbiter, so a short motion pulse stayed held forever. Motion-only rules
    # are documented to keep working without a detector.
    main, cycle, labels, base = _pipeline(tmp_path, monkeypatch, detector_available=False)
    assert cycle(0.0, motion=True) is None
    event_id = cycle(3.5)
    assert event_id is not None
    assert labels(event_id) == ['motion']
