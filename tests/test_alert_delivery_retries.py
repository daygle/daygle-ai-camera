"""Bounded retries for the alert transports (SMTP and ntfy).

A relay or ntfy server that was briefly unreachable used to lose the alert
outright: the mail session raised at setup and a push raised on its first
request, and the pool job only logged it. Both transports now retry a
*transient* failure with exponential backoff, and neither retries a permanent
one (bad credentials, bad recipient, 4xx) nor repeats a message the server
already accepted.
"""

from __future__ import annotations

import importlib
import urllib.error

import pytest


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Keep the real backoff out of the test runtime."""
    retry = importlib.import_module('app.alert_retry')
    attempts: list[int] = []
    monkeypatch.setattr(retry, 'sleep_before_retry', lambda attempt: attempts.append(attempt))
    return attempts


def _email_service(**overrides):
    from app.email_alerts import EmailAlertService
    settings = {
        'enabled': True, 'host': 'smtp.example.test', 'port': 587,
        'from_address': 'alerts@example.test', 'use_tls': True, 'use_ssl': False,
        'username': '', 'password': '',
    }
    settings.update(overrides)
    return EmailAlertService(settings)


def _open(service):
    """Run the session generator through ``__enter__`` and close it again."""
    session = service._create_smtp_session()
    smtp = session.__enter__()
    session.__exit__(None, None, None)
    return smtp


def test_retry_backoff_doubles_from_the_base(monkeypatch):
    retry = importlib.import_module('app.alert_retry')
    assert retry.retry_backoff_seconds(1) == retry.ALERT_RETRY_BASE_SECONDS
    assert retry.retry_backoff_seconds(2) == retry.ALERT_RETRY_BASE_SECONDS * 2
    assert retry.retry_backoff_seconds(3) == retry.ALERT_RETRY_BASE_SECONDS * 4


def test_smtp_connection_is_retried_when_the_server_is_unreachable(monkeypatch, slept):
    email_alerts = importlib.import_module('app.email_alerts')
    constructions: list[int] = []

    class FlakySMTP:
        def __init__(self, *_args, **_kwargs):
            constructions.append(1)
            if len(constructions) < 3:
                raise ConnectionRefusedError('Connection refused')

        def starttls(self, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            pass

        def quit(self):
            pass

    monkeypatch.setattr(email_alerts.smtplib, 'SMTP', FlakySMTP)
    _open(_email_service())
    assert len(constructions) == 3
    assert slept == [1, 2]


def test_smtp_gives_up_after_the_bounded_attempts(monkeypatch):
    email_alerts = importlib.import_module('app.email_alerts')
    retry = importlib.import_module('app.alert_retry')
    attempts: list[int] = []

    class DeadSMTP:
        def __init__(self, *_args, **_kwargs):
            attempts.append(1)
            raise TimeoutError('timed out')

    monkeypatch.setattr(email_alerts.smtplib, 'SMTP', DeadSMTP)
    from app.email_alerts import EmailAlertError
    with pytest.raises(EmailAlertError):
        _open(_email_service())
    assert len(attempts) == retry.ALERT_DELIVERY_ATTEMPTS


def test_smtp_login_failure_is_not_retried(monkeypatch, slept):
    email_alerts = importlib.import_module('app.email_alerts')
    attempts: list[int] = []

    class AuthFailingSMTP:
        def __init__(self, *_args, **_kwargs):
            attempts.append(1)

        def starttls(self, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            raise email_alerts.smtplib.SMTPAuthenticationError(535, b'bad credentials')

        def quit(self):
            pass

    monkeypatch.setattr(email_alerts.smtplib, 'SMTP', AuthFailingSMTP)
    from app.email_alerts import EmailAlertError
    with pytest.raises(EmailAlertError):
        _open(_email_service(username='user', password='wrong'))
    assert len(attempts) == 1
    assert slept == []


def _push_service():
    from app.push_notifications import PushNotificationService
    return PushNotificationService({
        'enabled': True, 'server_url': 'https://ntfy.example', 'topic': 'alerts',
    })


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_push_retries_a_transient_network_failure(monkeypatch, slept):
    push = importlib.import_module('app.push_notifications')
    calls: list[int] = []

    def urlopen(_request, timeout=10):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError('Name or service not known')
        return _FakeResponse()

    monkeypatch.setattr(push.urllib.request, 'urlopen', urlopen)
    _push_service()._deliver('Title', 'Body')
    assert len(calls) == 3
    assert slept == [1, 2]


def test_push_gives_up_after_the_bounded_attempts(monkeypatch):
    push = importlib.import_module('app.push_notifications')
    retry = importlib.import_module('app.alert_retry')
    calls: list[int] = []

    def urlopen(_request, timeout=10):
        calls.append(1)
        raise OSError('Network is unreachable')

    monkeypatch.setattr(push.urllib.request, 'urlopen', urlopen)
    from app.push_notifications import PushNotificationError
    with pytest.raises(PushNotificationError):
        _push_service()._deliver('Title', 'Body')
    assert len(calls) == retry.ALERT_DELIVERY_ATTEMPTS


def test_push_retries_a_503_but_not_a_401(monkeypatch):
    push = importlib.import_module('app.push_notifications')
    statuses = [503, 503, 200]
    calls: list[int] = []

    def urlopen(request, timeout=10):
        calls.append(1)
        code = statuses.pop(0)
        if code == 200:
            return _FakeResponse()
        raise urllib.error.HTTPError(request.full_url, code, 'unavailable', {}, None)

    monkeypatch.setattr(push.urllib.request, 'urlopen', urlopen)
    _push_service()._deliver('Title', 'Body')
    assert len(calls) == 3

    calls.clear()

    def unauthorized(request, timeout=10):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, None)

    monkeypatch.setattr(push.urllib.request, 'urlopen', unauthorized)
    from app.push_notifications import PushNotificationError
    with pytest.raises(PushNotificationError):
        _push_service()._deliver('Title', 'Body')
    assert len(calls) == 1
