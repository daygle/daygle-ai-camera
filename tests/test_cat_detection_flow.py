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


def _drive(tmp_path, monkeypatch, positions, *, rule_label='cat', record=True):
    """Run one cycle per entry of ``positions`` (the cat's box x, or None when
    it is out of view). Returns per-cycle results and the AI-routed alerts."""
    main = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as lm

    settings = _camera(rule_label, record=record)
    cat_x = {'value': None}

    class CatDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image, confidence=None):
            if cat_x['value'] is None:
                return []
            return [{'label': 'cat', 'confidence': 0.8,
                     'box': {'x': cat_x['value'], 'y': 0.5, 'width': 0.1, 'height': 0.1}}]

    monkeypatch.setattr(main._state, 'detector', CatDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())

    def cat_motion(*_args, **_kwargs):
        mask = np.zeros((MASK_H, MASK_W), dtype=bool)
        if cat_x['value'] is not None:
            left = int(cat_x['value'] * MASK_W)
            mask[45:54, left:left + 16] = True
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

    start = time.time()
    results = []
    for index, position in enumerate(positions):
        cat_x['value'] = position
        statuses.clear()
        now = start + index * CYCLE_SECONDS
        monkeypatch.setattr(time, 'time', lambda now=now: now)
        event_id = lm.process_live_stream_alerts(
            b'\xff\xd8frame', {'width': 1280, 'height': 720, 'timestamp': now},
            settings, enforce_interval=False,
        )
        final = statuses[-1]
        cats = [d for d in final.get('detections') or [] if d.get('label') == 'cat']
        results.append({'event_id': event_id, 'state': final.get('state'), 'cats': cats})
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
