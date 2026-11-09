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
