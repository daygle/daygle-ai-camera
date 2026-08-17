"""API integration tests: live-stream detection queue and its record-on-detection behaviour.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_live_status_preserves_best_object_confidence_for_vision_card(tmp_path, monkeypatch):
    """Live status keeps confidence available even when the UI falls back to labels."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.detection_status as detection_status

    main._state.live_detection_status.clear()
    detection_status.update_live_detection_status(
        'camera-1',
        state='checked',
        detected_labels=['person'],
        detections=[
            {'label': 'person', 'confidence': 0.71},
            {'label': 'person', 'confidence': 0.86},
        ],
    )

    payload = detection_status.live_detection_status_payload('camera-1')
    assert payload['detection_confidences'] == {'person': 0.86}


def test_live_object_status_reports_below_threshold_reason(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    from app.live_monitor import _below_threshold_object_reason

    reason = _below_threshold_object_reason(
        [{'label': 'person', 'confidence': 0.42, 'box': {'x': 0.2, 'y': 0.2, 'width': 0.2, 'height': 0.2}}],
        [{
            'id': 'porch',
            'x': 0,
            'y': 0,
            'width': 1,
            'height': 1,
            'monitor_objects': True,
            'object_rules': [{'label': 'person', 'min_confidence': 0.5, 'enabled': True}],
        }],
    )

    assert reason == {
        'code': 'below_threshold',
        'label': 'person',
        'confidence': 0.42,
        'threshold': 0.5,
    }


def test_live_snapshot_queues_foreground_detection_frame(monkeypatch):
    """The live snapshot path supplies frames when background detection is off."""
    import app.api.live_router as live_router
    import app.live_monitor as live_monitor

    captured = []
    monkeypatch.setattr(
        live_monitor,
        'queue_live_stream_alerts',
        lambda image, frame, settings, **kwargs: captured.append((image, frame, settings, kwargs)),
    )

    settings = {'id': 'camera-1', 'width': 1920, 'height': 1080}
    live_router._queue_detection_snapshot(
        settings,
        b'jpeg-frame',
        captured_ts=123.5,
        width=640,
        height=360,
    )

    assert captured == [(
        b'jpeg-frame',
        {
            'frame_number': 0,
            'timestamp': 123.5,
            'width': 640,
            'height': 360,
        },
        settings,
        {'allow_when_background_enabled': True},
    )]


def test_live_stream_detection_queue_runs_in_background_and_deduplicates(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    started = threading.Event()
    release = threading.Event()

    class SlowDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None
        calls = 0

        def detect_image(self, _image_bytes, confidence=None):
            self.calls += 1
            started.set()
            release.wait(timeout=2)
            return []

    detector = SlowDetector()
    monkeypatch.setattr(main._state, 'detector', detector)
    main._state.live_detection_last_checked.clear()
    main._state.active_live_detection_cameras.clear()
    # queue_live_stream_alerts is the frontend-triggered path and only runs detection
    # when background_detection_enabled=False (otherwise the background monitor handles it).
    main.database.set_setting('live', {'background_detection_enabled': False}, main.utc_now())
    settings = {'id': 'camera-1', 'name': 'Front Door', 'detection': {'zones': []}}

    mods.live_monitor.queue_live_stream_alerts(b'jpeg-frame-1', {'width': 1280, 'height': 720}, settings)
    assert started.wait(timeout=2)
    mods.live_monitor.queue_live_stream_alerts(b'jpeg-frame-2', {'width': 1280, 'height': 720}, settings)

    assert detector.calls == 1
    assert 'camera-1' in main._state.active_live_detection_cameras

    release.set()
    deadline = time.time() + 2
    while 'camera-1' in main._state.active_live_detection_cameras and time.time() < deadline:
        time.sleep(0.01)
    assert 'camera-1' not in main._state.active_live_detection_cameras


def test_live_stream_detection_without_alert_rule_does_not_record_by_default(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [
                {
                    'label': 'person',
                    'confidence': 0.91,
                    'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360},
                }
            ]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main._state.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    # The JPEG frame fails the motion gate (diff mask None), so every
    # detection is classified 'still'. This test is about recording rules,
    # not the still/moving filter: keep the historical any default.
    main.database.set_setting('objects', {'default_mode': 'any', 'labels': {}, 'still_alerts': {}}, main.utc_now())

    event_id = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {
                'zones': [
                    {'id': 'porch', 'name': 'Porch', 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'monitor_motion': True, 'monitor_objects': True},
                ],
            },
            'recording': {'continuous': False},
        },
    )

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert event['recording_status'] == 'none'
    status = mods.detection_status.live_detection_status_payload('camera-1')
    assert status['state'] == 'checked'
    assert status['recording_state'] == 'skipped'
    assert 'waiting for an enabled alert rule' in status['recording_reason']


def _detect_frame_with_objects_setting(tmp_path, monkeypatch, objects_setting, detections):
    """Run one live detection cycle against the given ``objects`` setting.

    Returns ``(event_id, status_payload)``. Uses a JPEG-byte frame so the
    motion gate fails closed (diff mask ``None``) and every detection is
    classified as **still** -- which is exactly what the still/moving filter
    needs to prove it drops (or keeps) still subjects.
    """
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return detections

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main._state.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    main.database.set_setting('objects', objects_setting, main.utc_now())

    event_id = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {'zones': []},
        },
    )
    status = mods.detection_status.live_detection_status_payload('camera-1')
    return event_id, status


def test_live_stream_moving_only_setting_drops_still_person(tmp_path, monkeypatch):
    """A 'moving only' person must not create an event for a still subject."""
    event_id, status = _detect_frame_with_objects_setting(
        tmp_path, monkeypatch,
        {'default_mode': 'any', 'labels': {'person': 'moving'}},
        [{'label': 'person', 'confidence': 0.91, 'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360}}],
    )
    assert event_id is None
    assert [detection['label'] for detection in status['detections']] == []
    assert 'person' not in status.get('detected_labels', [])


def test_live_stream_still_only_setting_keeps_still_person(tmp_path, monkeypatch):
    """A 'still only' person fires normally for a still subject."""
    event_id, status = _detect_frame_with_objects_setting(
        tmp_path, monkeypatch,
        {'default_mode': 'any', 'labels': {'person': 'still'}},
        [{'label': 'person', 'confidence': 0.91, 'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360}}],
    )
    assert event_id is not None
    assert [detection['label'] for detection in status['detections']] == ['person']
    # The kept detection carries its classification so the live overlay can tag it.
    assert status['detections'][0]['motion_state'] == 'still'


def test_live_stream_default_any_mode_annotates_motion_state(tmp_path, monkeypatch):
    """Even with no restricted labels, detections carry a moving/still tag."""
    event_id, status = _detect_frame_with_objects_setting(
        tmp_path, monkeypatch,
        {'default_mode': 'any', 'labels': {}},
        [{'label': 'person', 'confidence': 0.91, 'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360}}],
    )
    assert event_id is not None
    assert [detection['label'] for detection in status['detections']] == ['person']
    assert status['detections'][0]['motion_state'] == 'still'  # no diff mask on a JPEG-byte frame


def test_live_stream_still_alert_fires_once_streak_crosses_threshold(tmp_path, monkeypatch):
    """A 'still for N minutes' label alerts after N minutes of continuous stillness.

    Uses a JPEG-byte frame (no diff mask -> every detection is 'still') and a
    fake clock inside app.object_settings so the streak can be aged by six
    minutes between two cycles. The dwell event must bypass the per-label
    debounce (the label has been continuously detected, so its window is
    always fresh-refreshed) and carry the still-alert marker into the event.
    """
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.object_settings as object_settings
    mods = _m()

    class FakeTime:
        current = 1000.0

        @classmethod
        def time(cls):
            return cls.current

    monkeypatch.setattr(object_settings, 'time', FakeTime)

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [{'label': 'person', 'confidence': 0.91, 'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main._state.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    main.database.set_setting('objects', {'default_mode': 'any', 'still_alerts': {'person': 5}}, main.utc_now())

    settings = {'id': 'camera-1', 'name': 'Front Door', 'detection': {'zones': []}}

    # Cycle 1 starts the still streak; it is a normal person event, no dwell marker.
    # enforce_interval=False so the 0.5s interval gate cannot swallow the
    # back-to-back test cycles.
    event1 = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False,
    )
    assert event1 is not None
    status1 = mods.detection_status.live_detection_status_payload('camera-1')
    assert status1['detections'][0]['motion_state'] == 'still'
    assert not any(d.get('still_alert') for d in status1['detections'])

    # Advance six minutes: cycle 2 crosses the 5-minute threshold -> dwell alert.
    FakeTime.current = 1000.0 + 6 * 60
    event2 = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False,
    )
    status2 = mods.detection_status.live_detection_status_payload('camera-1')
    assert event2 is not None, f"cycle 2 status: {status2}"
    assert event2 != event1
    event2_data = main.database.get_event(event2)
    dwell_dets = [d for d in (event2_data.get('detections') or []) if d.get('still_alert')]
    assert len(dwell_dets) == 1
    assert dwell_dets[0]['label'] == 'person'
    assert dwell_dets[0]['still_alert_minutes'] == 5

    # The alert history carries the named dwell rule.
    alerts = main.database.alerts(limit=50)
    assert any('Still for 5 min' in str(alert.get('rule_name') or '') for alert in alerts)

    # A third cycle while still does not re-fire (one alert per streak).
    FakeTime.current = 1000.0 + 30 * 60
    event3 = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False,
    )
    assert event3 is None  # debounced/suppressed - the streak already alerted


def test_live_stream_default_moving_mode_applies_to_all_labels(tmp_path, monkeypatch):
    """A global 'moving only' default filters every label without an override."""
    event_id, status = _detect_frame_with_objects_setting(
        tmp_path, monkeypatch,
        {'default_mode': 'moving', 'labels': {}},
        [{'label': 'car', 'confidence': 0.91, 'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360}}],
    )
    assert event_id is None
    assert [detection['label'] for detection in status['detections']] == []


def test_live_stream_detection_saves_only_allowed_zone_object_labels(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [
                {
                    'label': 'person',
                    'confidence': 0.91,
                    'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360},
                },
                {
                    'label': 'suitcase',
                    'confidence': 0.88,
                    'box': {'x': 500, 'y': 120, 'width': 180, 'height': 220},
                },
            ]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main._state.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    # The JPEG frame fails the motion gate (diff mask None), so every
    # detection is classified 'still'. This test is about zone label
    # filtering, not the still/moving filter: keep the historical any default.
    main.database.set_setting('objects', {'default_mode': 'any', 'labels': {}, 'still_alerts': {}}, main.utc_now())

    event_id = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {
                'zones': [
                    {
                        'id': 'porch',
                        'name': 'Porch',
                        'x': 0,
                        'y': 0,
                        'width': 1,
                        'height': 1,
                        'monitor_motion': False,
                        'monitor_objects': True,
                        'object_labels': ['person', 'cat'],
                    },
                ],
            },
        },
    )

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert [detection['label'] for detection in event['detections']] == ['person']
    status = mods.detection_status.live_detection_status_payload('camera-1')
    assert [detection['label'] for detection in status['detections']] == ['person']


def test_live_stream_camera_continuous_recording_records_without_alert_rule(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [
                {
                    'label': 'person',
                    'confidence': 0.91,
                    'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360},
                }
            ]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main._state.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    # The JPEG frame fails the motion gate (diff mask None), so every
    # detection is classified 'still'. This test is about continuous
    # recording, not the still/moving filter: keep the historical any default.
    main.database.set_setting('objects', {'default_mode': 'any', 'labels': {}, 'still_alerts': {}}, main.utc_now())

    event_id = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {
                'zones': [
                    {'id': 'porch', 'name': 'Porch', 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'monitor_motion': True, 'monitor_objects': True},
                ],
            },
            'recording': {'continuous': True},
        },
    )

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert event['recording_status'] == 'linked'
    assert event['recordings'][0]['trigger_type'] == 'continuous'
    status = mods.detection_status.live_detection_status_payload('camera-1')
    assert status['recording_state'] == 'linked'
