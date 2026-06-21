from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import app.state as _state

ALERT_DATETIME_PREFS_TTL_SECONDS = 30.0
_alert_datetime_prefs_cache: tuple[tuple[str, str, str], float] | None = None
_alert_datetime_prefs_lock = threading.Lock()


def _alert_datetime_prefs() -> tuple[str, str, str]:
    """Return (timezone_name, date_format, time_format) from the primary admin user.

    Result is cached for ``ALERT_DATETIME_PREFS_TTL_SECONDS`` to avoid hitting
    the auth store (a SQLite SELECT) on every alert-dispatch call. ``deliver_email_alerts``
    and ``deliver_push_notifications`` invoke ``_rule_notify_active_now`` once per
    triggered alert × matched rule, so uncached this read could fire dozens of
    SELECTs per multi-object event.
    """
    global _alert_datetime_prefs_cache
    cached = _alert_datetime_prefs_cache
    if cached is not None:
        cached_value, cached_at = cached
        if time.time() - cached_at < ALERT_DATETIME_PREFS_TTL_SECONDS:
            return cached_value
    with _alert_datetime_prefs_lock:
        cached = _alert_datetime_prefs_cache
        if cached is not None:
            cached_value, cached_at = cached
            if time.time() - cached_at < ALERT_DATETIME_PREFS_TTL_SECONDS:
                return cached_value
        try:
            users = _state.auth.list_users()
            admin = None
            for entry in users:
                if entry.get('role') == 'admin' and entry.get('is_active'):
                    admin = entry
                    break
            if admin is None:
                admin = next(iter(users), None)
            if admin:
                value = (
                    str(admin.get('timezone') or 'UTC'),
                    str(admin.get('date_format') or 'iso'),
                    str(admin.get('time_format') or '24h'),
                )
            else:
                value = ('UTC', 'iso', '24h')
        except Exception:
            value = ('UTC', 'iso', '24h')
        _alert_datetime_prefs_cache = (value, time.time())
        return value


def _clear_datetime_prefs_cache() -> None:
    """Drop the cached admin datetime prefs. Exposed for test teardown so
    per-test timezones (mutated via ``monkeypatch.setattr`` on
    ``_state.auth.list_users``) are picked up on the next call without
    waiting for the 30s TTL to expire.
    """
    global _alert_datetime_prefs_cache
    with _alert_datetime_prefs_lock:
        _alert_datetime_prefs_cache = None


def _now_hm_in_admin_tz() -> str:
    """Return ``HH:MM`` for the current time evaluated in the admin's timezone.

    Used by both ``AlertEngine._is_active_now`` (the rule's active_start /
    active_end detection window) and ``_rule_notify_active_now`` in
    ``alert_dispatch`` (the rule's notify_start / notify_end delivery
    window) so both gates use a single admin-local clock instead of mixing
    server-local time with admin-local time.
    """
    tz_name, _, _ = _alert_datetime_prefs()
    try:
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, KeyError):
        now_local = datetime.now(timezone.utc)
    return now_local.strftime('%H:%M')


class AlertEngine:
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules
        self.last_triggered: dict[str, float] = {}
        self._lock = threading.Lock()

    def process(self, detections: list[dict[str, Any]], rules: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        effective_rules = rules if rules is not None else self.rules
        alerts: list[dict[str, Any]] = []
        motion_detections = [detection for detection in detections if detection.get('motion_event')]
        for motion_detection in motion_detections:
            self._append_motion_alerts(alerts, motion_detection, effective_rules)

        for detection in detections:
            label = detection.get('label')
            if not isinstance(label, str) or not label:
                continue
            label_key = self._normalize_object_label(label)
            confidence = float(detection.get('confidence', 0))

            for rule in effective_rules:
                if not rule.get('enabled', True):
                    continue

                if self._is_motion_rule(rule):
                    continue

                if not self._is_active_now(rule):
                    continue

                if self._normalize_object_label(rule.get('object')) != label_key:
                    continue

                rule_zone_id = str(rule.get('zone_id') or '').strip()
                detection_zone_id = str(detection.get('zone_id') or '').strip()
                if rule_zone_id and rule_zone_id != detection_zone_id:
                    continue

                if confidence < float(rule.get('min_confidence', 0.5)):
                    continue

                rule_name = str(rule.get('name') or label)
                cooldown_key = str(rule.get('cooldown_key') or rule_name)
                cooldown = int(rule.get('cooldown_seconds', 60))

                now = time.time()
                with self._lock:
                    last = self.last_triggered.get(cooldown_key, 0)
                    if now - last < cooldown:
                        continue
                    self.last_triggered[cooldown_key] = now

                alerts.append({
                    'rule_name': rule_name,
                    'label': label,
                    'confidence': confidence,
                    'message': f'Alert triggered: {label} detected ({confidence:.2%})'
                })

        return alerts

    def _append_motion_alerts(self, alerts: list[dict[str, Any]], detection: dict[str, Any], rules: list[dict[str, Any]] | None = None) -> None:
        confidence = float(detection.get('confidence', 0))
        detection_zone_id = str(detection.get('zone_id') or '').strip()
        for rule in (rules if rules is not None else self.rules):
            if not rule.get('enabled', True) or not self._is_motion_rule(rule):
                continue
            if not self._is_active_now(rule):
                continue
            if confidence < float(rule.get('min_confidence', 0.0)):
                continue
            # Mirror the zone-id check used for object rules (line 43-46) so
            # a motion rule scoped to Zone A doesn't fire when motion is only
            # in Zone B.
            rule_zone_id = str(rule.get('zone_id') or '').strip()
            if rule_zone_id and rule_zone_id != detection_zone_id:
                continue

            rule_name = str(rule.get('name') or 'Motion')
            cooldown_key = str(rule.get('cooldown_key') or rule_name)
            cooldown = int(rule.get('cooldown_seconds', 60))
            now = time.time()
            with self._lock:
                last = self.last_triggered.get(cooldown_key, 0)
                if now - last < cooldown:
                    continue
                self.last_triggered[cooldown_key] = now
            alerts.append({
                'rule_name': rule_name,
                'label': 'motion',
                'confidence': confidence,
                'message': f'Alert triggered: motion detected ({confidence:.2%})',
            })

    @staticmethod
    def _is_motion_rule(rule: dict[str, Any]) -> bool:
        return AlertEngine._normalize_object_label(rule.get('object')) == 'motion'

    @staticmethod
    def _normalize_object_label(value: Any) -> str:
        label = str(value or '').strip().lower()
        aliases = {
            'human': 'person',
            'people': 'person',
            'pedestrian': 'person',
        }
        return aliases.get(label, label)

    @staticmethod
    def _is_active_now(rule: dict[str, Any]) -> bool:
        """Return True when the rule's detection/active window covers now.

        This gates whether the rule raises an alert at all (in-app, email and
        push). The separate email/push notification window (notify_start /
        notify_end) is applied later, only to email and push delivery.

        Evaluated in the admin's timezone (via ``_now_hm_in_admin_tz``) so the
        active window matches the admin's local clock. Previously this used
        ``datetime.now``, which is the host's local time -- a server running
        in UTC and an admin in ``America/Los_Angeles`` would suppress alerts
        during the admin's local night even when UTC is daytime.
        """
        start = rule.get('active_start')
        end = rule.get('active_end')
        start_text = str(start or '')
        end_text = str(end or '')
        if not start_text or not end_text or start_text == end_text:
            return True
        now = _now_hm_in_admin_tz()
        if start_text <= end_text:
            return start_text <= now <= end_text
        return now >= start_text or now <= end_text
