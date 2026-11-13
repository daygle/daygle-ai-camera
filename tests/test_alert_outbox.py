"""Durable retry queue for alert deliveries the transports cannot hand over.

The transports retry a transient failure for a second or two
(app.alert_retry); a longer relay/ntfy outage is queued in
``alert_delivery_outbox`` and a background worker re-sends it with a
minutes-scale backoff. Only deliveries where nothing was accepted are queued,
so a retry can never duplicate an alert.
"""

from __future__ import annotations

import importlib
import json
import time

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    database = importlib.import_module('app.database')
    state = importlib.import_module('app.state')
    instance = database.EventDatabase(str(tmp_path / 'alerts.sqlite3'))
    monkeypatch.setattr(state, 'database', instance)
    return instance


def _outbox():
    return importlib.import_module('app.alert_outbox')


def _alert(**overrides):
    alert = {'label': 'cat', 'rule_name': 'Cat alert', 'confidence': 0.9, 'message': 'Cat matched'}
    alert.update(overrides)
    return alert


def test_outbox_enqueue_dedupes_the_same_delivery(db):
    outbox = _outbox()
    assert outbox.enqueue('email', 7, _alert(), recipients=['ops@example.com']) is True
    assert outbox.enqueue('email', 7, _alert(), recipients=['ops@example.com']) is False

    rows = db.due_alert_deliveries(time.time() + 1)
    assert len(rows) == 1
    payload = json.loads(rows[0]['payload'])
    assert payload['recipients'] == ['ops@example.com']
    assert payload['alert']['rule_name'] == 'Cat alert'

    # Another rule (or channel) on the same event is its own delivery.
    assert outbox.enqueue('email', 7, _alert(rule_name='Other'), recipients=['ops@example.com']) is True
    assert outbox.enqueue('push', 7, _alert(), topics=['daygle-ops']) is True
    assert len(db.due_alert_deliveries(time.time() + 1)) == 3


def test_outbox_enqueue_needs_a_recipient_or_topic(db):
    outbox = _outbox()
    assert outbox.enqueue('email', 7, _alert(), recipients=[]) is False
    assert outbox.enqueue('push', 7, _alert(), topics=[]) is False


def test_retry_delivers_then_deletes_the_row(db, monkeypatch):
    outbox = _outbox()
    delivered: list[dict] = []
    monkeypatch.setattr(outbox, '_deliver_row', lambda row: delivered.append(row))

    outbox.enqueue('push', 9, _alert(label='person'), topics=['daygle-ops'])
    assert outbox.retry_due(now=time.time() + 1) == 1
    assert len(delivered) == 1
    assert delivered[0]['event_id'] == 9
    assert db.due_alert_deliveries(time.time() + 10 ** 9) == []


def test_retry_reschedules_with_backoff_then_gives_up(db, monkeypatch):
    outbox = _outbox()

    def unreachable(_row):
        raise OSError('Network is unreachable')

    monkeypatch.setattr(outbox, '_deliver_row', unreachable)
    outbox.enqueue('email', 11, _alert(), recipients=['ops@example.com'])
    now = time.time()

    for attempt in range(1, outbox.OUTBOX_MAX_ATTEMPTS):
        assert outbox.retry_due(now=now) == 1
        rows = db.due_alert_deliveries(now + 10 ** 7)
        assert rows and rows[0]['attempts'] == attempt
        assert 'Network is unreachable' in (rows[0]['last_error'] or '')
        # The backoff must keep the row out of the next immediate pass.
        assert db.due_alert_deliveries(now + 1) == []
        now += 10 ** 6

    # The last allowed attempt drops the row: a permanently broken setup must
    # not accumulate failures for the retention window.
    assert outbox.retry_due(now=now) == 1
    assert db.due_alert_deliveries(now + 10 ** 9) == []


def test_retry_drops_a_row_when_the_channel_was_disabled(db, monkeypatch):
    outbox = _outbox()

    def disabled(_row):
        raise outbox._ChannelDisabled('email alerts are disabled')

    monkeypatch.setattr(outbox, '_deliver_row', disabled)
    outbox.enqueue('email', 12, _alert(), recipients=['ops@example.com'])
    assert outbox.retry_due(now=time.time() + 1) == 1
    assert db.due_alert_deliveries(time.time() + 10 ** 9) == []


def test_prune_drops_rows_past_the_retention_window(db):
    outbox = _outbox()
    outbox.enqueue('push', 13, _alert(), topics=['daygle-ops'])
    assert outbox.prune(now=time.time()) == 0
    assert outbox.prune(now=time.time() + outbox.OUTBOX_RETENTION_SECONDS + 60) == 1


def test_public_entries_describe_the_queue_without_the_raw_payload(db):
    outbox = _outbox()
    outbox.enqueue('email', 21, _alert(), recipients=['ops@example.com'], error='relay down')
    entries = outbox.public_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry['channel'] == 'email'
    assert entry['rule_name'] == 'Cat alert'
    assert entry['label'] == 'cat'
    assert entry['recipients'] == ['ops@example.com']
    assert entry['attempts'] == 0
    assert 'payload' not in entry
    assert outbox.pending_count() == 1


def test_retry_now_reports_the_outcome_and_404s_a_missing_row(db, monkeypatch):
    outbox = _outbox()
    outbox.enqueue('push', 22, _alert(), topics=['daygle-ops'])
    row_id = db.due_alert_deliveries(time.time() + 1)[0]['id']

    monkeypatch.setattr(outbox, '_deliver_row', lambda row: None)
    assert outbox.retry_now(row_id) == {'status': 'delivered'}
    assert outbox.retry_now(row_id) is None

    outbox.enqueue('push', 23, _alert(), topics=['daygle-ops'])
    row_id = db.due_alert_deliveries(time.time() + 1)[0]['id']

    def still_down(_row):
        raise OSError('still down')

    monkeypatch.setattr(outbox, '_deliver_row', still_down)
    result = outbox.retry_now(row_id)
    assert result['status'] == 'rescheduled'
    assert 'still down' in result['error']
    assert outbox.pending_count() == 1


def test_retry_all_now_makes_every_row_due(db):
    outbox = _outbox()
    outbox.enqueue('email', 24, _alert(), recipients=['ops@example.com'])
    outbox.enqueue('push', 25, _alert(), topics=['daygle-ops'])
    db.request_all_alert_delivery_retries(time.time() + 3600)
    assert db.due_alert_deliveries(time.time()) == []

    assert outbox.retry_all_now() == 2
    assert len(db.due_alert_deliveries(time.time() + 1)) == 2


def test_worker_starts_idempotently_and_stops(db):
    outbox = _outbox()
    outbox.start_worker()
    first = outbox._worker
    outbox.start_worker()
    assert outbox._worker is first
    outbox.stop_worker()
    assert not first.is_alive()


def test_email_dispatch_queues_an_unavailable_delivery(monkeypatch):
    dispatch = importlib.import_module('app.alert_dispatch')
    queued: list[tuple] = []

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_alert(self, *_args, **_kwargs):
            raise dispatch.EmailAlertUnavailable('relay unreachable')

    monkeypatch.setattr(dispatch, 'EmailAlertService', FakeEmailAlertService)
    monkeypatch.setattr(
        dispatch, 'effective_email_alert_settings',
        lambda: {'enabled': True, 'host': 'smtp.example', 'from_address': 'a@example.com'},
    )
    monkeypatch.setattr(dispatch.alert_outbox, 'enqueue', lambda *a, **k: queued.append((a, k)))

    rules = [{'name': 'Cat alert', 'email_enabled': True, 'email_recipients': ['ops@example.com']}]
    dispatch.deliver_email_alerts(
        [_alert()], 7, rules=rules,
        event={'metadata': {}, 'snapshot_path': '', 'created_at': ''},
    )

    assert len(queued) == 1
    args, kwargs = queued[0]
    assert args[0] == 'email' and args[1] == 7
    assert kwargs['recipients'] == ['ops@example.com']


def test_email_dispatch_does_not_queue_a_permanent_failure(monkeypatch):
    dispatch = importlib.import_module('app.alert_dispatch')
    queued: list[tuple] = []

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def send_alert(self, *_args, **_kwargs):
            raise dispatch.EmailAlertError('bad@example.com: mailbox unavailable')

    monkeypatch.setattr(dispatch, 'EmailAlertService', FakeEmailAlertService)
    monkeypatch.setattr(
        dispatch, 'effective_email_alert_settings',
        lambda: {'enabled': True, 'host': 'smtp.example', 'from_address': 'a@example.com'},
    )
    monkeypatch.setattr(dispatch.alert_outbox, 'enqueue', lambda *a, **k: queued.append((a, k)))

    rules = [{'name': 'Cat alert', 'email_enabled': True, 'email_recipients': ['ops@example.com']}]
    dispatch.deliver_email_alerts(
        [_alert()], 7, rules=rules,
        event={'metadata': {}, 'snapshot_path': '', 'created_at': ''},
    )
    assert queued == []


def test_push_dispatch_queues_only_an_unavailable_delivery(monkeypatch):
    dispatch = importlib.import_module('app.alert_dispatch')
    queued: list[tuple] = []

    def _service_raising(exc_type):
        class FakePushNotificationService:
            def __init__(self, settings):
                self.settings = settings

            def send_alert(self, *_args, **_kwargs):
                raise exc_type('down')

        return FakePushNotificationService

    monkeypatch.setattr(
        dispatch, 'effective_push_notification_settings',
        lambda: {'enabled': True, 'server_url': 'https://ntfy.example', 'topic': 'shared'},
    )
    monkeypatch.setattr(dispatch.alert_outbox, 'enqueue', lambda *a, **k: queued.append((a, k)))
    rules = [{'name': 'Cat alert', 'push_enabled': True}]
    event = {'metadata': {}, 'snapshot_path': '', 'created_at': ''}

    monkeypatch.setattr(
        dispatch, 'PushNotificationService',
        _service_raising(dispatch.PushNotificationUnavailable),
    )
    dispatch.deliver_push_notifications([_alert()], 7, rules=rules, event=event)
    assert len(queued) == 1
    args, kwargs = queued[0]
    assert args[0] == 'push' and kwargs['topics'] == []

    queued.clear()
    monkeypatch.setattr(
        dispatch, 'PushNotificationService',
        _service_raising(dispatch.PushNotificationError),
    )
    dispatch.deliver_push_notifications([_alert()], 8, rules=rules, event=event)
    assert queued == []


def test_push_topics_report_unavailable_only_when_nothing_was_delivered():
    push = importlib.import_module('app.push_notifications')
    service = push.PushNotificationService(
        {'enabled': True, 'server_url': 'https://ntfy.example', 'topic': 'shared'},
    )

    def all_fail(_title, _body, *, tags=None, topic=None):
        raise push.PushNotificationUnavailable('down')

    service._deliver = all_fail
    with pytest.raises(push.PushNotificationUnavailable):
        service._deliver_to_topics('T', 'B', topics=['alice', 'bob'])

    def partial(_title, _body, *, tags=None, topic=None):
        if topic == 'bob':
            raise push.PushNotificationUnavailable('down')

    service._deliver = partial
    with pytest.raises(push.PushNotificationError) as exc_info:
        service._deliver_to_topics('T', 'B', topics=['alice', 'bob'])
    # Alice got the push, so the batch must not be queued as if none did.
    assert not isinstance(exc_info.value, push.PushNotificationUnavailable)


def test_smtp_setup_exhaustion_raises_unavailable(monkeypatch):
    email_alerts = importlib.import_module('app.email_alerts')
    retry = importlib.import_module('app.alert_retry')
    monkeypatch.setattr(retry, 'sleep_before_retry', lambda attempt: None)

    class DeadSMTP:
        def __init__(self, *_args, **_kwargs):
            raise TimeoutError('timed out')

    monkeypatch.setattr(email_alerts.smtplib, 'SMTP', DeadSMTP)
    service = email_alerts.EmailAlertService({
        'enabled': True, 'host': 'smtp.example', 'port': 587,
        'from_address': 'alerts@example.com', 'use_tls': True, 'use_ssl': False,
    })
    with pytest.raises(email_alerts.EmailAlertUnavailable):
        with service._create_smtp_session():
            pass
