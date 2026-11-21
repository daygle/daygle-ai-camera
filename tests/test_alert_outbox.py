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


def test_a_due_row_is_claimed_by_only_one_drainer(db):
    """The admin retry and the background worker must not both send a row."""
    outbox = _outbox()
    outbox.enqueue('push', 11, _alert(), topics=['daygle-ops'])
    now = time.time() + 1
    first = db.claim_due_alert_deliveries(now)
    second = db.claim_due_alert_deliveries(now)
    assert len(first) == 1 and second == []
    # A drainer that dies mid-attempt leaves the row for the next lease.
    assert len(db.claim_due_alert_deliveries(now + 3600)) == 1


def test_outbox_enqueue_needs_an_email_recipient(db):
    outbox = _outbox()
    assert outbox.enqueue('email', 7, _alert(), recipients=[]) is False


def test_a_shared_topic_push_is_queued_and_retried_on_the_shared_topic(db, monkeypatch):
    """No topics means the shared Settings topic (the default setup). It used
    to be refused by ``enqueue``, so an ntfy outage lost those alerts."""
    outbox = _outbox()
    assert outbox.enqueue('push', 7, _alert(), topics=[]) is True

    sent: list[dict] = []

    class FakePushNotificationService:
        def __init__(self, settings):
            self.settings = settings

        def configured(self):
            return True

        def send_alert(self, alert, **kwargs):
            sent.append(kwargs)

    push = importlib.import_module('app.push_notifications')
    facades = importlib.import_module('app.config_facades')
    monkeypatch.setattr(push, 'PushNotificationService', FakePushNotificationService)
    monkeypatch.setattr(facades, 'effective_push_notification_settings', lambda: {'enabled': True})
    assert outbox.retry_due(now=time.time() + 1) == 1
    assert len(sent) == 1 and sent[0]['topics'] == []
    assert db.count_alert_deliveries() == 0


def test_manual_retry_does_not_resend_a_row_the_worker_is_sending(db, monkeypatch):
    outbox = _outbox()
    outbox.enqueue('push', 31, _alert(), topics=['daygle-ops'])
    row_id = db.alert_deliveries()[0]['id']
    delivered: list[int] = []
    manual: list[dict | None] = []

    def deliver(row):
        delivered.append(row['id'])
        # The admin clicks Retry while the worker is mid-delivery.
        manual.append(outbox.retry_now(row_id))

    monkeypatch.setattr(outbox, '_deliver_row', deliver)
    assert outbox.retry_due(now=time.time() + 1) == 1
    assert delivered == [row_id]
    assert manual[0]['status'] == 'in_progress'
    # The worker's pass is over: the row is gone and ownership was released.
    assert outbox.retry_now(row_id) is None
    assert outbox._in_flight == set()


def test_worker_skips_a_claimed_row_that_is_being_retried_manually(db, monkeypatch):
    outbox = _outbox()
    outbox.enqueue('push', 32, _alert(), topics=['daygle-ops'])
    delivered: list[int] = []
    passes: list[int] = []

    def deliver(row):
        delivered.append(row['id'])
        # The worker's pass lands while the manual retry is still sending.
        passes.append(outbox.retry_due(now=time.time() + 1))

    monkeypatch.setattr(outbox, '_deliver_row', deliver)
    row_id = db.alert_deliveries()[0]['id']
    assert outbox.retry_now(row_id) == {'status': 'delivered'}
    assert passes == [1] and delivered == [row_id]
    assert outbox._in_flight == set()


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


def test_email_retry_skips_a_user_disabled_while_the_row_waited(db, monkeypatch):
    """The queued row is re-resolved at send time, so disabling a user during
    a relay outage does not deliver their queued alerts afterwards."""
    outbox = _outbox()
    state = importlib.import_module('app.state')
    users = [{'id': 1, 'email': 'alice@example.com', 'is_active': 1}]
    monkeypatch.setattr(state, 'auth', type('FakeAuth', (), {'list_users': staticmethod(lambda: users)})())
    monkeypatch.setattr(db, 'get_event', lambda _event_id: {})

    sent: list[list[str]] = []

    class FakeEmailAlertService:
        def __init__(self, settings):
            self.settings = settings

        def configured(self):
            return True

        def send_alert(self, _alert, *, recipients, **_kwargs):
            sent.append(list(recipients))

    email_alerts = importlib.import_module('app.email_alerts')
    facades = importlib.import_module('app.config_facades')
    monkeypatch.setattr(email_alerts, 'EmailAlertService', FakeEmailAlertService)
    monkeypatch.setattr(
        facades, 'effective_email_alert_settings',
        lambda: {'enabled': True, 'host': 'smtp.example', 'from_address': 'alerts@example.com'},
    )

    outbox.enqueue(
        'email', 41, _alert(),
        recipients=['alice@example.com', 'ops@example.com'],
        recipient_entries=['user:1', 'ops@example.com'],
    )
    users[0]['is_active'] = 0
    assert outbox.retry_due(now=time.time() + 1) == 1
    assert sent == [['ops@example.com']]

    # Only the disabled user was chosen: the row is dropped, nothing is sent.
    outbox.enqueue(
        'email', 42, _alert(),
        recipients=['alice@example.com'], recipient_entries=['user:1'],
    )
    assert outbox.retry_due(now=time.time() + 2) == 1
    assert sent == [['ops@example.com']]
    assert db.count_alert_deliveries() == 0


def _fake_push_service(monkeypatch, sent):
    class FakePushNotificationService:
        def __init__(self, settings):
            self.settings = settings

        def configured(self):
            return True

        def send_alert(self, _alert, *, topics=None, **_kwargs):
            sent.append(list(topics or []))

    push = importlib.import_module('app.push_notifications')
    facades = importlib.import_module('app.config_facades')
    monkeypatch.setattr(push, 'PushNotificationService', FakePushNotificationService)
    monkeypatch.setattr(
        facades, 'effective_push_notification_settings',
        lambda: {'enabled': True, 'server_url': 'https://ntfy.example', 'topic': 'shared'},
    )


def test_push_retry_skips_a_user_disabled_while_the_row_waited(db, monkeypatch):
    """The queued push is re-resolved at send time, so disabling a user during
    an ntfy outage does not push to them afterwards."""
    outbox = _outbox()
    state = importlib.import_module('app.state')
    users = [
        {'id': 1, 'ntfy_topic': 'alice-phone', 'is_active': 1},
        {'id': 2, 'ntfy_topic': 'bob-phone', 'is_active': 1},
    ]
    monkeypatch.setattr(state, 'auth', type('FakeAuth', (), {'list_users': staticmethod(lambda: users)})())
    sent: list[list[str]] = []
    _fake_push_service(monkeypatch, sent)

    outbox.enqueue(
        'push', 51, _alert(), topics=['alice-phone', 'bob-phone'],
        push_entries=['user:1', 'user:2'],
    )
    users[0]['is_active'] = 0
    assert outbox.retry_due(now=time.time() + 1) == 1
    assert sent == [['bob-phone']]

    # Only the disabled user was chosen: the row is dropped, nothing is sent.
    outbox.enqueue('push', 52, _alert(), topics=['alice-phone'], push_entries=['user:1'])
    assert outbox.retry_due(now=time.time() + 2) == 1
    assert sent == [['bob-phone']]
    assert db.count_alert_deliveries() == 0


def test_push_retry_filters_a_row_queued_with_only_its_topics(db, monkeypatch):
    """Rows written before ``push_entries`` existed carry resolved topics; a
    disabled user's topic is still dropped, while the shared topic is not."""
    outbox = _outbox()
    state = importlib.import_module('app.state')
    users = [
        {'id': 1, 'ntfy_topic': 'alice-phone', 'is_active': 0},
        {'id': 2, 'ntfy_topic': 'bob-phone', 'is_active': 1},
    ]
    monkeypatch.setattr(state, 'auth', type('FakeAuth', (), {'list_users': staticmethod(lambda: users)})())
    sent: list[list[str]] = []
    _fake_push_service(monkeypatch, sent)

    outbox.enqueue('push', 53, _alert(), topics=['alice-phone', 'bob-phone'])
    assert outbox.retry_due(now=time.time() + 1) == 1
    assert sent == [['bob-phone']]

    # A row on the shared Settings topic (no chosen users) still goes out.
    outbox.enqueue('push', 54, _alert())
    assert outbox.retry_due(now=time.time() + 2) == 1
    assert sent == [['bob-phone'], []]

    # Every stored topic belonged to a disabled user: dropped.
    outbox.enqueue('push', 55, _alert(), topics=['alice-phone'])
    assert outbox.retry_due(now=time.time() + 3) == 1
    assert sent == [['bob-phone'], []]
    assert db.count_alert_deliveries() == 0


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
