"""Face pipeline: tracking across face-pass gaps, identity state, alert delivery.

The face model runs on its own, slower clock (Face Detection Interval, 1s by
default) than object detection (0.5s), so most detection cycles carry no face
even while one is on screen. These tests pin what must survive those gaps:

* the face's track id (the tracker must not age a face track on a cycle the
  face model skipped);
* the per-track identity state -- one stranger alert per track, one Review
  capture per track, the recognition cache;
* a face alert raised a cycle after the person's event (recognition usually
  lands late), which the duplicate-event gate used to swallow.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

import app.state as state
from app import face_identity
from app.object_tracking import live_track_ids, update_object_tracks

FACE_BOX = {'x': 0.4, 'y': 0.2, 'width': 0.1, 'height': 0.15}


def _face(**extra):
    return {'label': 'face', 'confidence': 0.9, 'box': dict(FACE_BOX), **extra}


def _reset_tracks(camera_id):
    with state._object_tracks_lock:
        state._object_tracks.pop(camera_id, None)


def test_face_track_survives_cycles_the_face_model_skipped():
    camera = 'cam-face-gap'
    _reset_tracks(camera)
    [first] = update_object_tracks(camera, [_face()])
    # Ten cycles without a face pass (e.g. a 5s face interval at 0.5s cycles).
    for _ in range(10):
        update_object_tracks(camera, [], hold_labels={'face'})
    assert live_track_ids(camera, 'face') == {first['track_id']}
    [again] = update_object_tracks(camera, [_face()])
    assert again['track_id'] == first['track_id']
    # Cycles where the face pass DID run and saw nothing still age it out.
    for _ in range(6):
        update_object_tracks(camera, [])
    assert live_track_ids(camera, 'face') == set()
    _reset_tracks(camera)


def test_held_labels_only_pause_their_own_tracks():
    camera = 'cam-face-hold-scope'
    _reset_tracks(camera)
    update_object_tracks(camera, [_face(), {'label': 'car', 'confidence': 0.9, 'box': {'x': 0.0, 'y': 0.6, 'width': 0.3, 'height': 0.3}}])
    for _ in range(6):
        update_object_tracks(camera, [], hold_labels={'face'})
    assert live_track_ids(camera, 'face') and not live_track_ids(camera, 'car')
    _reset_tracks(camera)


class _Stranger:
    """A recognition service that never matches anyone."""

    available = True
    matcher_generation = 1
    auto_enrich_enabled = False
    model_id = 'test-model'

    def __init__(self):
        self.recognitions = 0

    def recognizable(self, _crop):
        return True

    def recognize(self, _crop):
        self.recognitions += 1
        return None


def test_lingering_stranger_alerts_and_is_captured_once(monkeypatch):
    camera = 'cam-face-stranger'
    _reset_tracks(camera)
    face_identity.reset_camera_identities(camera)
    service = _Stranger()
    captures = []
    monkeypatch.setattr(face_identity, 'get_face_recognition_service', lambda: service)
    monkeypatch.setattr(face_identity, 'effective_face_detection_rules', lambda: {'rules': [{'id': '_unknown', 'enabled': True}]})
    import app.postprocess_pool as pool
    monkeypatch.setattr(pool, 'enrichment_pool', lambda: SimpleNamespace(submit=lambda _fn, *args, **_kw: captures.append(args[1]) or True))
    monkeypatch.setattr(state, 'database', None)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    alerts = []
    for cycle in range(12):
        face_pass = cycle % 2 == 0  # 1s face interval at 0.5s detection cycles
        detections = update_object_tracks(camera, [_face()] if face_pass else [], hold_labels=set() if face_pass else {'face'})
        detections = face_identity.annotate_face_identities(camera, detections, frame)
        alerts.extend(face_identity.unknown_face_alerts(camera, detections))
    assert len(alerts) == 1, 'one stranger alert per track, not one per face pass'
    assert len(captures) == 1, 'one Review capture per track'
    _reset_tracks(camera)
    face_identity.reset_camera_identities(camera)


def test_identity_cache_is_reused_across_face_less_cycles(monkeypatch):
    camera = 'cam-face-known'
    _reset_tracks(camera)
    face_identity.reset_camera_identities(camera)

    class _Alice(_Stranger):
        def recognize(self, _crop):
            self.recognitions += 1
            return SimpleNamespace(person_id=7, name='Alice', score=0.9, runner_up_score=0.1)

    service = _Alice()
    monkeypatch.setattr(face_identity, 'get_face_recognition_service', lambda: service)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    for cycle in range(10):
        face_pass = cycle % 2 == 0
        detections = update_object_tracks(camera, [_face()] if face_pass else [], hold_labels=set() if face_pass else {'face'})
        face_identity.annotate_face_identities(camera, detections, frame)
    assert service.recognitions == 1, 'a recognised track is embedded once, not on every face pass'
    _reset_tracks(camera)
    face_identity.reset_camera_identities(camera)


def test_face_mode_set_on_the_faces_page_is_honoured():
    from app.object_settings import filter_detections_by_motion_mode

    still_face = {**_face(), 'motion_state': 'still'}
    default = {'default_mode': 'moving', 'labels': {}}
    assert filter_detections_by_motion_mode([still_face], None, default), 'faces default to moving and still'
    moving_only = {'default_mode': 'moving', 'labels': {'face': 'moving'}}
    assert filter_detections_by_motion_mode([still_face], None, moving_only) == []


def test_face_rule_cooldown_of_zero_is_honoured(monkeypatch):
    from app import face_detection_rules as fdr

    rule = {'id': 'alice', 'person_id': 7, 'name': 'Alice', 'enabled': True, 'cooldown_minutes': 0}
    monkeypatch.setattr(fdr, 'effective_face_recognition_config', lambda: {'enabled': True})
    monkeypatch.setattr(fdr, 'enabled_rules_for_label', lambda *_a, **_k: rule)
    fdr._face_rule_cooldowns.pop('cam-zero', None)
    face = {**_face(), 'recognized': True, 'track_id': 3, 'person_id': 7, 'person_name': 'Alice'}
    assert len(fdr.known_face_rules_for_camera('cam-zero', [face])) == 1
    assert len(fdr.known_face_rules_for_camera('cam-zero', [face])) == 1, 'cooldown 0 means no cooldown'
    rule['cooldown_minutes'] = None
    fdr._face_rule_cooldowns.pop('cam-zero', None)
    assert len(fdr.known_face_rules_for_camera('cam-zero', [face])) == 1
    assert fdr.known_face_rules_for_camera('cam-zero', [face]) == [], 'unset keeps the 5 minute default'
    fdr._face_rule_cooldowns.pop('cam-zero', None)


def test_late_face_alert_is_not_swallowed_by_the_person_event(tmp_path, monkeypatch):
    """Recognition lands a cycle after the person's event; the duplicate-event
    gate used to suppress that cycle, so "Alice detected" never fired."""
    from tests.support import _load_app, _m

    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()
    plan = {'face_alert': False}

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image, confidence=None):
            return [{'label': 'person', 'confidence': 0.9, 'box': {'x': 200, 'y': 120, 'width': 200, 'height': 400}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    monkeypatch.setattr(mods.live_monitor, 'known_face_rules_for_camera', lambda _cam, _dets: [
        {'rule_name': 'Alice', 'face_rule_id': 'alice', 'zone_id': '', 'label': 'face', 'confidence': 0.9,
         'message': 'Alert triggered: Alice detected'},
    ] if plan['face_alert'] else [])
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx'}, main.utc_now())
    main.database.set_setting('objects', {'default_mode': 'any', 'labels': {}, 'still_alerts': {}}, main.utc_now())
    zone = {'id': 'porch', 'name': 'Porch', 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'monitor_motion': False,
            'monitor_objects': True, 'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5,
                                                       'cooldown_seconds': 60, 'record_on_detect': True}]}
    settings = {'id': 'cam-porch', 'name': 'Porch', 'detection': {'zones': [zone]}, 'recording': {'continuous': False}}
    base = time.time() - 30
    clock = {'now': base}
    monkeypatch.setattr(mods.event_debounce, 'time', SimpleNamespace(time=lambda: clock['now']))

    def cycle(offset):
        clock['now'] = base + offset
        return mods.live_monitor.process_live_stream_alerts(
            b'jpeg', {'timestamp': base + offset, 'width': 1280, 'height': 720}, settings, enforce_interval=False,
        )

    person_event = cycle(0.0)
    assert person_event is not None
    assert cycle(0.5) is None, 'the same person inside its cooldown is still a duplicate'
    plan['face_alert'] = True
    face_event = cycle(1.0)
    assert face_event is not None, 'the recognised face must not be swallowed by the person cooldown'
    alerts = [row['rule_name'] for row in main.database.alerts(limit=20)]
    assert 'Alice' in alerts


def test_matcher_reload_keeps_the_stranger_alert_guard(monkeypatch):
    """Enrolling someone (or auto-enrich elsewhere) reloads the matcher; that
    invalidates cached matches but not which strangers already alerted."""
    camera = 'cam-face-reload'
    _reset_tracks(camera)
    face_identity.reset_camera_identities(camera)
    service = _Stranger()
    monkeypatch.setattr(face_identity, 'get_face_recognition_service', lambda: service)
    monkeypatch.setattr(face_identity, 'effective_face_detection_rules', lambda: {'rules': [{'id': '_unknown', 'enabled': True}]})
    import app.postprocess_pool as pool
    monkeypatch.setattr(pool, 'enrichment_pool', lambda: SimpleNamespace(submit=lambda *_a, **_k: None))
    monkeypatch.setattr(state, 'database', None)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    alerts = []
    for cycle in range(4):
        if cycle == 2:
            service.matcher_generation += 1
        detections = face_identity.annotate_face_identities(camera, update_object_tracks(camera, [_face()]), frame)
        alerts.extend(face_identity.unknown_face_alerts(camera, detections))
    assert len(alerts) == 1
    assert service.recognitions == 4, 'unknown faces are retried; the reload itself must not break that'
    _reset_tracks(camera)
    face_identity.reset_camera_identities(camera)


def test_a_capture_the_queue_rejects_is_offered_again(monkeypatch):
    camera = 'cam-face-queue-full'
    face_identity.reset_camera_identities(camera)
    import app.postprocess_pool as pool
    accepted = []
    replies = iter([False, True])

    def submit(_fn, *args, **_kw):
        ok = next(replies)
        if ok:
            accepted.append(args[1])
        return ok

    monkeypatch.setattr(pool, 'enrichment_pool', lambda: SimpleNamespace(submit=submit))
    monkeypatch.setattr(state, 'database', None)
    crop = np.zeros((20, 20, 3), dtype=np.uint8)
    face_identity._maybe_capture_unknown(camera, 5, {}, crop, None)
    face_identity._maybe_capture_unknown(camera, 5, {}, crop, None)
    assert accepted == [5], 'the rejected capture is retried on the next sighting'
    face_identity._maybe_capture_unknown(camera, 5, {}, crop, None)
    assert accepted == [5], 'and still only once per track once it is queued'
    face_identity.reset_camera_identities(camera)


def test_a_failed_capture_is_logged_as_a_warning(monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError('disk full')

    import importlib

    # Patch the live module: other tests in this file reload the app package.
    monkeypatch.setattr(importlib.import_module('app.state'), 'database', SimpleNamespace(store_unknown_face=broken))
    service = SimpleNamespace(model_id='m', embed_face=lambda _crop: np.ones(4, dtype=np.float32))
    warnings = []
    monkeypatch.setattr(face_identity.logger, 'warning', lambda msg, *args: warnings.append(msg % args))
    face_identity._store_unknown_face('cam', 1, {}, np.zeros((20, 20, 3), dtype=np.uint8), service)
    assert any('Failed to capture unknown face on camera cam' in message for message in warnings)
