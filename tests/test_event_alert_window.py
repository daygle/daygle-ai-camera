"""An event is an alert only when a notification is due, not when a rule matched.

The events list's "Alert" badge ("an alert notification was fired for this
event") reads the event's alert_history rows. The live monitor wrote one for
every matched rule, so a person at 08:37 on a rule that notifies 20:00-06:00
still showed "Alert" although no email or push was sent. The sound and
behaviour monitors already record an alert only when a notification is due.
"""

from __future__ import annotations

import time

import pytest

from tests.support import _load_app, _m


def _run(tmp_path, monkeypatch, *, now_hm, notify=('20:00', '06:00'), push=True):
    _load_app(tmp_path, monkeypatch)
    import app.alert_dispatch as alert_dispatch
    import app.main as main
    mods = _m()

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image, confidence=None):
            return [{'label': 'person', 'confidence': 0.8,
                     'box': {'x': 400, 'y': 200, 'width': 120, 'height': 300}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    monkeypatch.setattr(alert_dispatch, '_now_hm_in_admin_tz', lambda: now_hm)
    verified = []
    monkeypatch.setattr(
        mods.live_monitor, 'submit_alert_notification_with_verification',
        lambda triggered, event_id, *_a, **_k: verified.append([a['rule_name'] for a in triggered]),
    )
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx'}, main.utc_now())
    main.database.set_setting('objects', {'default_mode': 'any', 'labels': {}, 'still_alerts': {}}, main.utc_now())
    rule = {'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'cooldown_seconds': 30,
            'push_enabled': push, 'email_enabled': False,
            'notify_start': notify[0], 'notify_end': notify[1]}
    settings = {
        'id': 'driveway', 'name': 'Driveway',
        'detection': {'zones': [{'id': 'drive', 'name': 'Driveway', 'enabled': True,
                                 'monitor_objects': True, 'monitor_motion': False,
                                 'x': 0, 'y': 0, 'width': 1, 'height': 1,
                                 'object_rules': [rule]}]},
        'recording': {'continuous': False},
    }
    event_id = mods.live_monitor.process_live_stream_alerts(
        b'jpeg', {'timestamp': time.time() - 1, 'width': 1280, 'height': 720},
        settings, enforce_interval=False,
    )
    assert event_id is not None, 'the detection is still an event outside the window'
    return main.database.get_event(event_id), verified


def test_detection_outside_the_notify_window_is_not_an_alert(tmp_path, monkeypatch):
    event, verified = _run(tmp_path, monkeypatch, now_hm='08:37')
    assert event['alert'] is None
    assert not event['alert_triggered']
    # No AI verification spent on a notification delivery would skip.
    assert verified == []
    assert [d['label'] for d in event['detections']] == ['person']


@pytest.mark.parametrize('now_hm', ['21:15', '03:00'])
def test_detection_inside_the_notify_window_is_an_alert(tmp_path, monkeypatch, now_hm):
    event, verified = _run(tmp_path, monkeypatch, now_hm=now_hm)
    assert event['alert'] is not None
    assert event['alert_triggered']
    assert len(verified) == 1


def test_rule_without_a_notify_window_alerts_any_time(tmp_path, monkeypatch):
    event, _verified = _run(tmp_path, monkeypatch, now_hm='08:37', notify=('', ''))
    assert event['alert'] is not None
