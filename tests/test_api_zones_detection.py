"""API integration tests: monitoring zones, object/motion rules, zone filtering, and alert-engine gating.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_motion_min_confidence_filters_low_confidence_motion(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image_bytes, confidence=None):
            return [
                {
                    'label': 'person',
                    'confidence': 0.4,
                    'box': {'x': 64, 'y': 72, 'width': 320, 'height': 360},
                }
            ]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main._state.live_detection_last_checked.clear()
    # The 'jpeg-frame' bytes below are not a decodable image, so the adaptive
    # background gate cannot measure them. Pin the gate to a deterministic
    # low-confidence (0.4) motion read instead: this test exercises the
    # motion-rule min_confidence filter itself, not image decoding.
    monkeypatch.setattr(
        mods.live_monitor,
        'detect_frame_motion',
        lambda camera_id, image, **_kwargs: (True, 0.4, None, 0.04),
    )

    strict_settings = {
        'id': 'camera-1',
        'name': 'Front Door',
        'detection': {
            'object_labels': ['cat'],
            'zones': [
                {
                    'id': 'motion-zone',
                    'name': 'Motion Zone',
                    'x': 0,
                    'y': 0,
                    'width': 1,
                    'height': 1,
                    'object_rules': [{'label': 'motion', 'min_confidence': 0.45}],
                },
            ],
        },
    }

    blocked_event_id = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        strict_settings,
        enforce_interval=False,
    )
    assert blocked_event_id is None

    relaxed_settings = {
        'id': 'camera-1',
        'name': 'Front Door',
        'detection': {
            'object_labels': ['cat'],
            'zones': [
                {
                    'id': 'motion-zone',
                    'name': 'Motion Zone',
                    'x': 0,
                    'y': 0,
                    'width': 1,
                    'height': 1,
                    'object_rules': [{'label': 'motion', 'min_confidence': 0.35}],
                },
            ],
        },
    }

    first_allowed_frame = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        relaxed_settings,
        enforce_interval=False,
    )
    assert first_allowed_frame is None, 'the first motion frame must wait for confirmation'
    allowed_event_id = mods.live_monitor.process_live_stream_alerts(
        b'jpeg-frame',
        {'width': 1280, 'height': 720},
        relaxed_settings,
        enforce_interval=False,
    )
    assert allowed_event_id is not None
    event = main.database.get_event(allowed_event_id)
    assert event is not None
    assert any(detection['label'] == 'motion' for detection in event['detections'])


def test_invalid_motion_frame_does_not_create_recording(tmp_path, monkeypatch):
    """A motion-gate decode failure must not synthesize a motion recording."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    class EmptyDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image_bytes, confidence=None):
            return []

    monkeypatch.setattr(main._state, 'detector', EmptyDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())

    settings = {
        'id': 'camera-1',
        'name': 'Front Door',
        'detection': {
            'zones': [{
                'id': 'motion-zone',
                'name': 'Motion Zone',
                'x': 0, 'y': 0, 'width': 1, 'height': 1,
                'monitor_motion': True,
                'monitor_objects': False,
                'object_rules': [{
                    'label': 'motion',
                    'min_confidence': 0.45,
                    'record_on_detect': True,
                }],
            }],
        },
        'recording': {'continuous': False},
    }

    event_id = mods.live_monitor.process_live_stream_alerts(
        b'not-a-valid-jpeg',
        {'timestamp': time.time(), 'width': 1280, 'height': 720},
        settings,
        enforce_interval=False,
    )

    assert event_id is None


def test_multiple_cameras_have_per_camera_detection_settings_and_zones(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        cameras = [
            {
                'id': 'front-door',
                'name': 'Front Door',
                'backend': 'onvif',
                'stream_url': 'rtsp://127.0.0.1:554/front-door',
                'width': 1280,
                'height': 720,
                'fps': 15,
                'detection': {
                    'motion_enabled': True,
                    'object_detection_enabled': True,
                    'object_labels': ['person', 'cat'],
                    'zones': [
                        {'id': 'porch', 'name': 'Porch', 'x': 0.0, 'y': 0.0, 'width': 0.5, 'height': 0.5, 'monitor_motion': True, 'monitor_objects': True, 'object_labels': ['person']}
                    ],
                },
            },
            {
                'id': 'garage',
                'name': 'Garage',
                'backend': 'onvif',
                'stream_url': 'rtsp://127.0.0.1:554/garage',
                'width': 640,
                'height': 480,
                'fps': 10,
                'detection': {'motion_enabled': False, 'object_detection_enabled': False, 'zones': []},
            },
        ]
        status, _headers, payload = client.request('/api/cameras', method='PUT', json_body={'cameras': cameras}, headers={'X-CSRF-Token': csrf})
        assert status == 200
        assert [camera['id'] for camera in payload['cameras']] == ['front-door', 'garage']
        assert payload['cameras'][0]['detection']['object_labels'] == ['person', 'cat']
        assert payload['cameras'][0]['detection']['zones'][0]['name'] == 'Porch'
        assert payload['cameras'][0]['detection']['zones'][0]['object_labels'] == ['person']

        status, _headers, listed = client.request('/api/cameras')
        assert status == 200
        assert len(listed['cameras']) == 2
        assert listed['cameras'][1]['detection']['object_detection_enabled'] is False

        status, _headers, status_payload = client.request('/api/status?camera_id=garage')
        assert status == 200
        assert status_payload['camera_id'] == 'garage'
        assert status_payload['resolution'] == {'width': 640, 'height': 480}

        status, _headers, updated = client.request(
            '/api/cameras/front-door',
            method='PUT',
            json_body={
                **listed['cameras'][0],
                'detection': {
                    **listed['cameras'][0]['detection'],
                    'zones': [
                        {'id': 'driveway', 'name': 'Driveway', 'x': 0.25, 'y': 0.25, 'width': 0.5, 'height': 0.5, 'monitor_motion': True, 'monitor_objects': False, 'object_labels': 'cat, person, cat'}
                    ],
                },
            },
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert updated['detection']['zones'][0]['id'] == 'driveway'
        assert updated['detection']['zones'][0]['monitor_objects'] is False
        assert updated['detection']['zones'][0]['object_labels'] == ['cat', 'person']
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_polygon_monitoring_zones_are_normalized_and_filter_by_shape(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.zone_schema as _zs
    import app.zone_detection as _zd
    triangle = {
        'id': 'triangle',
        'name': 'Triangle',
        'points': [
            {'x': 0.1, 'y': 0.1},
            {'x': 0.8, 'y': 0.1},
            {'x': 0.1, 'y': 0.8},
        ],
        'monitor_motion': True,
        'monitor_objects': True,
    }

    zones = _zs.normalize_monitoring_zones([triangle])

    assert zones[0]['x'] == 0.1
    assert zones[0]['y'] == 0.1
    assert zones[0]['width'] == 0.7
    assert zones[0]['height'] == 0.7
    assert zones[0]['points'] == triangle['points']

    settings = {'detection': {'zones': zones}}
    detections = [
        {'label': 'person', 'box': {'x': 0.25, 'y': 0.25, 'width': 0.1, 'height': 0.1}},
        {'label': 'car', 'box': {'x': 0.7, 'y': 0.7, 'width': 0.1, 'height': 0.1}},
    ]

    filtered = _zd.filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_objects', require_zones=True)

    assert [detection['label'] for detection in filtered] == ['person']


def test_monitoring_zones_filter_object_detections_by_label(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.zone_schema as _zs
    import app.zone_detection as _zd
    zones = _zs.normalize_monitoring_zones([
        {
            'id': 'porch',
            'name': 'Porch',
            'x': 0,
            'y': 0,
            'width': 1,
            'height': 1,
            'monitor_objects': True,
            'object_labels': ['person', 'cat'],
        }
    ])
    settings = {'detection': {'zones': zones}}
    detections = [
        {'label': 'person', 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}},
        {'label': 'suitcase', 'box': {'x': 0.2, 'y': 0.2, 'width': 0.1, 'height': 0.1}},
        {'label': 'cat', 'box': {'x': 0.3, 'y': 0.3, 'width': 0.1, 'height': 0.1}},
    ]

    filtered = _zd.filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_objects', require_zones=True)

    assert [detection['label'] for detection in filtered] == ['person', 'cat']


def test_monitoring_zones_normalize_object_rules(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.zone_schema as _zs
    zones = _zs.normalize_monitoring_zones([
        {
            'id': 'porch',
            'name': 'Porch',
            'x': 0,
            'y': 0,
            'width': 1,
            'height': 1,
            'monitor_motion': False,
            'object_rules': [
                {
                    'label': 'Cat',
                    'record_on_detect': False,
                    'alert_on_detect': True,
                    'min_confidence': 0.7,
                    'cooldown_seconds': 5,
                    'email_enabled': True,
                    'email_recipients': 'alerts@example.test, bad-address',
                    'active_start': '07:00',
                    'active_end': '18:00',
                    'notify_start': '22:00',
                    'notify_end': '05:00',
                }
            ],
        }
    ])

    rule = zones[0]['object_rules'][0]
    assert zones[0]['object_labels'] == ['cat']
    assert rule['label'] == 'cat'
    assert rule['record_on_detect'] is False
    assert rule['min_confidence'] == 0.7
    assert rule['cooldown_seconds'] == 5
    assert rule['email_recipients'] == ['alerts@example.test']
    assert rule['active_start'] == '07:00'
    assert rule['active_end'] == '18:00'
    assert rule['notify_start'] == '22:00'
    assert rule['notify_end'] == '05:00'


def test_rule_notify_active_now_window(tmp_path, monkeypatch):
    """The email/push window gates only when set, supports midnight wrap, and is
    evaluated in the admin's local timezone."""
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.alert_dispatch as _ad
    # `_rule_notify_active_now` reads `_alert_datetime_prefs` from
    # ``app.alert_dispatch``'s module-global namespace, NOT via main.
    monkeypatch.setattr('app.alert_dispatch._alert_datetime_prefs', lambda: ('UTC', 'iso', '24h'))

    import datetime as _dt
    fixed = _dt.datetime(2026, 1, 1, 23, 30, tzinfo=_dt.timezone.utc)  # 23:30 local

    class _FakeDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed

    # `_rule_notify_active_now` lives at app.alert_dispatch; its `datetime`
    # class is bound there via ``from datetime import datetime``.
    monkeypatch.setattr('app.alert_dispatch.datetime', _FakeDateTime)

    # No window (or partial) means notify any time.
    assert _ad._rule_notify_active_now({}) is True
    assert _ad._rule_notify_active_now({'notify_start': '22:00'}) is True
    # Wrap-past-midnight window that covers 23:30.
    assert _ad._rule_notify_active_now({'notify_start': '22:00', 'notify_end': '05:00'}) is True
    # Same-day window that covers 23:30.
    assert _ad._rule_notify_active_now({'notify_start': '23:00', 'notify_end': '23:59'}) is True
    # Window that excludes 23:30.
    assert _ad._rule_notify_active_now({'notify_start': '06:00', 'notify_end': '18:00'}) is False


def test_zone_object_alert_rules_are_scoped_to_matching_zone(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.zone_schema as _zs
    import app.zone_detection as _zd
    from app.alerts import AlertEngine
    zones = _zs.normalize_monitoring_zones([
        {
            'id': 'porch',
            'name': 'Porch',
            'x': 0,
            'y': 0,
            'width': 0.5,
            'height': 0.5,
            'monitor_motion': False,
            'object_rules': [{'label': 'cat', 'email_enabled': True, 'record_on_detect': False}],
        },
        {
            'id': 'driveway',
            'name': 'Driveway',
            'x': 0.5,
            'y': 0.5,
            'width': 0.5,
            'height': 0.5,
            'monitor_motion': False,
            'object_rules': [{'label': 'cat', 'record_on_detect': True}],
        },
    ])
    settings = {'id': 'front', 'name': 'Front Door', 'detection': {'zones': zones}}
    detections = [
        {'label': 'cat', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}},
        {'label': 'dog', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}},
        {'label': 'cat', 'confidence': 0.9, 'box': {'x': 0.8, 'y': 0.8, 'width': 0.1, 'height': 0.1}},
    ]

    rules = _zd.zone_object_alert_rules(settings)
    alert_detections = _zd.zone_alert_detections(settings, detections)

    assert [rule['name'] for rule in rules] == ['Front Door / Porch / cat']
    assert len(alert_detections) == 1
    assert alert_detections[0]['zone_id'] == 'porch'
    assert alert_detections[0]['box']['x'] == 0.1
    triggered = AlertEngine(rules).process(alert_detections + [{**detections[2], 'zone_id': 'driveway'}])
    assert [alert['rule_name'] for alert in triggered] == ['Front Door / Porch / cat']
    assert _zd.zone_record_on_detect(detections[0], settings) is False
    assert _zd.zone_record_on_detect(detections[2], settings) is True


def test_camera_object_labels_filter_without_monitoring_zones(tmp_path, monkeypatch):
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.zone_detection as _zd
    settings = {'detection': {'object_labels': ['person', 'cat'], 'zones': []}}
    detections = [
        {'label': 'person', 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}},
        {'label': 'suitcase', 'box': {'x': 0.2, 'y': 0.2, 'width': 0.1, 'height': 0.1}},
    ]

    filtered = _zd.filter_detections_for_camera(detections, settings)

    assert [detection['label'] for detection in filtered] == ['person']


def test_object_detection_enabled_flag_gates_object_detections(tmp_path, monkeypatch):
    """Setting object_detection_enabled=False must suppress all object detections."""
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.zone_detection as _zd

    detections = [{'label': 'person', 'confidence': 0.9, 'box': {'x': 0.3, 'y': 0.3, 'width': 0.1, 'height': 0.1}}]

    enabled_settings = {'detection': {'object_detection_enabled': True, 'zones': []}}
    disabled_settings = {'detection': {'object_detection_enabled': False, 'zones': []}}

    assert _zd.filter_detections_for_camera(detections, enabled_settings) == detections
    assert _zd.filter_detections_for_camera(detections, disabled_settings) == []


def test_zone_motion_rule_gates_motion_detections(tmp_path, monkeypatch):
    """Motion is gated per zone: a disabled zone motion rule suppresses motion detections."""
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.zone_schema as _zs
    import app.zone_detection as _zd

    def make_zones(rule_enabled):
        return _zs.normalize_monitoring_zones([
            {'id': 'z1', 'name': 'Zone 1', 'x': 0, 'y': 0, 'width': 1, 'height': 1,
             'monitor_motion': True, 'monitor_objects': False,
             'object_rules': [{'label': 'motion', 'min_confidence': 0.3, 'enabled': rule_enabled}]},
        ])

    enabled_settings = {'detection': {'zones': make_zones(True)}}
    disabled_settings = {'detection': {'zones': make_zones(False)}}

    # High-confidence motion frame
    assert _zd.zone_motion_detections(enabled_settings, frame_motion_confidence=0.9) != []
    assert _zd.zone_motion_detections(disabled_settings, frame_motion_confidence=0.9) == []


def test_legacy_camera_motion_disabled_migrates_to_zone_rules(tmp_path, monkeypatch):
    """Cameras stored with the removed camera-level motion switch off must keep
    motion off after the upgrade by disabling each zone's motion rule."""
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.camera_config as _cc
    import app.zone_detection as _zd

    def legacy_zone():
        return {'id': 'z1', 'name': 'Zone 1', 'x': 0, 'y': 0, 'width': 1, 'height': 1,
                'object_rules': [{'label': 'motion', 'min_confidence': 0.3}]}

    for legacy_detection in (
        {'motion_enabled': False, 'zones': [legacy_zone()]},
        {'motion': {'enabled': False}, 'zones': [legacy_zone()]},
    ):
        camera = _cc.normalize_camera_settings({'id': 'cam-1', 'detection': legacy_detection})
        detection = camera['detection']
        assert 'motion' not in detection
        assert 'motion_enabled' not in detection
        assert detection['zones'][0]['monitor_motion'] is False
        motion_rule = next(r for r in detection['zones'][0]['object_rules'] if r['label'] == 'motion')
        assert motion_rule['enabled'] is False
        assert _zd.zone_motion_detections({'detection': detection}, frame_motion_confidence=0.9) == []

    # Cameras without the legacy switch keep motion governed by the zone rule.
    camera = _cc.normalize_camera_settings({'id': 'cam-2', 'detection': {'zones': [legacy_zone()]}})
    assert camera['detection']['zones'][0]['monitor_motion'] is True


def test_zone_spatial_filtering_blocks_detections_outside_zone(tmp_path, monkeypatch):
    """Objects outside the configured zone area must not trigger alerts."""
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.zone_schema as _zs
    import app.live_monitor as _lm

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [
                # person inside left-half zone (center x=0.2)
                {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.15, 'y': 0.3, 'width': 0.1, 'height': 0.2}},
                # person outside zone (center x=0.75)
                {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.7, 'y': 0.3, 'width': 0.1, 'height': 0.2}},
            ]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main._state.live_detection_last_checked.clear()
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())

    zones = _zs.normalize_monitoring_zones([
        {
            'id': 'left-half',
            'name': 'Left Half',
            'x': 0.0,
            'y': 0.0,
            'width': 0.5,
            'height': 1.0,
            'monitor_motion': False,
            'monitor_objects': True,
            'object_rules': [{'label': 'person', 'alert_on_detect': True, 'record_on_detect': True, 'min_confidence': 0.5}],
        }
    ])
    settings = {
        'id': 'camera-1',
        'name': 'Front Door',
        'detection': {'zones': zones},
    }

    event_id = _lm.process_live_stream_alerts(b'jpeg-frame', {'width': 1280, 'height': 720}, settings)

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert event is not None
    # Only the detection inside the zone should appear in the event
    assert len(event['detections']) == 1
    # Detections are stored flat (x, y, width, height) in the database
    det = event['detections'][0]
    assert det['x'] == pytest.approx(0.15, abs=0.01)


def test_zone_label_aliases_match_configured_rules(tmp_path, monkeypatch):
    """Detection labels that are aliases of a configured rule label should still match."""
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.zone_schema as _zs
    import app.zone_detection as _zd

    zones = _zs.normalize_monitoring_zones([
        {
            'id': 'porch',
            'name': 'Porch',
            'x': 0.0,
            'y': 0.0,
            'width': 1.0,
            'height': 1.0,
            'monitor_motion': False,
            'monitor_objects': True,
            'object_rules': [{'label': 'person', 'email_enabled': True, 'min_confidence': 0.5}],
        }
    ])
    settings = {'detection': {'zones': zones}}

    # A detection with an aliased label ('human' → 'person') should be allowed in the zone
    aliased_detection = {'label': 'human', 'confidence': 0.8, 'box': {'x': 0.3, 'y': 0.3, 'width': 0.1, 'height': 0.1}}
    filtered = _zd.filter_detections_for_camera_zones([aliased_detection], settings, zone_monitor_key='monitor_objects', require_zones=True)
    assert len(filtered) == 1

    # zone_object_rule_matches should also resolve the alias
    matches = _zd.zone_object_rule_matches(settings, aliased_detection, action='alert')
    assert len(matches) == 1
    assert matches[0][1]['label'] == 'person'


def test_detection_has_matching_record_rule(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.zone_detection as _zd

    rules = [
        {'name': 'Person alert', 'object': 'person', 'min_confidence': 0.5, 'enabled': True},
        {'name': 'Dog alert', 'object': 'dog', 'min_confidence': 0.7, 'enabled': True},
        {'name': 'Disabled cat', 'object': 'cat', 'min_confidence': 0.5, 'enabled': False},
        {'name': 'Motion alert', 'object': 'motion', 'min_confidence': 0.3, 'enabled': True},
    ]

    assert _zd.detection_has_matching_record_rule({'label': 'person', 'confidence': 0.8}, rules) is True
    assert _zd.detection_has_matching_record_rule({'label': 'dog', 'confidence': 0.7}, rules) is True
    assert _zd.detection_has_matching_record_rule({'label': 'dog', 'confidence': 0.69}, rules) is False
    assert _zd.detection_has_matching_record_rule({'label': 'car', 'confidence': 0.9}, rules) is False
    assert _zd.detection_has_matching_record_rule({'label': 'cat', 'confidence': 0.9}, rules) is False
    assert _zd.detection_has_matching_record_rule({'label': 'human', 'confidence': 0.8}, rules) is True
    assert _zd.detection_has_matching_record_rule({'label': 'motion', 'confidence': 0.4}, rules) is True
    assert _zd.detection_has_matching_record_rule({'label': 'motion', 'confidence': 0.1}, rules) is False
    assert _zd.detection_has_matching_record_rule({'label': '', 'confidence': 0.9}, rules) is False


def test_record_only_zone_rule_detection_creates_event_and_recording(tmp_path, monkeypatch):
    """Cat with record_on_detect=True but no email/push (record only) must not be silently
    dropped when another label has an alert rule (which makes zone_rules non-empty and triggers
    zone_alert_detections filtering)."""
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [{'label': 'cat', 'confidence': 0.88, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    main._state.live_detection_last_checked.clear()

    # Camera has a zone covering the whole frame:
    # - cat rule: record_on_detect=True, no email/push (record only, no alert)
    # - person rule: record_on_detect=False, email_enabled=True (alert only)
    # The person alert rule makes zone_rules non-empty, which used to cause zone_alert_detections
    # to filter out the cat entirely (no alert rule for cat).
    event_id = _lm.process_live_stream_alerts(
        b'cat-frame',
        {'width': 1280, 'height': 720},
        {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {
                'zones': [
                    {
                        'id': 'porch',
                        'name': 'Porch',
                        'x': 0, 'y': 0, 'width': 1, 'height': 1,
                        'monitor_motion': False,
                        'monitor_objects': True,
                        'object_rules': [
                            {'label': 'cat', 'record_on_detect': True, 'min_confidence': 0.5},
                            {'label': 'person', 'record_on_detect': False, 'email_enabled': True, 'min_confidence': 0.5},
                        ],
                    },
                ],
            },
            'recording': {'continuous': False},
        },
        enforce_interval=False,
    )

    assert event_id is not None, "Event must be created for record-only zone detection"
    event = main.database.get_event(event_id)
    assert any(d['label'] == 'cat' for d in event['detections']), "Cat must appear in event detections"
    assert any(d['label'] == 'cat' and d['zone_name'] == 'Porch' for d in event['detections']), "Cat detection must keep its zone name"
    assert event['recording_status'] == 'linked', "Recording must be linked for record-only zone rule"


def test_record_only_zone_with_no_alert_rules_keeps_zone_name(tmp_path, monkeypatch):
    """A zone whose only rule is record-only (no email/push, so it raises no alert)
    must still tag its detections with the zone name. Regression: zone-name
    annotation must key off the presence of object zones, not off whether any
    rule raises an alert."""
    _app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return [{'label': 'person', 'confidence': 0.82, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'models/fake.onnx', 'labels_path': 'models/coco.names'}, main.utc_now())
    main._state.live_detection_last_checked.clear()

    event_id = _lm.process_live_stream_alerts(
        b'person-frame',
        {'width': 1280, 'height': 720},
        {
            'id': 'camera-1',
            'name': 'Driveway',
            'detection': {
                'zones': [
                    {
                        'id': 'driveway', 'name': 'Driveway (Full)',
                        'x': 0, 'y': 0, 'width': 1, 'height': 1,
                        'monitor_motion': False, 'monitor_objects': True,
                        # Record only: no email_enabled / push_enabled, so no alert is raised.
                        'object_rules': [{'label': 'person', 'record_on_detect': True, 'min_confidence': 0.5}],
                    },
                ],
            },
            'recording': {'continuous': False},
        },
        enforce_interval=False,
    )

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert any(d['label'] == 'person' and d['zone_name'] == 'Driveway (Full)' for d in event['detections']), \
        'record-only zone detection must keep its zone name'
    # No alert should be raised because the rule has neither email nor push enabled.
    assert not any(a['label'] == 'person' for a in main.database.alerts(limit=10))


@pytest.mark.parametrize('label,confidence,box', [
    ('person', 0.91, {'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.5}),
    ('cat', 0.82, {'x': 0.2, 'y': 0.2, 'width': 0.3, 'height': 0.3}),
])
def test_zone_detection_creates_alert_and_recording(tmp_path, monkeypatch, label, confidence, box):
    """A detection inside a zone with a matching alert+record rule must produce a
    saved event with recording_status='linked' and an alert history entry."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, **kwargs):
            return [{'label': label, 'confidence': confidence, 'box': box}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    main._state.live_detection_last_checked.clear()
    main.alerts.last_triggered.clear()

    settings = _zone_camera_settings([
        {'label': label, 'record_on_detect': True, 'email_enabled': True, 'min_confidence': 0.5, 'cooldown_seconds': 0},
    ])
    event_id = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)

    assert event_id is not None
    event = main.database.get_event(event_id)
    assert any(d['label'] == label for d in event['detections'])
    assert any(d['label'] == label and d['zone_name'] == 'Full Frame' for d in event['detections'])
    assert event['recording_status'] == 'linked'
    assert event['recordings'][0]['trigger_label'] == label
    alerts = main.database.alerts(limit=10)
    assert any(a['label'] == label for a in alerts)


def test_person_and_cat_in_zone_each_create_independent_events(tmp_path, monkeypatch):
    """Two successive detections - first person, then cat - in the same zone each produce
    their own event and recording when both have zero cooldown."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm

    # Key detections off the frame bytes rather than call order, so each live
    # call answers deterministically for its own frame regardless of how many
    # times the detector is invoked.
    labels_by_frame = {
        b'frame1': [{'label': 'person', 'confidence': 0.90, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.5}}],
        b'frame2': [{'label': 'cat',    'confidence': 0.85, 'box': {'x': 0.5, 'y': 0.4, 'width': 0.2, 'height': 0.2}}],
    }

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image_bytes, confidence=None):
            return labels_by_frame.get(image_bytes, [])

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    main._state.live_detection_last_checked.clear()
    main.alerts.last_triggered.clear()

    settings = _zone_camera_settings([
        {'label': 'person', 'record_on_detect': True, 'alert_on_detect': True, 'min_confidence': 0.5, 'cooldown_seconds': 0},
        {'label': 'cat',    'record_on_detect': True, 'alert_on_detect': True, 'min_confidence': 0.5, 'cooldown_seconds': 0},
    ])

    person_event_id = _lm.process_live_stream_alerts(b'frame1', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    cat_event_id    = _lm.process_live_stream_alerts(b'frame2', {'width': 1280, 'height': 720}, settings, enforce_interval=False)

    assert person_event_id is not None
    assert cat_event_id is not None
    assert person_event_id != cat_event_id

    person_event = main.database.get_event(person_event_id)
    cat_event    = main.database.get_event(cat_event_id)
    assert any(d['label'] == 'person' for d in person_event['detections'])
    assert any(d['label'] == 'cat'    for d in cat_event['detections'])
    assert person_event['recording_status'] == 'linked'
    assert cat_event['recording_status']    == 'linked'
    assert person_event['recordings'][0]['trigger_label'] == 'person'
    assert cat_event['recordings'][0]['trigger_label']    == 'cat'


def test_alert_cooldown_is_scoped_by_internal_key_even_when_rule_names_match():
    from app.alerts import AlertEngine

    engine = AlertEngine([])
    detection = {
        'label': 'cat',
        'confidence': 0.92,
        'zone_id': 'full-frame',
    }
    base_rule = {
        'name': 'Front Door / Full Frame / cat',
        'object': 'cat',
        'zone_id': 'full-frame',
        'min_confidence': 0.5,
        'cooldown_seconds': 60,
        'enabled': True,
    }

    first = engine.process([detection], rules=[{**base_rule, 'cooldown_key': 'front-yard::full-frame::cat'}])
    second = engine.process([detection], rules=[{**base_rule, 'cooldown_key': 'driveway::full-frame::cat'}])
    repeated_first = engine.process([detection], rules=[{**base_rule, 'cooldown_key': 'front-yard::full-frame::cat'}])

    assert len(first) == 1
    assert len(second) == 1
    assert repeated_first == []


def test_alert_engine_respects_rule_max_confidence_upper_bound():
    """The AlertEngine gates on the per-rule confidence window: a detection
    above ``max_confidence`` does not fire even though it clears
    ``min_confidence``. Rules without ``max_confidence`` keep no upper limit."""
    from app.alerts import AlertEngine

    engine = AlertEngine([])
    rule = {
        'name': 'Front Door / Full Frame / cat',
        'object': 'cat',
        'zone_id': 'full-frame',
        'min_confidence': 0.4,
        'max_confidence': 0.7,
        'cooldown_seconds': 0,
        'enabled': True,
    }
    over = {'label': 'cat', 'confidence': 0.92, 'zone_id': 'full-frame'}
    inside = {'label': 'cat', 'confidence': 0.6, 'zone_id': 'full-frame'}

    assert engine.process([over], rules=[rule]) == []
    assert len(engine.process([inside], rules=[rule])) == 1

    # No max_confidence -> the 0.92 detection fires (upper bound defaults to 1.0).
    no_cap = {k: v for k, v in rule.items() if k != 'max_confidence'}
    assert len(engine.process([over], rules=[no_cap])) == 1


def test_alert_engine_group_rule_matches_member_labels():
    """An umbrella group rule (``animal``/``pet``) fires for any member label
    while a concrete rule still matches only its own label -- so adding groups
    never changes the behavior of existing per-label rules."""
    from app.alerts import AlertEngine

    engine = AlertEngine([])
    animal_rule = {
        'name': 'Garden / Full Frame / animal',
        'object': 'animal',
        'zone_id': 'full-frame',
        'min_confidence': 0.3,
        'cooldown_seconds': 0,
        'enabled': True,
    }
    # A cat and a dog both satisfy the ``animal`` group rule...
    assert len(engine.process([{'label': 'cat', 'confidence': 0.8, 'zone_id': 'full-frame'}], rules=[animal_rule])) == 1
    assert len(engine.process([{'label': 'dog', 'confidence': 0.8, 'zone_id': 'full-frame'}], rules=[animal_rule])) == 1
    # ...but a person (not in the animal group) does not.
    assert engine.process([{'label': 'person', 'confidence': 0.8, 'zone_id': 'full-frame'}], rules=[animal_rule]) == []

    # A concrete ``cat`` rule must NOT fire for a dog (regression guard: group
    # expansion is one-directional, only when the CONFIGURED label is a group).
    cat_rule = {**animal_rule, 'name': 'Garden / Full Frame / cat', 'object': 'cat'}
    assert engine.process([{'label': 'dog', 'confidence': 0.9, 'zone_id': 'full-frame'}], rules=[cat_rule]) == []
    assert len(engine.process([{'label': 'cat', 'confidence': 0.9, 'zone_id': 'full-frame'}], rules=[cat_rule])) == 1


def test_coco_labels_load_person_and_cat_at_correct_indices(tmp_path):
    """Verify coco.names resolves COCO class IDs 0→'person' and 15→'cat'."""
    from app.detector import load_labels
    labels = load_labels('models/coco.names')
    assert len(labels) >= 80, "coco.names must contain at least 80 labels"
    assert labels[0] == 'person', "COCO class 0 must be 'person'"
    assert labels[15] == 'cat',   "COCO class 15 must be 'cat'"


def test_object_outside_zone_does_not_create_recording(tmp_path, monkeypatch):
    """A person detected entirely outside the configured zone must not create a recording."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            # Object is in the right half of the frame (x=0.6..0.9)
            return [{'label': 'person', 'confidence': 0.88, 'box': {'x': 0.6, 'y': 0.1, 'width': 0.3, 'height': 0.5}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    main._state.live_detection_last_checked.clear()

    # Zone covers only the left half of the frame
    settings = {
        'id': 'camera-1',
        'name': 'Front Door',
        'detection': {
            'zones': [
                {
                    'id': 'left-half',
                    'name': 'Left Half',
                    'x': 0, 'y': 0, 'width': 0.5, 'height': 1,
                    'monitor_motion': False,
                    'monitor_objects': True,
                    'object_rules': [
                        {'label': 'person', 'record_on_detect': True, 'alert_on_detect': True, 'min_confidence': 0.5},
                    ],
                },
            ],
        },
        'recording': {'continuous': False},
    }
    event_id = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)

    assert event_id is None, "Person outside the zone must not produce any event"


@pytest.mark.parametrize('zone_rules,global_conf,expected', [
    # No zone rules -> falls back to global AI confidence
    (None, 0.62, 0.62),
    # Global confidence 0 is a REAL persisted value ('accept everything'),
    # not 'unset' -- the fallback uses an explicit None check so 0 survives
    # the old ``or 0.45`` truthiness trap (pins the ONNX Min Confidence
    # slider's floor behaviour end to end).
    (None, 0.0, 0.0),
    # Zone with person rule at 0.35 -> uses lowest rule confidence
    ([{'label': 'person', 'min_confidence': 0.35, 'record_on_detect': True, 'alert_on_detect': True, 'cooldown_seconds': 60}], 0.5, 0.35),
    # Zone with motion rule at 0.1 -> motion rule ignored, falls back to global
    ([{'label': 'motion', 'min_confidence': 0.1, 'record_on_detect': True, 'alert_on_detect': True, 'cooldown_seconds': 60},
      {'label': 'person', 'min_confidence': 0.45, 'record_on_detect': True, 'alert_on_detect': True, 'cooldown_seconds': 60}], 0.5, 0.45),
])
def test_compute_minimum_rule_confidence(tmp_path, monkeypatch, zone_rules, global_conf, expected):
    """compute_minimum_rule_confidence returns the lowest enabled object rule's
    min_confidence, falling back to the global AI confidence when no object rule
    is lower. Motion rules are excluded from the floor calculation."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    # Phase-N migrated _min_rule_confidence_cache to app.alert_dispatch; write
    # through the canonical module path so the test actually invalidates the
    # production cache (production does the same in app/main.py via
    # `_alert_dispatch._min_rule_confidence_cache = None`). Also keeps
    # test_api_router_split_invariants green (no main.<attr> reference).
    import app.alert_dispatch as _alert_dispatch

    main.database.set_setting('ai', {'backend': 'onnx', 'confidence': global_conf, 'model_path': 'fake.onnx'}, main.utc_now())
    _alert_dispatch._min_rule_confidence_cache = None

    if zone_rules is not None:
        main.database.set_setting('cameras', [
            {'id': 'camera-1', 'backend': 'onvif', 'stream_url': 'rtsp://127.0.0.1:554/stream',
             'detection': {
                 'object_labels': ['person', 'cat'],
                 'zones': [{'id': 'test', 'name': 'Test', 'x': 0, 'y': 0, 'width': 1, 'height': 1,
                            'monitor_motion': True, 'monitor_objects': True, 'object_rules': zone_rules}],
             }},
        ], main.utc_now())
    else:
        main.database.set_setting('cameras', [], main.utc_now())

    _alert_dispatch._min_rule_confidence_cache = None
    assert _alert_dispatch.compute_minimum_rule_confidence() == pytest.approx(expected)


def test_motion_debounce_trailing_window_and_independent_recording(tmp_path, monkeypatch):
    """Motion uses a short trailing suppression window after non-motion events.

    Within _MOTION_TRAILING_SUPPRESSION_SECONDS of a prior object/sound event,
    motion is suppressed (background re-settling noise). Beyond that window it
    fires independently. Motion after a prior motion event always uses the normal
    label-overlap path so back-to-back motion events still merge correctly.
    """
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.event_debounce as _ed

    # Motion within the trailing window after a non-motion event → suppressed.
    main._state.live_event_last_emitted.clear()
    _ed.remember_live_event('camera-1', {'person'})
    assert _ed.live_event_is_debounced('camera-1', {'motion'}, 60.0) is True

    # Simulate elapsed time beyond the trailing window - motion records independently.
    with main._state.live_event_last_emitted_lock:
        main._state.live_event_last_emitted['camera-1']['timestamp'] = (
            time.time() - _ed._MOTION_TRAILING_SUPPRESSION_SECONDS - 1
        )
    assert _ed.live_event_is_debounced('camera-1', {'motion'}, 60.0) is False

    # A concrete object never uses the trailing path - label overlap decides.
    _ed.remember_live_event('camera-1', {'person'})
    assert _ed.live_event_is_debounced('camera-1', {'cat'}, 10.0) is False

    # Motion after a prior motion event IS debounced via normal label overlap.
    main._state.live_event_last_emitted.clear()
    _ed.remember_live_event('camera-1', {'motion'})
    assert _ed.live_event_is_debounced('camera-1', {'motion'}, 10.0) is True


def test_debounce_window_refreshes_while_activity_continues(tmp_path, monkeypatch):
    """Continuing detections must refresh the debounce window so a new event/recording
    requires a quiet gap, instead of re-firing every debounce_seconds while the same
    activity persists (which produced back-to-back duplicate recordings)."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return [{'label': 'person', 'confidence': 0.91, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.5}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    main._state.live_detection_last_checked.clear()
    main._state.live_event_last_emitted.clear()
    main.alerts.last_triggered.clear()

    settings = _zone_camera_settings([
        {'label': 'person', 'record_on_detect': True, 'alert_on_detect': True, 'min_confidence': 0.5, 'cooldown_seconds': 30},
    ])

    first_event = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert first_event is not None

    # Simulate the original event being 25s old (still inside the 30s window) when
    # another scan sees the same person: it must be suppressed AND refresh the window.
    main._state.live_event_last_emitted['camera-1']['timestamp'] = time.time() - 25
    suppressed = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert suppressed is None
    refreshed_ts = main._state.live_event_last_emitted['camera-1']['timestamp']
    assert time.time() - refreshed_ts < 5, 'suppressed detection must refresh the debounce window'

    # 25s later again (would be 50s after the original event - past the old anchor)
    # the same ongoing activity must STILL be suppressed thanks to the refresh.
    main._state.live_event_last_emitted['camera-1']['timestamp'] = time.time() - 25
    still_suppressed = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert still_suppressed is None

    # Only after a quiet gap longer than the window does a new event get created.
    main._state.live_event_last_emitted['camera-1']['timestamp'] = time.time() - 31
    new_event = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert new_event is not None
    assert new_event != first_event


def test_alert_only_event_is_debounced_without_recording(tmp_path, monkeypatch):
    """A camera whose alert rules match but whose record rules don't (or that has
    recording off) must still be throttled by the debounce window. Previously the
    debounce gate only ran when a recording attached, so an alert-only camera
    created a fresh event + snapshot on every detection cycle (~4 Hz), flooding
    the timeline with duplicates of the same activity."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return [{'label': 'person', 'confidence': 0.91, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.5}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    main._state.live_detection_last_checked.clear()
    main._state.live_event_last_emitted.clear()
    main.alerts.last_triggered.clear()

    # Alert rule with NO record_on_detect: the detection fires the alert path but
    # never attaches a recording, so should_record_event is False.
    settings = _zone_camera_settings([
        {'label': 'person', 'email_enabled': True, 'min_confidence': 0.5, 'cooldown_seconds': 30},
    ])

    first_event = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert first_event is not None

    # The same activity inside the 30s window: no recording ever attached, but
    # the event must still be suppressed so the timeline doesn't flood.
    suppressed = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert suppressed is None

    events = main.database.search_events(limit=50)
    assert len(events) == 1, 'alert-only activity must be debounced to one event per window'
