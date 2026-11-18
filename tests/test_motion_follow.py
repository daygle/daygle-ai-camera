"""A fired motion zone keeps its box on the subject under the trigger.

Recording case this guards: a box truck drove through a motion zone. Its plain
white panel barely changes between frames and it went behind a bush, so the
zone cleared its trigger on one cycle only. The clip's track held a single
motion box ("Event: Single Detection") and playback dropped it after a second
while the truck kept moving.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

from app import motion_follow
from app.zone_detection import zone_motion_follow_detections
from tests.support import _load_app, _m

ZONE = {'id': 'street', 'name': 'Street', 'enabled': True, 'monitor_motion': True,
        'x': 0, 'y': 0, 'width': 1, 'height': 1}
SETTINGS = {'detection': {'zones': [ZONE]}}


def _mask_with_blob(x):
    mask = np.zeros((240, 320), dtype=bool)
    mask[100:120, x:x + 20] = True  # 400 of 76800 px ~= 0.52%
    return mask


def _levels(fraction, trigger=0.02):
    return [{'zone_id': 'street', 'zone_name': 'Street', 'fraction': fraction, 'trigger': trigger}]


def test_follow_box_tracks_the_blob_under_the_trigger():
    boxes = []
    for x in (20, 140, 260):
        result = zone_motion_follow_detections(
            SETTINGS, _mask_with_blob(x), _levels(0.0052), {'street'}, min_trigger_ratio=0.25,
        )
        assert len(result) == 1 and result[0]['motion_follow'] is True
        boxes.append(result[0]['box']['x'])
    assert boxes == sorted(boxes) and boxes[0] < 0.1 < boxes[1] < 0.5 < boxes[2]


def test_follow_skips_quiet_unfollowed_and_over_trigger_zones():
    mask = _mask_with_blob(100)
    follow = lambda levels, zones={'street'}: zone_motion_follow_detections(  # noqa: E731
        SETTINGS, mask, levels, zones, min_trigger_ratio=0.25,
    )
    assert follow(_levels(0.004)) == []          # under a quarter of the trigger: noise
    assert follow(_levels(0.03)) == []           # over the trigger: it has its own detection
    assert follow(_levels(0.0052), set()) == []  # zone has not fired recently
    assert follow(_levels(None)) == []           # no usable mask this cycle


def test_follow_window_expires_and_is_per_camera():
    motion_follow.forget_camera('cam-a')
    motion_follow.note_fired('cam-a', ['street'], 100.0)
    assert motion_follow.following_zones('cam-a', 105.0) == {'street'}
    assert motion_follow.following_zones('cam-b', 105.0) == set()
    assert motion_follow.following_zones('cam-a', 100.0 + motion_follow.FOLLOW_SECONDS + 0.1) == set()
    motion_follow.note_fired('cam-a', ['street'], 200.0)
    motion_follow.forget_camera('cam-a')
    assert motion_follow.following_zones('cam-a', 201.0) == set()


def test_live_pipeline_saves_follow_boxes_to_the_track_without_new_events(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    from app.detection_state import build_track_from_live_history
    mods = _m()
    plan = {'motion': False, 'follow_x': None}

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image, confidence=None):
            return []

    def zone_motion(_settings, _confidence, *, levels=None, **_kwargs):
        if not plan['motion']:
            return []
        return [{'confidence': 1.0, 'motion_fraction': 0.03, 'zone_id': 'street', 'zone_name': 'Street',
                 'box': {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.3}}]

    def follow(_settings, _mask, _levels, zone_ids, **_kwargs):
        if plan['follow_x'] is None or 'street' not in zone_ids:
            return []
        return [{'confidence': 0.4, 'motion_fraction': 0.008, 'zone_id': 'street', 'zone_name': 'Street',
                 'box': {'x': plan['follow_x'], 'y': 0.2, 'width': 0.2, 'height': 0.2}, 'motion_follow': True}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    monkeypatch.setattr(mods.live_monitor, 'zone_motion_detections', zone_motion)
    monkeypatch.setattr(mods.live_monitor, 'zone_motion_follow_detections', follow)
    monkeypatch.setattr(mods.live_monitor, 'confirm_motion_detections', lambda _cam, detections, **_kw: detections)
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx'}, main.utc_now())
    main.database.set_setting('objects', {'default_mode': 'any', 'labels': {}, 'still_alerts': {}}, main.utc_now())
    main.database.set_setting('live', {'motion_object_grace_seconds': 0}, main.utc_now())
    camera_id = 'cam-truck'
    motion_follow.forget_camera(camera_id)
    settings = {'id': camera_id, 'name': 'Front', 'detection': {'zones': [ZONE]},
                'recording': {'continuous': False}}
    base = time.time() - 30
    clock = {'now': base}
    monkeypatch.setattr(mods.event_debounce, 'time', SimpleNamespace(time=lambda: clock['now']))

    def cycle(offset, *, motion=False, follow_x=None):
        plan['motion'], plan['follow_x'] = motion, follow_x
        clock['now'] = base + offset
        return mods.live_monitor.process_live_stream_alerts(
            b'jpeg', {'timestamp': base + offset, 'width': 1280, 'height': 720},
            settings, enforce_interval=False,
        )

    # Before the zone ever fired, sub-trigger motion saves nothing.
    cycle(-1.0, follow_x=0.05)
    event_id = cycle(0.0, motion=True)
    assert event_id is not None
    for offset, x in ((0.5, 0.3), (1.0, 0.5), (1.5, 0.7)):
        assert cycle(offset, follow_x=x) is None
    # Past the follow window, sub-trigger motion is not saved again.
    cycle(motion_follow.FOLLOW_SECONDS + 1.0, follow_x=0.9)

    track = build_track_from_live_history(camera_id, base - 2, base + motion_follow.FOLLOW_SECONDS + 2)
    xs = [d['box']['x'] for sample in track for d in sample['detections'] if d.get('label') == 'motion']
    assert xs == [0.1, 0.3, 0.5, 0.7]
    assert [event['id'] for event in main.database.search_events(limit=10)] == [event_id]
