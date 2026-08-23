"""Face detection rules: per-person alert configuration.

Each enrolled person can have a rule that fires email or push notifications
when they are detected on any camera.  An ``_unknown`` system rule covers
faces that do not match any enrolled person.

Stored under the ``face_detection_rules`` key in the settings database as::

    {"rules": [<rule>, ...]}

The live pipeline reads rules via :func:`enabled_rules_for_label` and
:func:`effective_face_detection_rules` — both reach ``app.state.database``
at call time to avoid stale caches.
"""

from __future__ import annotations

import logging
from typing import Any

import app.state as _state
from app.config_facades import effective_face_recognition_config

logger = logging.getLogger('daygle.ai')

_DEFAULT_RULES: dict[str, Any] = {'rules': []}

# The synthetic id for the "unknown person" system rule that covers
# faces not matching any enrolled person.
UNKNOWN_RULE_ID = '_unknown'

# Per-label cooldown tracks in the live pipeline keyed by camera_id,
# same shape as the dwell/still streaks.
_face_rule_cooldowns: dict[str, dict[str, float]] = {}
_face_rule_cooldown_lock_any: Any  # resolved lazily to avoid import-time lock

# Lazy lock — ``threading.Lock()`` must not run at import time before the
# module is fully initialised; ``threading`` is imported at the top of the
# file, so this is safe.
import threading as _threading


def _cooldown_lock() -> _threading.Lock:
    global _face_rule_cooldown_lock_any
    if _face_rule_cooldown_lock_any is None:
        _face_rule_cooldown_lock_any = _threading.Lock()
    return _face_rule_cooldown_lock_any


_face_rule_cooldown_lock_any = None  # type: ignore[assignment]


def effective_face_detection_rules() -> dict[str, Any]:
    """Read the stored face-detection-rules dict.

    Returns ``_DEFAULT_RULES`` when the setting is absent or the database
    is not yet initialised.
    """
    if _state.database is None:
        return dict(_DEFAULT_RULES)
    stored = _state.database.get_setting('face_detection_rules')
    if stored and isinstance(stored, dict):
        return stored
    return dict(_DEFAULT_RULES)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'} if value else default


def _validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Normalise a single rule dict into canonical shape."""
    return {
        'id': str(rule.get('id') or '').strip(),
        'person_id': rule.get('person_id'),
        'name': str(rule.get('name') or '').strip() or 'Unknown',
        'enabled': _coerce_bool(rule.get('enabled'), True),
        'email_enabled': _coerce_bool(rule.get('email_enabled'), False),
        'push_enabled': _coerce_bool(rule.get('push_enabled'), False),
        'email_recipients': str(rule.get('email_recipients') or '').strip(),
        'cooldown_minutes': max(0, int(rule.get('cooldown_minutes') or 5)),
    }


def validate_face_detection_rules(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a face-detection-rules payload."""
    raw_rules = payload.get('rules')
    if not isinstance(raw_rules, list):
        return {'rules': []}
    validated = []
    seen_ids: set[str] = set()
    for raw in raw_rules:
        rule = _validate_rule(raw)
        if not rule['id']:
            continue
        if rule['id'] in seen_ids:
            continue  # duplicate id — last-wins
        seen_ids.add(rule['id'])
        validated.append(rule)
    return {'rules': validated}


def enabled_unknown_rule() -> dict[str, Any] | None:
    """Return the ``_unknown`` system rule when it is enabled, else ``None``.

    The ``_unknown`` rule is the single place unknown-person alerting is
    configured (enable/disable, email + push toggles, recipients, cooldown).
    It replaced the legacy ``alert_unknown`` / ``alert_unknown_email``
    face-recognition settings, which the Face Rules tab superseded. Returns
    ``None`` when the rule is absent or disabled, so the unknown-face alert
    path (and email/push dispatch) can treat "no rule" as "don't alert".
    """
    rules = effective_face_detection_rules().get('rules') or []
    for rule in rules:
        if str(rule.get('id') or '') == UNKNOWN_RULE_ID:
            return rule if _coerce_bool(rule.get('enabled'), False) else None
    return None


def heal_legacy_unknown_alert_config() -> bool:
    """Migrate legacy unknown-face alert settings into an ``_unknown`` rule.

    Before the Face Rules tab could configure unknown-person alerts, they were
    governed by ``alert_unknown`` / ``alert_unknown_email`` on the
    face-recognition Settings form. Those keys are being removed, so an
    existing deployment that enabled them would silently stop alerting; this
    one-time heal copies the old configuration into an ``_unknown`` rule (when
    none exists yet) so behaviour is preserved across the migration. Returns
    ``True`` when a rule was seeded, ``False`` when nothing needed doing.
    """
    fr = effective_face_recognition_config()
    legacy_unknown = _coerce_bool(fr.get('alert_unknown'), False)
    legacy_email = str(fr.get('alert_unknown_email') or '').strip()
    if not legacy_unknown and not legacy_email:
        return False
    if _state.database is None:
        return False
    stored = effective_face_detection_rules()
    rules = stored.get('rules') or []
    if any(str(rule.get('id') or '') == UNKNOWN_RULE_ID for rule in rules):
        return False
    seeded = {
        'id': UNKNOWN_RULE_ID,
        'person_id': None,
        'name': 'Unknown Person',
        # Legacy ``alert_unknown`` governed both alert generation and push;
        # email additionally needed recipients to be set.
        'enabled': legacy_unknown,
        'email_enabled': bool(legacy_email),
        'push_enabled': legacy_unknown,
        'email_recipients': legacy_email,
        'cooldown_minutes': 5,
    }
    from app.auth import utc_now

    _state.database.set_setting('face_detection_rules', {'rules': rules + [seeded]}, utc_now())
    logger.warning(
        'Healed legacy unknown-face alert config: seeded the _unknown system '
        'rule (enabled=%s, email=%s, push=%s) from the removed '
        'alert_unknown/alert_unknown_email settings.',
        legacy_unknown, bool(legacy_email), legacy_unknown,
    )
    return True


def enabled_rules_for_label(label: str) -> dict[str, Any] | None:
    """Return the first enabled rule matching *label* (case-insensitive).

    ``label`` is the ``person_name`` annotation stamped by
    ``annotate_face_identities`` (e.g. ``"Alice"``) or ``"face"`` for the
    unknown-person system rule.  Returns ``None`` when no enabled rule
    matches.
    """
    rules = effective_face_detection_rules().get('rules') or []
    label_lower = label.strip().lower()
    for rule in rules:
        if not rule.get('enabled'):
            continue
        rule_name = str(rule.get('name') or '').strip().lower()
        if rule_name == label_lower:
            return rule
    return None


def known_face_rules_for_camera(camera_id: str, detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return alert dicts for face detections that match enabled face rules.

    Called after ``annotate_face_identities`` so each face detection carries
    ``person_id`` / ``person_name`` annotations (or is marked unknown).
    Returns a list of alert dicts in the same shape the live pipeline uses::

        {'rule_name': 'Alice', 'label': 'face', 'confidence': 0.8, 'message': '...'}

    One alert per unique ``track_id`` per camera (debounced via the cooldown
    dict so a lingering subject does not re-alert).
    """
    import time as _time

    if not effective_face_recognition_config().get('enabled'):
        return []

    now = _time.time()
    cooldowns = _face_rule_cooldowns.setdefault(camera_id, {})
    new_alerts: list[dict[str, Any]] = []
    # Unknown faces are handled separately by unknown_face_alerts() (gated on
    # the _unknown system rule); this function only handles KNOWN faces.
    for detection in detections:
        label = str(detection.get('label') or '').strip().lower()
        if label != 'face':
            continue
        if not detection.get('recognized'):
            continue
        track_id = detection.get('track_id')
        if track_id is None:
            continue
        # Use the person_name as the rule lookup key; falls back to
        # the synthetic UNKNOWN_RULE_ID if the name is missing.
        person_name = str(detection.get('person_name') or '').strip() or None
        if person_name is None:
            # Unknown face — handled by unknown_face_alerts(), skip here
            continue
        rule = enabled_rules_for_label(person_name)
        if rule is None:
            continue
        # Cooldown: ``cooldown_minutes`` between alerts for the same track
        cooldown_sec = max(0, int(rule.get('cooldown_minutes') or 5)) * 60
        last_fired = cooldowns.get(track_id, 0)
        if now - last_fired < cooldown_sec:
            continue
        cooldowns[track_id] = now
        new_alerts.append({
            'rule_name': person_name,
            'label': 'face',
            'confidence': float(detection.get('confidence') or 0),
            'message': f'Alert triggered: {person_name} detected',
        })
    # Prune stale cooldown entries so the dict cannot grow unbounded.
    stale = [tid for tid, ts in cooldowns.items() if now - ts > 3600 * 6]
    for tid in stale:
        cooldowns.pop(tid, None)
    return new_alerts


def face_rule_notify_active_now(rule: dict[str, Any]) -> bool:
    """Return ``True`` when the rule's email/push is globally enabled."""
    return bool(rule.get('email_enabled')) or bool(rule.get('push_enabled'))


def face_rule_email_recipients(rule: dict[str, Any]) -> list[str]:
    """Parse the comma-separated ``email_recipients`` field."""
    raw = str(rule.get('email_recipients') or '').strip()
    if not raw:
        return []
    return [addr.strip() for addr in raw.split(',') if addr.strip()]
