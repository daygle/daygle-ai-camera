"""API integration tests: email and ntfy push alert delivery.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_admin_can_send_test_alert_email(tmp_path, monkeypatch):
    sent: list[tuple[dict[str, object], str]] = []

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_test(self, recipient):
            sent.append((self.settings, recipient))

    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        # Patch on the handler module's actual import namespace
        # (app.api.alert_email_router). `from app.email_alerts import
        # EmailAlertService` creates a module-level binding there at import
        # time, and the handler's bare-name lookup reads from THAT
        # module's globals. Patching on app.email_alerts alone would
        # rebind a different binding the handler never reads.
        import app.api.alert_email_router as alert_email_router_module
        monkeypatch.setattr(alert_email_router_module, "EmailAlertService", FakeEmailAlertService)

        status, _headers, payload = client.request(
            "/api/settings/alert-email/test",
            method="POST",
            json_body={
                "settings": {
                    "enabled": True,
                    "host": "smtp.example.com",
                    "port": 587,
                    "from_address": "alerts@example.com",
                    "use_tls": True,
                    "use_ssl": False,
                },
                "recipient": "admin@example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )

        assert status == 200
        assert payload == {"ok": True, "recipient": "admin@example.com"}
        assert sent == [(
            {
                "enabled": True,
                "host": "smtp.example.com",
                "port": 587,
                "username": "",
                "password": "",
                "from_address": "alerts@example.com",
                "use_tls": True,
                "use_ssl": False,
            },
            "admin@example.com",
        )]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_push_notification_title_lists_all_triggered_labels(monkeypatch):
    """A cat+person event must produce TWO push notifications (one per matching
    rule), each with the title "Daygle AI Camera alert: Cat, Person Detected" and a body
    that lists every triggered label."""
    from app.push_notifications import PushNotificationService
    import urllib.request

    captured: list[dict] = []

    class FakeResponse:
        def __init__(self, *_a, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    def fake_urlopen(request, timeout=10):
        captured.append({
            'url': request.full_url,
            'title': request.headers.get('Title'),
            'body': request.data.decode('utf-8') if request.data else '',
        })
        return FakeResponse()

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    service = PushNotificationService({
        'enabled': True,
        'server_url': 'https://ntfy.sh',
        'topic': 'daygle-test',
    })

    all_triggered_labels = ['cat', 'person']
    for label in all_triggered_labels:
        service.send_alert(
            {'label': label, 'rule_name': f'{label.title()} alert', 'confidence': 0.9,
             'message': f'{label.title()} matched'},
            event_id=42,
            camera_name='Front Door',
            triggered_labels=all_triggered_labels,
        )

    assert len(captured) == 2, 'expected one push per matching rule'
    for entry in captured:
        assert entry['title'] == 'Daygle AI Camera alert: Cat, Person Detected'
        assert 'All triggers: Cat, Person' in entry['body']
        assert 'Camera: Front Door' in entry['body']
        assert 'Detection Type: Object' in entry['body']
        assert 'Rule:' in entry['body']
        assert 'Object - ' not in entry['body']
        assert 'Detected at:' not in entry['body']


def test_deliver_push_notifications_passes_all_triggered_labels(tmp_path, monkeypatch):
    _app, _ = _load_app(tmp_path, monkeypatch)
    main_module = sys.modules["app.main"]
    captured: list[dict[str, object]] = []

    class FakePushNotificationService:
        def __init__(self, settings):
            self.settings = settings

        def send_alert(
            self,
            alert,
            *,
            event_id,
            camera_name=None,
            camera_id=None,
            triggered_labels=None,
            detected_at=None,
        ):
            captured.append({
                'alert': alert,
                'event_id': event_id,
                'camera_name': camera_name,
                'camera_id': camera_id,
                'triggered_labels': triggered_labels,
                'detected_at': detected_at,
            })

    import app.alert_dispatch as _ad
    monkeypatch.setattr(_ad, 'effective_push_notification_settings', lambda: {'enabled': True})
    monkeypatch.setattr(
        main_module.database,
        'get_event',
        lambda _event_id: {'metadata': {'camera_name': 'Front Door', 'camera_id': 'front'}},
    )
    monkeypatch.setattr(_ad, 'PushNotificationService', FakePushNotificationService)

    triggered = [
        {'label': 'cat', 'rule_name': 'Cat alert', 'confidence': 0.9, 'message': 'Cat matched'},
        {'label': 'person', 'rule_name': 'Person alert', 'confidence': 0.8, 'message': 'Person matched'},
    ]
    rules = [
        {'name': 'Cat alert', 'push_enabled': True},
        {'name': 'Person alert', 'push_enabled': True},
    ]

    import app.alert_dispatch as _ad2
    _ad2.deliver_push_notifications(triggered, 42, rules=rules)

    assert len(captured) == 2
    assert [entry['triggered_labels'] for entry in captured] == [['cat', 'person'], ['cat', 'person']]
    assert {entry['camera_name'] for entry in captured} == {'Front Door'}
    assert {entry['camera_id'] for entry in captured} == {'front'}


def test_email_alert_subject_lists_all_triggered_labels():
    """A single event whose detections include both cat and person must produce
    TWO alert emails (one per rule), each citing "Cat, Person detected" in the
    subject and body, so recipients see the full label set at a glance.
    """
    from app.email_alerts import EmailAlertService
    import smtplib

    sent_messages: list = []

    class FakeSMTP:
        def __init__(self, *_a, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def starttls(self): pass
        def login(self, *_a, **_k): pass
        def send_message(self, message): sent_messages.append(message)

    _original_smtp = smtplib.SMTP
    _original_smtp_ssl = smtplib.SMTP_SSL
    try:
        smtplib.SMTP = FakeSMTP
        smtplib.SMTP_SSL = FakeSMTP

        service = EmailAlertService({
            'enabled': True,
            'host': 'smtp.example.test',
            'port': 587,
            'from_address': 'alerts@example.test',
            'use_tls': True,
            'use_ssl': False,
        })

        all_triggered_labels = ['cat', 'person']
        # Two rules, two alerts - one per label - both with email enabled.
        for label in all_triggered_labels:
            service.send_alert(
                {'label': label, 'rule_name': f'{label.title()} alert', 'confidence': 0.9,
                 'message': f'{label.title()} matched'},
                event_id=42,
                recipients=['owner@example.test'],
                camera_name='Front Door',
                triggered_labels=all_triggered_labels,
            )

        assert len(sent_messages) == 2, 'expected one email per matching rule'
        for message in sent_messages:
            assert message['Subject'] == 'Daygle AI Camera Alert: Cat, Person Detected (Front Door)'
            # Walk the multipart tree to find the html part. get_payload() may
            # return a flat list of parts (multipart/alternative) or a nested
            # Message with its own walk() (multipart/related).
            def _iter_parts(message):
                payload = message.get_payload()
                if isinstance(payload, list):
                    for part in payload:
                        yield from _iter_parts(part)
                else:
                    yield message
            html_part = None
            for part in _iter_parts(message):
                if part.get_content_type() == 'text/html':
                    html_part = part.get_payload(decode=True).decode('utf-8', 'ignore')
                    break
            assert html_part is not None, 'expected an html part'
            assert 'Cat, Person' in html_part, 'html body must list every triggered label'
            assert 'All triggers' in html_part, 'html body must include an All triggers row'
            assert 'Detection Type' in html_part, 'html body must include a Detection Type row'
            assert 'Object' in html_part, 'html body must show Object detection type'
            assert 'Rule' in html_part, 'html body must include a Rule row'
            assert 'Object - ' not in html_part, 'Rule row must not include the old Object prefix'
    finally:
        smtplib.SMTP = _original_smtp
        smtplib.SMTP_SSL = _original_smtp_ssl


@pytest.mark.parametrize('label', ['person', 'cat'])
def test_object_detection_with_email_rule_delivers_email(tmp_path, monkeypatch, label):
    """A person/cat detected in a zone whose rule has email_enabled and a recipient must
    deliver an email to that recipient with the object in the subject line.

    This locks in the end-to-end alerting goal: object detected in footage -> email sent.
    """
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm
    import app.alert_dispatch as _ad

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return [{'label': label, 'confidence': 0.9, 'box': {'x': 0.2, 'y': 0.2, 'width': 0.2, 'height': 0.2}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    main._state.live_detection_last_checked.clear()
    main.alerts.last_triggered.clear()

    sent = _email_alert_capture(main, monkeypatch)
    settings = _zone_camera_settings_with_email(label)
    event_id = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    _ad.wait_for_pending_alert_notifications()

    assert event_id is not None
    assert len(sent) == 1, f'exactly one email should be sent for a {label} detection'
    assert sent[0]['To'] == 'glenbday82@gmail.com'
    assert label in sent[0]['Subject'].lower()


def test_object_detection_with_email_rule_delivers_one_envelope_per_recipient(tmp_path, monkeypatch):
    """Loop-send regression net: an alert rule listing N recipients must
    trigger N one-to-one envelopes, each with a single To: header. The
    headers must never carry a multi-address To so subscribers do not see
    each other's email addresses.
    """
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm
    import app.alert_dispatch as _ad

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return [{'label': 'cat', 'confidence': 0.9, 'box': {'x': 0.2, 'y': 0.2, 'width': 0.2, 'height': 0.2}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    main._state.live_detection_last_checked.clear()
    main.alerts.last_triggered.clear()

    sent = _email_alert_capture(main, monkeypatch)
    settings = _zone_camera_settings_with_email('cat')
    # Override recipients on the nested rule so the alert fans out to 2 addresses.
    settings['detection']['zones'][0]['object_rules'][0]['email_recipients'] = ['alice@example.com', 'bob@example.com']
    event_id = _lm.process_live_stream_alerts(
        b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False,
    )
    _ad.wait_for_pending_alert_notifications()

    assert event_id is not None
    assert len(sent) == 2, 'one envelope per recipient, never a multi-recipient To'
    assert [entry['To'] for entry in sent] == ['alice@example.com', 'bob@example.com']
    assert 'cat' in sent[0]['Subject'].lower()


def test_sound_rule_normalization_keeps_email_recipients_and_active_window(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.camera_config as _cc

    camera = _cc.normalize_camera_settings({
        'id': 'sound-cam',
        'detection': {
            'sound': {
                'enabled': True,
                'rules': [{
                    'class': 'cat_meow',
                    'enabled': True,
                    'email_enabled': True,
                    'email_recipients': 'alerts@example.test, bad-address',
                    'active_start': '07:00',
                    'active_end': '18:00',
                    'notify_start': '22:00',
                    'notify_end': '05:00',
                }],
            },
        },
    })

    rule = camera['detection']['sound']['rules'][0]
    assert rule['email_enabled'] is True
    assert rule['email_recipients'] == ['alerts@example.test']
    assert rule['active_start'] == '07:00'
    assert rule['active_end'] == '18:00'
    assert rule['notify_start'] == '22:00'
    assert rule['notify_end'] == '05:00'


def test_sound_detection_with_email_rule_delivers_to_rule_recipients(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.sound_monitor as _sm
    import app.alert_dispatch as _ad
    # ``_on_sound_detected`` reads cameras from the canonical
    # ``_state.cameras_config`` (see app/sound_monitor.py:_on_sound_detected).

    sent = _email_alert_capture(main, monkeypatch)
    camera = {
        'id': 'sound-cam',
        'name': 'Sound Camera',
        'detection': {
            'sound': {
                'enabled': True,
                'rules': [{
                    'class': 'cat_meow',
                    'name': 'Cat meow alert',
                    'enabled': True,
                    'record_on_detect': False,
                    'email_enabled': True,
                    'email_recipients': ['alerts@example.test'],
                    'push_enabled': False,
                }],
            },
        },
    }
    # Patch the canonical store directly (Option C retarget, matches
    # ``test_detection_backoff_keeps_prebuffer_warm`` at line ~409).
    monkeypatch.setattr(main._state, 'cameras_config', [camera])

    _sm._on_sound_detected('sound-cam', 'cat_meow', 'Cat meow alert', 0.92, {'backend': 'test'})
    _ad.wait_for_pending_alert_notifications()

    assert len(sent) == 1
    assert sent[0]['To'] == 'alerts@example.test'
    assert 'Cat Meow' in sent[0]['Subject']


def test_object_detection_without_global_email_enabled_sends_nothing(tmp_path, monkeypatch):
    """A per-rule email_enabled flag must not deliver mail when global SMTP is disabled.

    EmailAlertService.configured() gates on the global settings, so the event/alert are
    still recorded but no message is delivered. This guards against silently emailing when
    the operator has not finished SMTP setup."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm
    import app.alert_dispatch as _ad

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return [{'label': 'cat', 'confidence': 0.9, 'box': {'x': 0.2, 'y': 0.2, 'width': 0.2, 'height': 0.2}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    main._state.live_detection_last_checked.clear()
    main.alerts.last_triggered.clear()

    # Global email left disabled; only the per-rule flag is on.
    main.database.set_setting(
        'alert_email',
        {'enabled': False, 'host': '', 'from_address': '', 'port': 587, 'use_tls': True, 'use_ssl': False},
        main.utc_now(),
    )
    delivered: list[object] = []
    monkeypatch.setattr(main.EmailAlertService, '_deliver', lambda self, message: delivered.append(message))

    settings = _zone_camera_settings_with_email('cat')
    event_id = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    _ad.wait_for_pending_alert_notifications()

    assert event_id is not None, 'event/alert should still be recorded even without email configured'
    assert delivered == [], 'no email should be delivered while global SMTP is disabled'


def test_alert_email_settings_get_and_update(tmp_path, monkeypatch):
    """GET returns current email alert settings; PUT updates and persists them."""
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, settings = client.request("/api/settings/alert-email")
        assert status == 200
        expected_keys = {"enabled", "host", "port", "username", "password", "from_address", "use_tls", "use_ssl"}
        assert expected_keys <= set(settings)
        status, _headers, updated = client.request(
            "/api/settings/alert-email",
            method="PUT",
            json_body={"enabled": False, "host": "smtp.example.com", "port": 587, "from_address": "alerts@example.com"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert updated["enabled"] is False
        assert updated["host"] == "smtp.example.com"
        assert updated["from_address"] == "alerts@example.com"
        import sqlite3
        with sqlite3.connect(database_path) as db:
            row = db.execute("SELECT value FROM app_settings WHERE key = 'alert_email'").fetchone()
        assert row is not None
        saved = json.loads(row[0])
        assert saved["host"] == "smtp.example.com"
        assert saved["enabled"] is False
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_push_notification_settings_get_and_update(tmp_path, monkeypatch):
    """GET returns current push notification settings; PUT updates and persists them."""
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, settings = client.request("/api/settings/alert-push")
        assert status == 200
        expected_keys = {"enabled", "server_url", "topic", "priority", "username", "password"}
        assert expected_keys <= set(settings)
        status, _headers, updated = client.request(
            "/api/settings/alert-push",
            method="PUT",
            json_body={"enabled": True, "server_url": "https://ntfy.sh", "topic": "daygle-test", "priority": "default"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert updated["enabled"] is True
        assert updated["server_url"] == "https://ntfy.sh"
        assert updated["topic"] == "daygle-test"
        import sqlite3
        with sqlite3.connect(database_path) as db:
            row = db.execute("SELECT value FROM app_settings WHERE key = 'alert_push'").fetchone()
        assert row is not None
        saved = json.loads(row[0])
        assert saved["topic"] == "daygle-test"
        assert saved["enabled"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_push_notification_test_endpoint(tmp_path, monkeypatch):
    """POST /api/settings/alert-push/test sends a test push notification."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        import app.main as main_module
        sent = []
        monkeypatch.setattr(main_module.PushNotificationService, "send_test", lambda self: sent.append(self.settings))
        status, _headers, payload = client.request(
            "/api/settings/alert-push/test",
            method="POST",
            json_body={
                "settings": {
                    "enabled": True,
                    "server_url": "https://ntfy.sh",
                    "topic": "daygle-test",
                    "priority": "default",
                },
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload == {"ok": True}
        assert len(sent) == 1
        assert sent[0]["topic"] == "daygle-test"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_redact_password_for_viewer_on_alert_settings(tmp_path, monkeypatch):
    """Viewer can read alert-email/alert-push settings but does NOT see the password.
    Admin sees the full dict for round-trip through PUT.

    Closes the security gap the 40fc988 admin gate was reaching for via the
    right mechanism: server-side redaction rather than blanket-rejecting the
    GET (which broke ``test_admin_ai_settings_viewer_denied_and_db_override``).
    See ``app.deps.get_redacted_email_alert_settings`` and
    ``app.deps.get_redacted_push_notification_settings`` for the deps that
    drive this behavior.
    """
    endpoints = [
        (
            "/api/settings/alert-email",
            {
                "enabled": True,
                "host": "smtp.example.com",
                "port": 587,
                "from_address": "alerts@example.com",
                "password": "admin-test-password",
            },
        ),
        (
            "/api/settings/alert-push",
            {
                "enabled": True,
                "server_url": "https://ntfy.sh",
                "topic": "daygle-test",
                "priority": "default",
                "password": "admin-test-password",
            },
        ),
    ]
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        for endpoint, put_payload in endpoints:
            status, _headers, _body = client.request(
                endpoint,
                method="PUT",
                json_body=put_payload,
                headers={"X-CSRF-Token": csrf},
            )
            assert status == 200, f"PUT {endpoint} expected 200, got {status}"
            status, _headers, admin_settings = client.request(endpoint)
            assert status == 200, f"admin GET {endpoint} expected 200, got {status}"
            assert admin_settings.get("password") == "admin-test-password", (
                f"admin {endpoint}: expected password present, got {admin_settings.get('password')!r}"
            )
        # Now create viewer + verify redaction on both endpoints.
        status, _headers, viewer = client.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer-redact", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200, f"viewer create expected 200, got {status}"
        viewer_client = LocalClient(base_url)
        _login(viewer_client, viewer["username"], "Viewer123!")
        for endpoint, _put_payload in endpoints:
            status, _headers, viewer_settings = viewer_client.request(endpoint)
            assert status == 200, f"viewer GET {endpoint} expected 200, got {status}"
            assert "password" not in viewer_settings, (
                f"viewer {endpoint}: expected password stripped, got {viewer_settings.get('password')!r}"
            )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
