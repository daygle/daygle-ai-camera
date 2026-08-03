"""Live-alert delivery cluster extracted from ``app/main.py`` (Phase-28).

This module owns the five helpers that previously lived inline on
``app/main.py``:

* ``wait_for_pending_alert_notifications`` - sync barrier for tests,
  blocks until in-flight email/push delivery threads complete.
* ``deliver_alert_notifications`` - the main orchestrator that runs
  email + push delivery for a triggered event.
* ``deliver_email_alerts`` - email-side delivery (uses
  ``app.email_alerts.EmailAlertService``).
* ``deliver_push_notifications`` - push-side delivery (uses
  ``app.push_notifications.PushNotificationService``).
* ``deliver_sound_alert_notifications`` - sound-rule specific
  orchestrator that delegates to email + push.

The two state primitives live in ``app.state`` (accessed via ``_state.*``):

* ``_notification_threads_lock`` - ``threading.Lock`` guarding
  ``_notification_threads``.
* ``_notification_threads`` - list of in-flight delivery threads.

**Pool-A rebind (in ``app/main.py``):** every helper is re-bound as
``main.<orig_name>``. The two originally-underscored names
(``_deliver_alert_notifications`` and ``_deliver_sound_alert_notifications``)
are aliased back to their underscored public names in the rebind
block so existing ``main.py`` body callers keep working without
modification (the new module exposes them as clean public APIs:
``deliver_alert_notifications`` and ``deliver_sound_alert_notifications``).

**Pool-C reach sites used by this module (each resolved lazily via
``import app.main as main``):**

* ``_state._notification_threads_lock``, ``_state._notification_threads``
  - state primitives in ``app.state`` (not reached via ``main.*``).
* ``_state.database`` - the singleton DB handle via ``app.state``.
* ``_state.auth`` - the AuthService singleton via ``app.state``
  (used by ``_alert_datetime_prefs``).
* ``main._rule_notify_active_now(...)``,
  ``main.compute_minimum_rule_confidence()`` - helpers on ``main.py``
  (reached via ``from app.main import ...`` inside function bodies).
* ``_format_alert_datetime`` and ``_alert_datetime_prefs`` are LOCAL
  to this module (not Pool C).
* ``main.render_live_snapshot_jpeg_overlay(...)`` - Phase-25 rebind
  from ``app.live_snapshot``.
* ``main.EmailAlertService``, ``main.EmailAlertError``,
  ``main.PushNotificationService``, ``main.PushNotificationError``
  - service classes + exception types reached via Pool A so tests can
  monkeypatch ``main.<ServiceName>`` consistently (mirrors the Pool-A
  contract documented in :mod:`app.api.__init__` and the proven
  pattern in :mod:`app.detection_state`, :mod:`app.event_debounce`,
  :mod:`app.detection_status`). Replaces the previous direct
  top-of-module imports (see commit history - the bypass produced a
  single failing pytest case post-Phase-28, fixed by restoring the
  Pool-A reach).

**Logger acquisition:** the module uses its OWN child logger via
``logging.getLogger('daygle.ai')`` (matching the Phase-26
``app.detection_state`` precedent - same name, same logging tree.
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
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import app.state as _state
from app.config_facades import (
    effective_ai_config,
    effective_cameras_config,
    effective_email_alert_settings,
    effective_push_notification_settings,
)
from app.email_alerts import EmailAlertService, EmailAlertError
from app.live_snapshot import render_live_snapshot_jpeg_overlay
from app.media_utils import safe_storage_path
from app.push_notifications import PushNotificationService, PushNotificationError

logger = logging.getLogger('daygle.ai')

_MIN_RULE_CONFIDENCE_TTL = 5.0
_min_rule_confidence_cache: tuple[float, float] | None = None
_per_camera_min_rule_confidence_cache: dict[str, tuple[float, float]] = {}
_min_rule_confidence_lock = threading.Lock()


def compute_minimum_rule_confidence(fallback: float | None = None, camera_settings: dict | None = None) -> float:
    """Return the lowest min_confidence across enabled object rules.

    Without ``camera_settings`` this is the global floor across ALL cameras
    (used by status readouts / snapshot overlays). When a camera's settings are
    passed, only that camera's zones are scanned, so a low-threshold rule on
    one camera can no longer drag every camera's detector floor down. A camera
    with no enabled object rules falls back to the AI-settings confidence.
    Motion rules are always skipped (motion is gated separately). The result
    is cached per camera (or globally for the no-camera form) for
    _MIN_RULE_CONFIDENCE_TTL seconds to avoid a database read on every
    detection frame (called at ~4 Hz per camera from the hot path).
    """
    global _min_rule_confidence_cache
    camera_key = ''
    if camera_settings is not None:
        camera_key = str(camera_settings.get('id') or camera_settings.get('name') or '').strip()
    cached = _min_rule_confidence_cache if not camera_key else None
    if cached is not None:
        cached_value, cached_at = cached
        if time.time() - cached_at < _MIN_RULE_CONFIDENCE_TTL:
            return cached_value
    with _min_rule_confidence_lock:
        if camera_key:
            cached = _per_camera_min_rule_confidence_cache.get(camera_key)
        else:
            cached = _min_rule_confidence_cache
        if cached is not None:
            cached_value, cached_at = cached
            if time.time() - cached_at < _MIN_RULE_CONFIDENCE_TTL:
                return cached_value
        if fallback is None:
            # ``0`` is a legitimate persisted confidence (the ONNX slider's
            # floor) and must NOT fall through the ``or 0.45`` truthiness trap
            # -- only a genuinely absent value (None) or a non-numeric config
            # entry falls back to the 0.45 default.
            raw_conf = effective_ai_config().get('confidence')
            try:
                fallback = 0.45 if raw_conf is None else float(raw_conf)
            except (TypeError, ValueError):
                fallback = 0.45
        min_conf: float = fallback
        cameras = [camera_settings] if camera_settings is not None else effective_cameras_config()
        for camera in cameras:
            for zone in camera.get('detection', {}).get('zones', []):
                for rule in zone.get('object_rules', []):
                    if not rule.get('enabled', True):
                        continue
                    if str(rule.get('label') or '').strip().lower() == 'motion':
                        continue
                    try:
                        conf = float(rule.get('min_confidence', fallback))
                        if conf < min_conf:
                            min_conf = conf
                    except (TypeError, ValueError):
                        pass
        result = min_conf
        if camera_key:
            _per_camera_min_rule_confidence_cache[camera_key] = (result, time.time())
        else:
            _min_rule_confidence_cache = (result, time.time())
        return result


# ``_alert_datetime_prefs`` and ``_now_hm_in_admin_tz`` live canonically in
# ``app.alerts`` (the foundational alert-time module; the 30s cache lives
# there). Here in ``app.alert_dispatch`` we define LOCAL thin delegates so
# bare-name lookups in this module's namespace (``_alert_datetime_prefs()``
# inside ``_now_hm_in_admin_tz`` and ``_format_alert_datetime``,
# ``_now_hm_in_admin_tz()`` inside ``_rule_notify_active_now``) honor any
# ``monkeypatch.setattr('app.alert_dispatch._alert_datetime_prefs', ...)``
# by rebinding THIS module's name. The patch would otherwise bypass the
# cache in ``app.alerts`` (because that module's bare-name lookup resolves
# in its OWN namespace, unaffected by patches on a sibling module).
def _alert_datetime_prefs() -> tuple[str, str, str]:
    """Local delegate. Honors ``app.alert_dispatch._alert_datetime_prefs``
    monkeypatches (bare-name lookup resolves in this module's namespace).
    Falls through to the canonical cached implementation in ``app.alerts``.
    """
    from app.alerts import _alert_datetime_prefs as _alerts_impl
    return _alerts_impl()


def _now_hm_in_admin_tz() -> str:
    """Local delegate. Computes ``HH:MM`` for ``now`` in the admin's timezone.

    Honors ``app.alert_dispatch._alert_datetime_prefs`` monkeypatches
    because the timezone lookup uses the bare-name binding in this
    module's namespace, NOT the cached value in ``app.alerts``.

    Uses module-level ``datetime`` / ``ZoneInfo`` imports (not
    ``from datetime import datetime`` inline) so that tests can
    ``monkeypatch.setattr('app.alert_dispatch.datetime', _FakeDateTime)``
    and still pin the now-time to a deterministic value.
    """
    tz_name, _, _ = _alert_datetime_prefs()  # bare name in this module
    try:
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, KeyError):
        now_local = datetime.now(timezone.utc)
    return now_local.strftime('%H:%M')


def _rule_notify_active_now(rule: dict[str, Any]) -> bool:
    """Return True if a rule's notify_start/notify_end window covers now.

    An empty or partial window means "notify any time". Uses
    ``_now_hm_in_admin_tz`` (defined alongside the cached admin prefs in
    ``app.alerts``) so the window is evaluated in the admin's timezone and
    matches the clock used by ``AlertEngine._is_active_now``.
    """
    start = str(rule.get('notify_start') or '').strip()
    end = str(rule.get('notify_end') or '').strip()
    if not start or not end or start == end:
        return True
    now_hm = _now_hm_in_admin_tz()
    if start <= end:
        return start <= now_hm <= end
    return now_hm >= start or now_hm <= end


def _format_alert_datetime(iso_str: str) -> str:
    """Format a UTC ISO timestamp using the admin user's preferences."""
    tz_name, date_fmt, time_fmt = _alert_datetime_prefs()
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt = dt.astimezone(ZoneInfo(tz_name))
        tz_label = dt.strftime('%Z')
    except (ZoneInfoNotFoundError, KeyError):
        dt = dt.astimezone(timezone.utc)
        tz_label = 'UTC'
    date_str = dt.strftime({'us': '%m/%d/%Y', 'au': '%d/%m/%Y'}.get(date_fmt, '%Y-%m-%d'))
    if time_fmt == '12h':
        hour = str(int(dt.strftime('%I')))
        time_str = f"{hour}{dt.strftime(':%M:%S %p')}"
    else:
        time_str = dt.strftime('%H:%M:%S')
    return f'{date_str} {time_str} {tz_label}'


def wait_for_pending_alert_notifications(timeout: float = 10.0) -> None:
    """Block until in-flight alert email/push deliveries finish (used by tests)."""
    deadline = time.time() + max(0.0, timeout)
    with _state._notification_threads_lock:
        pending = [thread for thread in _state._notification_threads if thread.is_alive()]
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
    email_settings = effective_email_alert_settings()
    if not email_settings.get('enabled'):
        logger.debug('Email alerts disabled globally; skipping event %s', event_id)
        return
    event = _state.database.get_event(event_id) or {}
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    camera_name = str(metadata.get('camera_name') or '').strip() or None
    created_at_raw = str(event.get('created_at') or '').strip()
    detected_at = _format_alert_datetime(created_at_raw) if created_at_raw else None
    rules_by_name = {str(rule.get('name')): rule for rule in rules or []}
    any_email_enabled = any(
        (
            (rule := rules_by_name.get(str(alert.get('rule_name')), {})).get('email_enabled')
            and _rule_notify_active_now(rule)
        )
        for alert in triggered
    )
    snapshot_bytes: bytes | None = None
    snapshot_path = str(event.get('snapshot_path') or '')
    if any_email_enabled and snapshot_path:
        try:
            snap_path = safe_storage_path(snapshot_path, roots=('snapshots_dir',))
            if snap_path is not None and snap_path.exists():
                raw_bytes = snap_path.read_bytes()
                db_detections = event.get('detections') or []
                _email_min_conf = compute_minimum_rule_confidence()
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
                snapshot_bytes = render_live_snapshot_jpeg_overlay(raw_bytes, overlay_detections)
        except Exception as exc:
            logger.debug('Failed to annotate snapshot for email alert event %s: %s', event_id, exc)
    mailer = EmailAlertService(email_settings)
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
        if not _rule_notify_active_now(rule):
            logger.debug(
                'Email skipped for event %s rule %r: outside notify window %s-%s '
                '(the detection still recorded; widen the rule notify window to email it)',
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
                snapshot_bytes=snapshot_bytes,
                triggered_labels=all_triggered_labels,
                detected_at=detected_at,
            )
        except EmailAlertError as exc:
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
    push_settings = effective_push_notification_settings()
    if not push_settings.get('enabled'):
        logger.debug('Push notifications disabled globally; skipping event %s', event_id)
        return
    event = _state.database.get_event(event_id) or {}
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    camera_name = str(metadata.get('camera_name') or '').strip() or None
    camera_id = str(metadata.get('camera_id') or '').strip() or None
    created_at_raw = str(event.get('created_at') or '').strip()
    detected_at = _format_alert_datetime(created_at_raw) if created_at_raw else None
    rules_by_name = {str(rule.get('name')): rule for rule in rules or []}
    notifier = PushNotificationService(push_settings)
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
        if not _rule_notify_active_now(rule):
            logger.debug(
                'Push skipped for event %s rule %r: outside notify window %s-%s '
                '(the detection still recorded; widen the rule notify window to push it)',
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
        except PushNotificationError as exc:
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
