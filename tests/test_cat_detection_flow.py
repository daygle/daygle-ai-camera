"""End-to-end: an enabled object (a cat) entering the footage.

Drives the real ``process_live_stream_alerts`` cycle by cycle with a stub
detector and a motion mask that follows the cat, and checks every stage the
operator relies on:

* the cat is detected and keeps ONE tracker identity while it walks;
* it stays classified ``moving`` (Moving Only, the default) on every sighting,
  including the second one of a slow walker - the overlay must not blink;
* its rule alerts exactly once while it stays in view, and that alert is
  routed through AI verification when the rule asks for it (a ``pet`` group
  rule asks the model about the group);
* it alerts again when it leaves and comes back after the absence window;
* the live status tells the browser the camera watches ``cat`` even when the
  rule is the ``pet`` group, so the overlay draws the box it alerted on.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.main as app_main  # noqa: E402  -- break the circular-import gate
assert app_main is sys.modules["app.main"]

from tests.test_live_track_ordering import _load_app  # noqa: E402

CYCLE_SECONDS = 0.5
MASK_W, MASK_H = 160, 90


def _camera(rule_label: str = 'cat', *, record: bool = True) -> dict:
    return {
        'id': 'cam-garden', 'name': 'Garden',
        'detection': {'object_detection_enabled': True, 'zones': [{
            'id': 'yard', 'name': 'Yard', 'enabled': True,
            'monitor_objects': True, 'monitor_motion': False,
            'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}, {'x': 0, 'y': 1}],
            'object_rules': [{
                'label': rule_label, 'enabled': True, 'min_confidence': 0.4,
                'record_on_detect': record, 'ai_verify': True,
                'alert_schedules': [{'push_enabled': True}],
            }],
        }]},
        'recording': {'continuous': False},
    }


def _seen(label: str, x: float) -> dict:
    return {'label': label, 'confidence': 0.8, 'box': {'x': x, 'y': 0.5, 'width': 0.1, 'height': 0.1}}


def _drive(tmp_path, monkeypatch, positions, *, rule_label='cat', record=True, live=None, rules=None, setup=None):
    """Run one cycle per entry of ``positions``: the cat's box x, None when it
    is out of view, or a list of ``_seen`` detections. Returns per-cycle
    results and the AI-routed alerts."""
    main = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as lm

    settings = _camera(rule_label, record=record)
    if rules is not None:
        settings['detection']['zones'][0]['object_rules'] = rules
    if live:
        main.database.set_setting('live', live, main.utc_now())
    in_view: dict = {'value': []}

    class CatDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image, confidence=None):
            return [dict(seen, box=dict(seen['box'])) for seen in in_view['value']]

    monkeypatch.setattr(main._state, 'detector', CatDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())

    def cat_motion(*_args, **_kwargs):
        mask = np.zeros((MASK_H, MASK_W), dtype=bool)
        for seen in in_view['value']:
            box = seen['box']
            mask[int(box['y'] * MASK_H):int((box['y'] + box['height']) * MASK_H) + 1,
                 int(box['x'] * MASK_W):int((box['x'] + box['width']) * MASK_W) + 1] = True
        return True, 0.5, mask, 0.02

    monkeypatch.setattr(lm, 'detect_frame_motion', cat_motion)
    routed: list[tuple[int, list[dict]]] = []
    monkeypatch.setattr(
        lm, 'submit_alert_notification_with_verification',
        lambda triggered, event_id, _rules, **_kw: routed.append((event_id, list(triggered))),
    )
    statuses: list[dict] = []
    real_status = lm.update_live_detection_status

    def capture_status(camera_id, **kwargs):
        statuses.append(kwargs)
        return real_status(camera_id, **kwargs)

    monkeypatch.setattr(lm, 'update_live_detection_status', capture_status)
    if setup is not None:
        setup(lm)

    start = time.time()
    results = []
    for index, position in enumerate(positions):
        in_view['value'] = (
            position if isinstance(position, list) else [] if position is None else [_seen('cat', position)]
        )
        statuses.clear()
        now = start + index * CYCLE_SECONDS
        monkeypatch.setattr(time, 'time', lambda now=now: now)
        event_id = lm.process_live_stream_alerts(
            b'\xff\xd8frame', {'width': 1280, 'height': 720, 'timestamp': now},
            settings, enforce_interval=False,
        )
        final = statuses[-1]
        cats = [d for d in final.get('detections') or [] if d.get('label') == 'cat']
        results.append({'event_id': event_id, 'state': final.get('state'), 'cats': cats,
                         'reason': final.get('reason')})
    return main, settings, results, routed


def test_cat_entering_is_detected_followed_verified_and_alerted_once(tmp_path, monkeypatch):
    # Walks in from the left at 2-6% of the frame per cycle.
    positions = [0.05, 0.07, 0.10, 0.15, 0.21, 0.27]
    _main, _settings, results, routed = _drive(tmp_path, monkeypatch, positions)

    assert results[0]['event_id'] is not None and results[0]['state'] == 'alerted'
    assert all(result['event_id'] is None for result in results[1:]), 'one alert while it stays in view'
    for result in results:
        assert len(result['cats']) == 1, 'the cat is on the live status every cycle'
        assert result['cats'][0]['motion_state'] == 'moving'
    assert {result['cats'][0]['track_id'] for result in results} == {1}, 'one identity while it walks'

    assert len(routed) == 1
    event_id, alerts = routed[0]
    assert event_id == results[0]['event_id']
    assert [alert['label'] for alert in alerts] == ['cat']
    assert alerts[0]['ai_verify'] is True


def test_group_rule_alerts_verifies_the_group_and_is_watched(tmp_path, monkeypatch):
    from app.detection_status import _watched_status_labels

    _main, settings, results, routed = _drive(
        tmp_path, monkeypatch, [0.05, 0.07, 0.09], rule_label='pet', record=False,
    )
    assert results[0]['event_id'] is not None
    assert len(routed) == 1 and routed[0][1][0]['ai_verify_label'] == 'pet'
    # The live page draws only watched labels; a pet rule watches the cat
    # (and an alert-only rule counts), but not an unrelated class.
    status = {'detections': results[0]['cats'] + [{'label': 'potted plant'}], 'detected_labels': ['cat', 'bench']}
    assert _watched_status_labels(settings, status) == ['cat']


def test_cat_that_leaves_and_returns_alerts_again(tmp_path, monkeypatch):
    # Away for 50s, past the default 45s absence window (the recording's
    # extension step): the return is a new sighting.
    away = 100
    positions = [0.05, 0.07] + [None] * away + [0.50, 0.52]
    _main, _settings, results, routed = _drive(tmp_path, monkeypatch, positions)
    alerted = [index for index, result in enumerate(results) if result['event_id'] is not None]
    assert alerted == [0, 2 + away]
    assert len(routed) == 2
    # The returning cat is followed on its slow second sighting too.
    assert results[-1]['cats'] and results[-1]['cats'][0]['motion_state'] == 'moving'


def test_cat_out_of_view_briefly_does_not_alert_again(tmp_path, monkeypatch):
    # Hidden behind a bush for a few cycles: still the same visit.
    positions = [0.05, 0.08, None, None, None, 0.20, 0.23]
    _main, _settings, results, routed = _drive(tmp_path, monkeypatch, positions)
    assert [result['event_id'] is not None for result in results].count(True) == 1
    assert len(routed) == 1


def test_one_cat_read_as_other_animals_at_night_alerts_once(tmp_path, monkeypatch):
    # IR lighting: the same walking cat reads cat, dog, bird on successive
    # cycles. Counted by label, an ``animal`` rule alerted once per flip.
    labels = ['cat', 'dog', 'bird', 'cat', 'dog', 'cat']
    frames = [[_seen(label, 0.10 + 0.03 * index)] for index, label in enumerate(labels)]
    _main, _settings, results, routed = _drive(tmp_path, monkeypatch, frames, rule_label='animal')
    assert [index for index, result in enumerate(results) if result['event_id'] is not None] == [0]
    assert [[alert['label'] for alert in alerts] for _event, alerts in routed] == [['cat']]


def test_a_real_second_animal_elsewhere_still_alerts(tmp_path, monkeypatch):
    frames = [[_seen('cat', 0.10 + 0.03 * index)] for index in range(3)] + [
        [_seen('cat', 0.19 + 0.03 * index), _seen('dog', 0.80 - 0.03 * index)] for index in range(3)
    ]
    _main, _settings, results, routed = _drive(tmp_path, monkeypatch, frames, rule_label='animal')
    assert [index for index, result in enumerate(results) if result['event_id'] is not None] == [0, 3]
    assert [[alert['label'] for alert in alerts] for _event, alerts in routed] == [['cat'], ['dog']]


def test_status_names_the_confirmation_wait(tmp_path, monkeypatch):
    live = {'detection_confirm_frames': 2, 'detection_confirm_window': 3}
    _main, _settings, results, _routed = _drive(tmp_path, monkeypatch, [0.10, 0.13], live=live)
    assert results[0]['reason'] == 'Cat: Not confirmed yet (needs 2 of 3 cycles)'
    assert results[1]['event_id'] is not None


def _rule(label: str, *, verify: bool = True) -> dict:
    return {'label': label, 'enabled': True, 'min_confidence': 0.4, 'ai_verify': verify,
            'alert_schedules': [{'push_enabled': True}]}


def test_cat_the_detector_calls_a_person_alerts_as_a_cat_after_ai_check(tmp_path, monkeypatch):
    """ONNX misreads the cat as a person; the person rule asks the model,
    which rejects it and names a cat, so the cat rule alerts instead - once,
    including after the detector starts reading it correctly."""
    asked, delivered = [], []

    def real_verification(lm):
        import app.ai_verification as av

        def model(event, labels, _settings, **kwargs):
            asked.append((labels, kwargs.get('candidates')))
            return {'status': 'verified', 'labels': {
                label: {'present': False, 'reason': 'it is a cat', 'instead': 'cat'} for label in labels
            }}, None

        monkeypatch.setattr(av, 'verify_and_describe_event', model)
        monkeypatch.setattr(av, '_forward', lambda alerts, event_id, _rules: delivered.append(
            [(alert['label'], alert.get('ai_relabel_from')) for alert in alerts]))
        monkeypatch.setattr(lm, 'submit_alert_notification_with_verification', lambda alerts, event_id, rules, **kw: (
            av._verify_and_forward(alerts, event_id, rules, 'Garden', None, kw.get('camera_id'))))

    frames = [[_seen('person', 0.10 + 0.03 * index)] for index in range(3)] + [
        [_seen('cat', 0.19 + 0.03 * index)] for index in range(3)
    ]
    _main, _settings, results, _routed = _drive(
        tmp_path, monkeypatch, frames, rules=[_rule('person'), _rule('cat', verify=False)], setup=real_verification,
    )
    assert [index for index, result in enumerate(results) if result['event_id'] is not None] == [0]
    assert asked == [(['person'], {'person': ['cat']})]
    assert delivered == [[('cat', 'person')]]


def test_distant_cat_the_detector_catches_every_other_cycle(tmp_path, monkeypatch):
    def distant(index: int) -> list:
        if index % 2:
            return []
        seen = _seen('cat', 0.60 + 0.006 * index)
        seen['confidence'] = 0.45
        seen['box'].update(y=0.3, width=0.025, height=0.02)
        return [seen]

    live = {'detection_confirm_frames': 2, 'detection_confirm_window': 3}
    _main, _settings, results, routed = _drive(tmp_path, monkeypatch, [distant(index) for index in range(12)], live=live)
    assert [index for index, result in enumerate(results) if result['event_id'] is not None] == [2]
    assert len(routed) == 1
    assert {result['cats'][0]['track_id'] for result in results if result['cats']} == {1}


def _model_replies(monkeypatch, reply, delivered, calls):
    """Run AI verification in-line with the real verifier; only the model's
    raw reply is faked (``reply``: text, or an exception to raise)."""
    import json as _json

    def setup(lm):
        import app.ai_verification as av

        def chat(_self, messages, max_tokens=80):
            calls.append(messages[-1]['content'][0]['text'])
            if isinstance(reply, Exception):
                raise reply
            return _json.dumps(reply)

        monkeypatch.setattr(av.VisionVerifier, 'chat', chat)
        monkeypatch.setattr(av, '_forward', lambda alerts, _event, _rules: delivered.append(
            [(alert['label'], alert.get('detector_label')) for alert in alerts]))
        monkeypatch.setattr(lm, 'submit_alert_notification_with_verification', lambda alerts, event_id, rules, **kw: (
            av._verify_and_forward(alerts, event_id, rules, 'Garden', None, kw.get('camera_id'))))
    return setup


def _dog_reading(count: int) -> list:
    return [[_seen('dog', 0.10 + 0.03 * index)] for index in range(count)]


def test_cat_the_detector_calls_a_dog_alerts_on_the_models_yes(tmp_path, monkeypatch):
    delivered, calls = [], []
    _main, _settings, results, _routed = _drive(
        tmp_path, monkeypatch, _dog_reading(5), rules=[_rule('cat')],
        setup=_model_replies(monkeypatch, {'reason': 'a cat', 'present': True}, delivered, calls),
    )
    assert [index for index, result in enumerate(results) if result['event_id'] is not None] == [0]
    assert len(calls) == 1 and delivered == [[('cat', 'dog')]]
    assert all(result['cats'] for result in results), 'drawn as the cat the model confirmed'


def test_dog_reading_the_model_rejects_or_cannot_check_never_alerts(tmp_path, monkeypatch):
    from app.ai_verification import VerificationError

    for reply in ({'reason': 'a dog', 'present': False}, VerificationError('timed out')):
        delivered, calls = [], []
        _main, _settings, _results, _routed = _drive(
            tmp_path, monkeypatch, _dog_reading(4), rules=[_rule('cat')],
            setup=_model_replies(monkeypatch, reply, delivered, calls),
        )
        assert delivered == [[]] and len(calls) == 1


def test_dog_reading_is_left_alone_without_ai_check(tmp_path, monkeypatch):
    _main, _settings, results, routed = _drive(
        tmp_path, monkeypatch, _dog_reading(3), rules=[_rule('cat', verify=False)],
    )
    assert routed == [] and all(result['event_id'] is None for result in results)


def test_animal_rule_alerts_as_the_kind_the_model_names(tmp_path, monkeypatch):
    delivered, calls = [], []
    frames = [[_seen('bird', 0.10 + 0.03 * index)] for index in range(4)]
    _main, _settings, results, _routed = _drive(
        tmp_path, monkeypatch, frames, rules=[_rule('animal')],
        setup=_model_replies(monkeypatch, {'reason': 'a cat', 'present': True, 'kind': 'cat'}, delivered, calls),
    )
    assert delivered == [[('cat', 'bird')]]
    assert all(result['cats'] for result in results[1:]), 'drawn as the cat from then on'
    assert [index for index, result in enumerate(results) if result['event_id'] is not None] == [0]


def test_cat_stepping_out_from_cover_is_followed_and_alerts_on_time(tmp_path, monkeypatch):
    def at(x, width):
        seen = _seen('cat', x)
        seen['box']['width'] = width
        return [seen]

    frames = [at(0.50, 0.03), at(0.50, 0.06), at(0.50, 0.09), at(0.51, 0.10), at(0.54, 0.10)]
    live = {'detection_confirm_frames': 2, 'detection_confirm_window': 3}
    _main, _settings, results, _routed = _drive(tmp_path, monkeypatch, frames, live=live)
    assert [index for index, result in enumerate(results) if result['event_id'] is not None] == [1]
    assert all(result['cats'] and result['cats'][0]['motion_state'] == 'moving' for result in results)


def test_sitting_cat_stays_on_the_live_view_without_alerting(tmp_path, monkeypatch):
    positions = [0.10, 0.14, 0.18] + [0.20] * 12
    _main, _settings, results, routed = _drive(tmp_path, monkeypatch, positions)
    assert len(routed) == 1
    assert all(result['cats'] for result in results), 'never vanishes from the live view'
    assert results[-1]['cats'][0]['motion_state'] == 'still'
    assert 'Moving Only' in results[-1]['reason']


def test_sitting_cat_stays_on_clip_playback_display_only(tmp_path, monkeypatch):
    positions = [0.10, 0.14, 0.18] + [0.20] * 8
    start = time.time()
    _main, _settings, results, _routed = _drive(tmp_path, monkeypatch, positions)
    # Imported after _drive: the test app reloads the app modules.
    from app.detection_state import build_track_from_live_history
    assert results[-1]['cats'][0]['motion_state'] == 'still'
    track = build_track_from_live_history('cam-garden', start - 5, start + 60)
    last = track[-1]
    assert [(d['label'], d.get('motion_state'), d.get('display_only')) for d in last['detections']] == [
        ('cat', 'still', True)]
    assert [d['label'] for d in last.get('dropped') or []] == ['cat'], 'the log still explains the drop'
    assert all(not d.get('display_only') for sample in track[:3] for d in sample['detections'])


def test_a_persons_head_read_as_a_cat_never_shows_or_alerts(tmp_path, monkeypatch):
    """The doorway frame: seen from above, a woman's head was boxed "Cat 76%"
    inside her own person box. Only the person shows, and only she alerts."""
    def frame(dx):
        person = {'label': 'person', 'confidence': 0.85,
                  'box': {'x': 0.431 + dx, 'y': 0.196, 'width': 0.181, 'height': 0.604}}
        head = {'label': 'cat', 'confidence': 0.76,
                'box': {'x': 0.433 + dx, 'y': 0.218, 'width': 0.102, 'height': 0.149}}
        return [person, head]

    _main, _settings, results, routed = _drive(
        tmp_path, monkeypatch, [frame(0.02 * index) for index in range(4)],
        rules=[_rule('cat', verify=False), _rule('person', verify=False)],
    )
    assert all(result['cats'] == [] for result in results)
    assert [[alert['label'] for alert in alerts] for _event, alerts in routed] == [['person']]
