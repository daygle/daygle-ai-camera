"""API integration tests: live-stream detection queue and its record-on-detection behaviour.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


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
    _app, _database_path = _load_app(tmp_path, monkeypatch)
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
    _app, _database_path = _load_app(tmp_path, monkeypatch)
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


def test_live_stream_detection_saves_only_allowed_zone_object_labels(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
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
    _app, _database_path = _load_app(tmp_path, monkeypatch)
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
