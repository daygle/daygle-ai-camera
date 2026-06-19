"""Live-alert delivery cluster extracted from ``app/main.py`` (Phase-28).

This module owns the five helpers that previously lived inline on
``app/main.py``:

* ``wait_for_pending_alert_notifications`` — sync barrier for tests,
  blocks until in-flight email/push delivery threads complete.
* ``deliver_alert_notifications`` — the main orchestrator that runs
  email + push delivery for a triggered event.
* ``deliver_email_alerts`` — email-side delivery (uses
  ``app.email_alerts.EmailAlertService``).
* ``deliver_push_notifications`` — push-side delivery (uses
  ``app.push_notifications.PushNotificationService``).
* ``deliver_sound_alert_notifications`` — sound-rule specific
  orchestrator that delegates to email + push.

The two state primitives stay on ``app.main`` (NOT moved here):

* ``_notification_threads_lock`` — ``threading.Lock`` guarding
  ``_notification_threads``.
* ``_notification_threads`` — list of in-flight delivery threads.

This mirrors the Phase-26 ``live_detection_history`` +
``live_detection_history_lock`` precedent (state stays on main, the new
module reaches it through Pool C).

**Pool-A rebind (in ``app/main.py``):** every helper is re-bound as
``main.<orig_name>``. The two originally-underscored names
(``_deliver_alert_notifications`` and ``_deliver_sound_alert_notifications``)
are aliased back to their underscored public names in the rebind
block so existing ``main.py`` body callers keep working without
modification (the new module exposes them as clean public APIs:
``deliver_alert_notifications`` and ``deliver_sound_alert_notifications``).

**Pool-C reach sites used by this module (each resolved lazily via
``import app.main as main``):**

* ``main._notification_threads_lock``, ``main._notification_threads``
  — state primitives.
* ``main.database`` — the singleton DB handle, instantiated in
  ``app/main.py`` (~L248: ``database = EventDatabase(...)``). NOTE
  this is NOT a module-level export of ``app/database.py``;
  ``app/database.py`` only exposes the ``EventDatabase`` class.
* ``main.effective_email_alert_settings()``,
  ``main.effective_push_notification_settings()`` — Phase-9/10
  top-of-file rebound accessors.
* ``main._format_alert_datetime(...)``, ``main._rule_notify_active_now(...)``,
  ``main.compute_minimum_rule_confidence()`` — top-level helpers on
  ``main.py``.
* ``main.render_live_snapshot_jpeg_overlay(...)`` — Phase-25 rebind
  from ``app.live_snapshot``.
* ``main.EmailAlertService``, ``main.EmailAlertError``,
  ``main.PushNotificationService``, ``main.PushNotificationError``
  — service classes + exception types reached via Pool A so tests can
  monkeypatch ``main.<ServiceName>`` consistently (mirrors the Pool-A
  contract documented in :mod:`app.api.__init__` and the proven
  pattern in :mod:`app.detection_state`, :mod:`app.event_debounce`,
  :mod:`app.detection_status`). Replaces the previous direct
  top-of-module imports (see commit history — the bypass produced a
  single failing pytest case post-Phase-28, fixed by restoring the
  Pool-A reach).

**Logger acquisition:** the module uses its OWN child logger via
``logging.getLogger('daygle.ai')`` (matching the Phase-26
``app.detection_state`` precedent — same name, same logging tree.
Not reached via ``main.logger``.

**Bare-name internal calls.** ``deliver_alert_notifications`` and
``deliver_sound_alert_notifications`` invoke ``deliver_email_alerts``
and ``deliver_push_notifications`` as bare names. Because all five
helpers live in this module, those bare names resolve to the local
module namespace and do NOT require Pool A rebind interruption.

**Default-arg safety.** No function-default expressions evaluate
``main.<name>`` at module-load time, so the Phase-28 rebind loop can
fire BEFORE the body of this module is interpreted (everything is
resolved at call time only).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import app.main as main
# Service classes + their exception types are intentionally NOT imported
# here; ``deliver_email_alerts`` and ``deliver_push_notifications`` reach
# them through ``main.<ServiceName>`` / ``main.<ErrorType>`` (Pool A at
# call time) so test monkeypatches against ``main.<attr>`` land
# consistently. The service classes are still top-level attributes of
# ``app.main`` via the standard top-of-file Pool-A import block in
# ``app/main.py``.

logger = logging.getLogger('daygle.ai')


def wait_for_pending_alert_notifications(timeout: float = 10.0) -> None:
    """Block until in-flight alert email/push deliveries finish (used by tests)."""
    deadline = time.time() + max(0.0, timeout)
    with main._notification_threads_lock:
        pending = [thread for thread in main._notification_threads if thread.is_alive()]
    for thread in pending:
        thread.join(timeout=max(0.0, deadline - time.time()))


def deliver_alert_notifications(
    triggered: list[dict[str, Any]],
    event_id: int,
    rules: list[dict[str, Any]] | None,
) -> None:
    try:
        deliver_email_alerts(triggered, event_id, rules=rules)
    except Exception as exc:
        logger.warning('Email alert delivery failed for event %s: %s', event_id, exc)
    try:
        deliver_push_notifications(triggered, event_id, rules=rules)
    except Exception as exc:
        logger.warning('Push notification delivery failed for event %s: %s', event_id, exc)


def deliver_email_alerts(
    triggered: list[dict[str, Any]],
    event_id: int,
    rules: list[dict[str, Any]] | None = None,
) -> None:
    if not triggered:
        return
    event = main.database.get_event(event_id) or {}
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    camera_name = str(metadata.get('camera_name') or '').strip() or None
    camera_id = str(metadata.get('camera_id') or '').strip() or None
    created_at_raw = str(event.get('created_at') or '').strip()
    detected_at = main._format_alert_datetime(created_at_raw) if created_at_raw else None
    rules_by_name = {str(rule.get('name')): rule for rule in rules or []}
    any_email_enabled = any(
        (
            (rule := rules_by_name.get(str(alert.get('rule_name')), {})).get('email_enabled')
            and main._rule_notify_active_now(rule)
        )
        for alert in triggered
    )
    snapshot_bytes: bytes | None = None
    snapshot_path = str(event.get('snapshot_path') or '')
    if any_email_enabled and snapshot_path:
        try:
            snap_path = Path(snapshot_path)
            if snap_path.exists():
                raw_bytes = snap_path.read_bytes()
                db_detections = event.get('detections') or []
                _email_min_conf = main.compute_minimum_rule_confidence()
                overlay_detections = [
                    {
                        'label': d.get('label'),
                        'confidence': d.get('confidence'),
                        'box': {
                            'x': d.get('x', 0),
                            'y': d.get('y', 0),
                            'width': d.get('width', 0),
                            'height': d.get('height', 0),
                        },
                    }
                    for d in db_detections
                    if float(d.get('confidence') or 0) >= _email_min_conf
                ]
                snapshot_bytes = main.render_live_snapshot_jpeg_overlay(raw_bytes, overlay_detections)
        except Exception as exc:
            logger.debug('Failed to annotate snapshot for email alert event %s: %s', event_id, exc)
    mailer = main.EmailAlertService(main.effective_email_alert_settings())
    all_triggered_labels = sorted(
        {
            str(alert.get('label') or '').strip()
            for alert in triggered
            if str(alert.get('label') or '').strip()
        }
    )
    for alert in triggered:
        rule = rules_by_name.get(str(alert.get('rule_name')))
        if not rule or not rule.get('email_enabled'):
            continue
        if not main._rule_notify_active_now(rule):
            logger.debug(
                'Email skipped for event %s rule %r: outside email/push window %s-%s',
                event_id,
                alert.get('rule_name'),
                rule.get('notify_start'),
                rule.get('notify_end'),
            )
            continue
        try:
            mailer.send_alert(
                alert,
                event_id=event_id,
                recipients=rule.get('email_recipients', []),
                camera_name=camera_name,
                camera_id=camera_id,
                snapshot_bytes=snapshot_bytes,
                triggered_labels=all_triggered_labels,
                detected_at=detected_at,
            )
        except main.EmailAlertError as exc:
            logger.warning(
                'Failed to send email alert for event %s rule %s: %s',
                event_id,
                alert.get('rule_name'),
                exc,
            )


def deliver_push_notifications(
    triggered: list[dict[str, Any]],
    event_id: int,
    rules: list[dict[str, Any]] | None = None,
) -> None:
    if not triggered:
        return
    push_settings = main.effective_push_notification_settings()
    if not push_settings.get('enabled'):
        logger.debug('Push notifications disabled globally; skipping event %s', event_id)
        return
    event = main.database.get_event(event_id) or {}
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    camera_name = str(metadata.get('camera_name') or '').strip() or None
    camera_id = str(metadata.get('camera_id') or '').strip() or None
    created_at_raw = str(event.get('created_at') or '').strip()
    detected_at = main._format_alert_datetime(created_at_raw) if created_at_raw else None
    rules_by_name = {str(rule.get('name')): rule for rule in rules or []}
    notifier = main.PushNotificationService(push_settings)
    all_triggered_labels = sorted(
        {
            str(alert.get('label') or '').strip()
            for alert in triggered
            if str(alert.get('label') or '').strip()
        }
    )
    for alert in triggered:
        rule_name = str(alert.get('rule_name') or '')
        rule = rules_by_name.get(rule_name)
        if not rule:
            logger.debug('Push skipped for event %s: no rule found for %r', event_id, rule_name)
            continue
        if not rule.get('push_enabled'):
            logger.debug(
                'Push skipped for event %s rule %r: push_enabled is False',
                event_id,
                rule_name,
            )
            continue
        if not main._rule_notify_active_now(rule):
            logger.debug(
                'Push skipped for event %s rule %r: outside email/push window %s-%s',
                event_id,
                rule_name,
                rule.get('notify_start'),
                rule.get('notify_end'),
            )
            continue
        try:
            notifier.send_alert(
                alert,
                event_id=event_id,
                camera_name=camera_name,
                camera_id=camera_id,
                triggered_labels=all_triggered_labels,
                detected_at=detected_at,
            )
            logger.info('Push notification sent for event %s rule %r', event_id, rule_name)
        except main.PushNotificationError as exc:
            logger.error(
                'Failed to send push notification for event %s rule %r: %s',
                event_id,
                rule_name,
                exc,
            )


def deliver_sound_alert_notifications(
    triggered: list[dict[str, Any]],
    event_id: int,
    rule: dict[str, Any],
) -> None:
    if rule.get('email_enabled'):
        try:
            deliver_email_alerts(triggered, event_id, rules=[rule])
        except Exception as exc:
            logger.warning(
                'Sound alert email delivery failed for event %s: %s', event_id, exc,
            )
    if rule.get('push_enabled'):
        try:
            deliver_push_notifications(triggered, event_id, rules=[rule])
        except Exception as exc:
            logger.warning(
                'Sound alert push delivery failed for event %s: %s', event_id, exc,
            )
