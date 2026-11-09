"""Alert recipients that reference users (``user:<id>``) are resolved to the
user's current email address when the alert is sent."""
import sys

from tests.support import _load_app


def test_normalize_email_recipients_keeps_user_references():
    from app.utils import normalize_email_recipients

    assert normalize_email_recipients(
        ['User:7', 'a@example.com', 'user:7', 'not-an-address', 'user:x', ' A@example.com ']
    ) == ['user:7', 'a@example.com']
    assert normalize_email_recipients('user:3, b@example.com') == ['user:3', 'b@example.com']


def test_face_rule_recipients_keep_user_references():
    from app.face_detection_rules import _normalize_recipients_field, face_rule_email_recipients

    assert _normalize_recipients_field(['user:2', 'junk', 'c@example.com']) == 'user:2, c@example.com'
    assert face_rule_email_recipients({'email_recipients': 'USER:2, c@example.com'}) == ['user:2', 'c@example.com']


def test_resolve_email_recipients_uses_current_active_user_addresses():
    from app.alert_dispatch import resolve_email_recipients

    users = [
        {'id': 1, 'email': 'one@example.com', 'is_active': 1},
        {'id': 2, 'email': 'two@example.com', 'is_active': 0},
        {'id': 3, 'email': '', 'is_active': 1},
    ]
    assert resolve_email_recipients(
        ['user:1', 'typed@example.com', 'user:2', 'user:3', 'user:99', 'ONE@example.com'], users,
    ) == ['one@example.com', 'typed@example.com']
    assert resolve_email_recipients([], users) == []


def test_deliver_email_alerts_follows_user_email_changes(tmp_path, monkeypatch):
    _app, _ = _load_app(tmp_path, monkeypatch)
    import app.alert_dispatch as _ad

    auth = sys.modules['app.state'].auth
    user = auth.create_user('alice', 'Correct-Horse-Battery-9', email='alice@example.com')
    captured: list[list[str]] = []

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_alert(self, alert, *, event_id, recipients=None, **_kwargs):
            captured.append(list(recipients or []))

    monkeypatch.setattr(_ad, 'effective_email_alert_settings', lambda: {'enabled': True})
    monkeypatch.setattr(_ad, 'EmailAlertService', FakeEmailAlertService)
    monkeypatch.setattr(
        _ad._state.database, 'get_event',
        lambda _event_id: {'metadata': {}, 'snapshot_path': None, 'created_at': ''},
    )
    triggered = [{'label': 'person', 'rule_name': 'Gate person', 'confidence': 0.9, 'message': 'Person'}]
    rules = [{
        'name': 'Gate person', 'email_enabled': True,
        'email_recipients': [f"user:{user['id']}", 'typed@example.com'],
    }]

    _ad.deliver_email_alerts(triggered, 1, rules=rules)
    assert captured == [['alice@example.com', 'typed@example.com']]

    # The rule is untouched; the user's new address is used on the next alert.
    auth.update_user(int(user['id']), email='alice@new.example.com')
    captured.clear()
    _ad.deliver_email_alerts(triggered, 2, rules=rules)
    assert captured == [['alice@new.example.com', 'typed@example.com']]

    # A deactivated user gets nothing; nobody left means no send at all.
    auth.update_user(int(user['id']), is_active=False)
    captured.clear()
    _ad.deliver_email_alerts(triggered, 3, rules=[{**rules[0], 'email_recipients': [f"user:{user['id']}"]}])
    assert captured == []


def test_camera_offline_settings_keep_user_recipients(tmp_path, monkeypatch):
    from tests.support import LocalClient, _login, _server, _setup_admin

    app, _ = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, _payload = client.request(
            '/api/settings/camera-offline',
            method='PUT',
            json_body={
                'enabled': True, 'offline_delay_minutes': 2,
                'recipients': ['User:4', 'ops@example.com', 'junk', 'user:4'],
            },
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        status, _headers, payload = client.request('/api/settings/camera-offline')
        assert status == 200
        assert payload['recipients'] == ['user:4', 'ops@example.com']
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ─── Push recipients: users' own ntfy topics ────────────────────────────────

def test_normalize_push_recipients_keeps_only_user_references():
    from app.utils import normalize_push_recipients

    assert normalize_push_recipients(['user:3', 'a@example.com', 'USER:3', 'topic-name', 'user:5']) == ['user:3', 'user:5']
    assert normalize_push_recipients(None) == []


def test_resolve_push_topics_uses_current_active_user_topics():
    from app.alert_recipients import resolve_push_topics

    users = [
        {'id': 1, 'ntfy_topic': 'alice-phone', 'is_active': 1},
        {'id': 2, 'ntfy_topic': 'bob-phone', 'is_active': 0},
        {'id': 3, 'ntfy_topic': '', 'is_active': 1},
        {'id': 4, 'ntfy_topic': 'alice-phone', 'is_active': 1},
    ]
    assert resolve_push_topics(['user:1', 'user:2', 'user:3', 'user:4', 'user:99'], users) == ['alice-phone']
    assert resolve_push_topics([], users) == []


def test_ntfy_topic_is_validated_and_listed(tmp_path, monkeypatch):
    import pytest

    _load_app(tmp_path, monkeypatch)
    # Imported after _load_app, which re-imports the app.* namespace.
    from app.auth import AuthError

    auth = sys.modules['app.state'].auth
    user = auth.create_user('carol', 'Correct-Horse-Battery-9')
    assert auth.update_user(int(user['id']), ntfy_topic='  carol_phone-1 ')['ntfy_topic'] == 'carol_phone-1'
    assert [u['ntfy_topic'] for u in auth.list_users() if u['id'] == user['id']] == ['carol_phone-1']
    with pytest.raises(AuthError):
        auth.update_user(int(user['id']), ntfy_topic='bad/topic')
    assert auth.update_profile(int(user['id']), ntfy_topic='')['ntfy_topic'] == ''


def _capture_pushes(monkeypatch, _ad):
    captured: list[object] = []

    class FakePushNotificationService:
        def __init__(self, settings):
            self.settings = settings

        def send_alert(self, alert, *, event_id, topics=None, **_kwargs):
            captured.append(topics)

    monkeypatch.setattr(_ad, 'effective_push_notification_settings', lambda: {'enabled': True})
    monkeypatch.setattr(_ad, 'PushNotificationService', FakePushNotificationService)
    monkeypatch.setattr(
        _ad._state.database, 'get_event',
        lambda _event_id: {'metadata': {}, 'snapshot_path': None, 'created_at': ''},
    )
    return captured


def test_deliver_push_notifications_targets_chosen_users_topics(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.alert_dispatch as _ad

    auth = sys.modules['app.state'].auth
    alice = auth.create_user('alice', 'Correct-Horse-Battery-9')
    auth.update_user(int(alice['id']), ntfy_topic='alice-phone')
    captured = _capture_pushes(monkeypatch, _ad)
    triggered = [{'label': 'person', 'rule_name': 'Gate person', 'confidence': 0.9, 'message': 'Person'}]
    rule = {'name': 'Gate person', 'push_enabled': True}

    # Nobody chosen: the shared Settings topic (topics=[]).
    _ad.deliver_push_notifications(triggered, 1, rules=[rule])
    # A chosen user: their own topic, which follows profile changes.
    _ad.deliver_push_notifications(triggered, 2, rules=[{**rule, 'push_recipients': [f"user:{alice['id']}"]}])
    auth.update_user(int(alice['id']), ntfy_topic='alice-new')
    _ad.deliver_push_notifications(triggered, 3, rules=[{**rule, 'push_recipients': [f"user:{alice['id']}"]}])
    # Chosen but unable to receive (deactivated): nobody, no shared fallback.
    auth.update_user(int(alice['id']), is_active=False)
    _ad.deliver_push_notifications(triggered, 4, rules=[{**rule, 'push_recipients': [f"user:{alice['id']}"]}])
    assert captured == [[], ['alice-phone'], ['alice-new']]


def test_push_service_delivers_once_per_topic(monkeypatch):
    from app.push_notifications import PushNotificationError, PushNotificationService

    service = PushNotificationService({'enabled': True, 'server_url': 'https://ntfy.example', 'topic': 'shared'})
    sent: list[str | None] = []

    def fake_deliver(title, body, *, tags=None, topic=None):
        sent.append(topic)
        if topic == 'broken':
            raise PushNotificationError('boom')

    monkeypatch.setattr(service, '_deliver', fake_deliver)
    import pytest

    service._deliver_to_topics('t', 'b', topics=None)
    # A failing topic does not stop the rest, and is reported at the end.
    with pytest.raises(PushNotificationError, match='broken'):
        service._deliver_to_topics('t', 'b', topics=['a', 'broken', 'b', 'a'])
    assert sent == [None, 'a', 'broken', 'b']


def test_profile_topic_is_returned_as_user_topic_on_push_settings(tmp_path, monkeypatch):
    from tests.support import LocalClient, _login, _server, _setup_admin

    app, _ = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, payload = client.request(
            '/api/profile', method='PUT', json_body={'ntfy_topic': 'admin-phone'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200 and payload['ntfy_topic'] == 'admin-phone'
        status, _headers, payload = client.request(
            '/api/profile', method='PUT', json_body={'ntfy_topic': 'no spaces'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 400
        status, _headers, payload = client.request('/api/settings/alert-push')
        assert status == 200 and payload['user_topic'] == 'admin-phone'
        status, _headers, payload = client.request('/api/auth/me')
        assert payload['user']['ntfy_topic'] == 'admin-phone'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_test_email_goes_to_picked_users_or_the_from_address(tmp_path, monkeypatch):
    from tests.support import LocalClient, _login, _server, _setup_admin

    app, _ = _load_app(tmp_path, monkeypatch)
    sent: list[str] = []

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_test(self, recipient):
            sent.append(recipient)

    import app.api.alert_email_router as alert_email_router_module
    monkeypatch.setattr(alert_email_router_module, 'EmailAlertService', FakeEmailAlertService)
    settings = {
        'enabled': True, 'host': 'smtp.example.com', 'port': 587,
        'from_address': 'alerts@example.com', 'use_tls': True, 'use_ssl': False,
    }
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        # Created after the first-run admin setup, which needs an empty user table.
        auth = sys.modules['app.state'].auth
        alice = auth.create_user('alice', 'Correct-Horse-Battery-9', email='alice@example.com')
        bob = auth.create_user('bob', 'Correct-Horse-Battery-9', email='bob@example.com')
        auth.update_user(int(bob['id']), is_active=False)

        def send(recipients):
            return client.request(
                '/api/settings/alert-email/test', method='POST',
                json_body={'settings': settings, 'recipients': recipients},
                headers={'X-CSRF-Token': csrf},
            )

        status, _headers, payload = send([f"user:{alice['id']}", f"user:{bob['id']}"])
        assert status == 200 and payload == {'ok': True, 'recipients': ['alice@example.com']}
        status, _headers, payload = send([])
        assert status == 200 and payload['recipients'] == ['alerts@example.com']
        status, _headers, _payload = send([f"user:{bob['id']}"])
        assert status == 400
        assert sent == ['alice@example.com', 'alerts@example.com']
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_test_push_goes_to_picked_users_topics_or_the_shared_topic(tmp_path, monkeypatch):
    from tests.support import LocalClient, _login, _server, _setup_admin

    app, _ = _load_app(tmp_path, monkeypatch)
    sent: list[object] = []
    import app.push_notifications as push_module
    monkeypatch.setattr(push_module.PushNotificationService, 'send_test', lambda self, topics=None: sent.append(topics))
    settings = {'enabled': True, 'server_url': 'https://ntfy.sh', 'topic': 'shared-topic'}
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        auth = sys.modules['app.state'].auth
        alice = auth.create_user('alice', 'Correct-Horse-Battery-9')
        auth.update_user(int(alice['id']), ntfy_topic='alice-phone')
        bob = auth.create_user('bob', 'Correct-Horse-Battery-9')  # no topic

        def send(recipients):
            return client.request(
                '/api/settings/alert-push/test', method='POST',
                json_body={'settings': settings, 'recipients': recipients},
                headers={'X-CSRF-Token': csrf},
            )

        status, _headers, payload = send([f"user:{alice['id']}", f"user:{bob['id']}"])
        assert status == 200 and payload == {'ok': True, 'topics': ['alice-phone']}
        status, _headers, payload = send([])
        assert status == 200 and payload['topics'] == ['shared-topic']
        status, _headers, _payload = send([f"user:{bob['id']}"])
        assert status == 400
        assert sent == [['alice-phone'], []]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
