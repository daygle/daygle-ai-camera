"""Durable retry queue for alert deliveries the transports could not hand over.

``app.alert_retry`` retries a transient failure for a second or two. When a mail
relay or ntfy server stays unreachable longer than that, the delivery is written
to ``alert_delivery_outbox`` instead of being only logged, and a background
worker re-sends it with a minutes-scale backoff until it succeeds or its
attempt budget runs out.

One row is one delivery (all the recipients of one rule's email, or all the
topics of one rule's push). Only deliveries where *nothing* was accepted by the
server are queued -- the services raise their ``*Unavailable`` errors before a
message is handed over -- so a retry can never send an alert twice. A retried
email carries the annotated snapshot while the event's image is still on disk;
once it has been purged the alert goes as text.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import app.state as _state

logger = logging.getLogger('daygle.ai.alerts')

# Five tries over roughly 16 minutes, then the row is dropped: long enough to
# ride out a relay maintenance window, short enough that a permanently broken
# setup does not accumulate rows for a week.
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_RETRY_BASE_SECONDS = 60.0
OUTBOX_POLL_SECONDS = 30.0
OUTBOX_RETENTION_SECONDS = 7 * 24 * 3600
OUTBOX_BATCH_SIZE = 20

_worker: threading.Thread | None = None
_worker_stop = threading.Event()


class _ChannelDisabled(Exception):
    """The channel was switched off or unconfigured while the row waited."""


def enqueue(
    channel: str,
    event_id: int,
    alert: dict[str, Any],
    *,
    recipients: list[str] | None = None,
    topics: list[str] | None = None,
    camera_name: str | None = None,
    camera_id: str | None = None,
    triggered_labels: list[str] | None = None,
    detected_at: str | None = None,
    error: str = '',
) -> bool:
    """Persist one delivery the transport could not make.

    Returns False when there is no database yet, the delivery has no
    recipient/topic, or the same event/rule/label is already queued for this
    channel (a re-dispatch then adds nothing).
    """
    db = getattr(_state, 'database', None)
    if db is None:
        return False
    if channel == 'email' and not recipients:
        return False
    if channel == 'push' and not topics:
        return False
    rule_name = str(alert.get('rule_name') or '')
    label = str(alert.get('label') or '')
    payload = {
        'alert': alert,
        'recipients': list(recipients or []),
        'topics': list(topics or []),
        'camera_name': camera_name,
        'camera_id': camera_id,
        'triggered_labels': list(triggered_labels or []),
        'detected_at': detected_at,
    }
    try:
        queued = db.enqueue_alert_delivery(
            created_at=datetime.now(UTC).isoformat(),
            event_id=int(event_id),
            channel=channel,
            dedupe_key=f'{channel}:{int(event_id)}:{rule_name}:{label}',
            payload=json.dumps(payload),
            next_attempt_at=time.time(),
        )
    except Exception:
        logger.exception('Could not queue the %s alert for event %s', channel, event_id)
        return False
    if queued:
        logger.info(
            'Queued the %s alert for event %s rule %r for a later retry: %s',
            channel, event_id, rule_name, error,
        )
    return queued


def _deliver_row(row: dict[str, Any]) -> None:
    """Re-send one queued delivery, raising whatever the transport raises."""
    payload = json.loads(row.get('payload') or '{}')
    alert = payload.get('alert') if isinstance(payload.get('alert'), dict) else {}
    event_id = int(row.get('event_id') or 0)
    channel = str(row.get('channel') or '')
    if channel == 'email':
        from app.config_facades import effective_email_alert_settings
        from app.email_alerts import EmailAlertService
        settings = effective_email_alert_settings()
        service = EmailAlertService(settings)
        if not settings.get('enabled') or not service.configured():
            raise _ChannelDisabled('email alerts are disabled or not configured')
        # Imported lazily: app.alert_dispatch imports this module.
        from app.alert_dispatch import _email_snapshot_bytes
        database = getattr(_state, 'database', None)
        event = (database.get_event(event_id) or {}) if database is not None else {}
        service.send_alert(
            alert,
            event_id=event_id,
            recipients=list(payload.get('recipients') or []),
            camera_name=payload.get('camera_name'),
            snapshot_bytes=_email_snapshot_bytes(event, event_id),
            triggered_labels=payload.get('triggered_labels'),
            detected_at=payload.get('detected_at'),
        )
        return
    from app.config_facades import effective_push_notification_settings
    from app.push_notifications import PushNotificationService
    settings = effective_push_notification_settings()
    service = PushNotificationService(settings)
    if not settings.get('enabled') or not service.configured():
        raise _ChannelDisabled('push notifications are disabled or not configured')
    service.send_alert(
        alert,
        event_id=event_id,
        camera_name=payload.get('camera_name'),
        camera_id=payload.get('camera_id'),
        triggered_labels=payload.get('triggered_labels'),
        detected_at=payload.get('detected_at'),
        topics=list(payload.get('topics') or []),
    )


def _retry_row(row: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Attempt one queued delivery; returns its new status for the UI."""
    db = _state.database
    row_id = int(row.get('id') or 0)
    channel = str(row.get('channel') or '')
    event_id = row.get('event_id')
    attempts = int(row.get('attempts') or 0) + 1
    try:
        _deliver_row(row)
    except _ChannelDisabled as exc:
        # The operator turned the channel off (or left it unconfigured) while
        # the row waited: dropping is the honest outcome, not a week of rows.
        db.delete_alert_delivery(row_id)
        logger.info(
            'Dropping the queued %s alert for event %s: %s', channel, event_id, exc,
        )
        return {'status': 'dropped', 'error': str(exc)}
    except Exception as exc:
        if attempts >= OUTBOX_MAX_ATTEMPTS:
            db.delete_alert_delivery(row_id)
            logger.warning(
                'Giving up on the queued %s alert for event %s after %d attempt(s): %s',
                channel, event_id, attempts, exc,
            )
            return {'status': 'gave_up', 'error': str(exc)}
        delay = OUTBOX_RETRY_BASE_SECONDS * (2 ** (attempts - 1))
        next_at = (time.time() if now is None else now) + delay
        db.reschedule_alert_delivery(row_id, attempts, next_at, str(exc)[:500])
        logger.warning(
            'Queued %s alert for event %s still failing (attempt %d/%d): %s; '
            'next try in %.0f min',
            channel, event_id, attempts, OUTBOX_MAX_ATTEMPTS, exc, delay / 60,
        )
        return {'status': 'rescheduled', 'error': str(exc)}
    db.delete_alert_delivery(row_id)
    logger.info(
        'Queued %s alert for event %s delivered on retry %d',
        channel, event_id, attempts,
    )
    return {'status': 'delivered'}


def retry_due(now: float | None = None, limit: int = OUTBOX_BATCH_SIZE) -> int:
    """Re-attempt up to ``limit`` due rows; returns how many were attempted."""
    db = getattr(_state, 'database', None)
    if db is None:
        return 0
    # Claim (not just list) the rows: the admin retry endpoint and the
    # background worker can run concurrently, and a second drainer must not
    # re-deliver a batch this one is already sending.
    rows = db.claim_due_alert_deliveries(time.time() if now is None else now, limit)
    for row in rows:
        try:
            _retry_row(row, now)
        except Exception:
            # A failed bookkeeping write must not abort the rest of the batch.
            logger.exception('Alert outbox retry failed for row %s', row.get('id'))
    return len(rows)


def public_entries(limit: int = 100) -> list[dict[str, Any]]:
    """Queued deliveries shaped for the admin settings view.

    Deliberately excludes the raw payload (the alert dict and any snapshot
    reference): the UI needs the rule, channel and reason, not the internals.
    """
    db = getattr(_state, 'database', None)
    if db is None:
        return []
    entries: list[dict[str, Any]] = []
    for row in db.alert_deliveries(limit):
        try:
            payload = json.loads(row.get('payload') or '{}')
        except ValueError:
            payload = {}
        alert = payload.get('alert') if isinstance(payload.get('alert'), dict) else {}
        entries.append({
            'id': int(row.get('id') or 0),
            'created_at': row.get('created_at'),
            'event_id': row.get('event_id'),
            'channel': row.get('channel'),
            'rule_name': str(alert.get('rule_name') or ''),
            'label': str(alert.get('label') or ''),
            'recipients': list(payload.get('recipients') or []),
            'topics': list(payload.get('topics') or []),
            'attempts': int(row.get('attempts') or 0),
            'last_error': row.get('last_error'),
            'next_attempt_at': float(row.get('next_attempt_at') or 0),
        })
    return entries


def pending_count() -> int:
    db = getattr(_state, 'database', None)
    return db.count_alert_deliveries() if db is not None else 0


def retry_now(row_id: int) -> dict[str, Any] | None:
    """Retry one queued delivery immediately; None when the row is gone."""
    db = getattr(_state, 'database', None)
    if db is None:
        return None
    row = db.alert_delivery(int(row_id))
    if row is None:
        return None
    try:
        return _retry_row(row)
    except Exception as exc:
        # A failed bookkeeping write must not surface as a 500 to the UI.
        logger.exception('Manual alert outbox retry failed for row %s', row_id)
        return {'status': 'error', 'error': str(exc)}


def retry_all_now(now: float | None = None) -> int:
    """Make every queued delivery due so the worker drains them.

    Deliberately not attempted inline: while the server is still down each row
    costs its transports' retry budget, which would hang the request.
    """
    db = getattr(_state, 'database', None)
    if db is None:
        return 0
    return db.request_all_alert_delivery_retries(time.time() if now is None else now)


def prune(now: float | None = None) -> int:
    """Drop rows older than the retention window; returns how many went."""
    db = getattr(_state, 'database', None)
    if db is None:
        return 0
    current = time.time() if now is None else now
    cutoff = datetime.fromtimestamp(current - OUTBOX_RETENTION_SECONDS, UTC)
    removed = db.prune_alert_deliveries(cutoff.isoformat())
    if removed:
        logger.info('Pruned %d expired queued alert delivery row(s)', removed)
    return removed


def _worker_loop() -> None:
    try:
        prune()
    except Exception:
        logger.warning('Alert outbox prune failed', exc_info=True)
    while not _worker_stop.wait(OUTBOX_POLL_SECONDS):
        try:
            retry_due()
        except Exception:
            logger.warning('Alert outbox retry pass failed', exc_info=True)


def start_worker() -> None:
    """Start the background retry thread (idempotent)."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker_stop.clear()
    _worker = threading.Thread(target=_worker_loop, name='alert-outbox', daemon=True)
    _worker.start()


def stop_worker() -> None:
    """Stop the retry thread, letting an in-flight pass finish briefly."""
    _worker_stop.set()
    worker = _worker
    if worker is not None:
        worker.join(timeout=2.0)
