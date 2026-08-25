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
import math
import threading
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
# Guards the read-and-claim of a track's cooldown slot (and the stale-entry
# prune) in ``known_face_rules_for_camera`` so two threads processing the same
# camera concurrently cannot both see an expired window and double-alert, nor
# mutate one camera's dict while another iterates it. Built at import like every
# other lock in the codebase: constructing a ``threading.Lock`` at import is
# safe, and unlike a lazily built one it cannot race two callers into two
# different lock objects -- which would silently defeat the guard.
_face_rule_cooldown_lock = threading.Lock()


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


def _coerce_optional_float(value: Any) -> float | None:
    """Parse an optional 0-1 float; blank / invalid / out-of-range -> ``None``.

    ``None`` means "no per-rule gate" -- the rule alerts on any detection the
    detector already reports (which itself is gated by the global Face
    Confidence setting).
    """
    if value is None:
        return None
    raw = str(value).strip()
    if raw == '':
        return None
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) and 0 <= num <= 1 else None


def _coerce_non_negative_int(value: Any, default: int = 5) -> int:
    """Return a safe integer for user-controlled rule settings."""
    if isinstance(value, bool) or value in (None, ''):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _coerce_detection_confidence(value: Any) -> float:
    """Convert detector confidence to a finite value for rule comparisons."""
    try:
        confidence = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return confidence if math.isfinite(confidence) else 0.0


def _normalize_recipients_field(value: Any) -> str:
    """Normalise ``email_recipients`` to a clean comma-separated string.

    Handles: list, comma-separated string, or a corrupted Python repr string
    like ``"['glen@daygle.net']"`` that was stored when ``str()`` was called
    on a list before the array-to-string fix.
    """
    if isinstance(value, list):
        parts = value
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith('['):
            # Corrupted Python list repr — strip brackets and quotes
            parts = [s.strip().strip("'\"") for s in stripped.strip('[]').split(',')]
        else:
            parts = stripped.split(',')
    else:
        parts = []
    return ', '.join(
        addr.strip() for addr in parts
        if addr.strip() and '@' in addr
    )


def _validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Normalise a single rule dict into canonical shape."""
    return {
        'id': str(rule.get('id') or '').strip(),
        'person_id': rule.get('person_id'),
        'name': str(rule.get('name') or '').strip() or 'Unknown',
        'enabled': _coerce_bool(rule.get('enabled'), True),
        'email_enabled': _coerce_bool(rule.get('email_enabled'), False),
        'push_enabled': _coerce_bool(rule.get('push_enabled'), False),
        'email_recipients': _normalize_recipients_field(rule.get('email_recipients')),
        'cooldown_minutes': _coerce_non_negative_int(rule.get('cooldown_minutes')),
        'min_confidence': _coerce_optional_float(rule.get('min_confidence')),
        # Optional scoping (People card on the Zones page). Empty means the
        # legacy global behaviour: the rule applies on every camera / any
        # zone. A zone-scoped unknown rule uses an id of ``_unknown:<zone>``
        # so multiple zones can each carry their own stranger-alert config.
        'camera_id': str(rule.get('camera_id') or '').strip(),
        'zone_id': str(rule.get('zone_id') or '').strip(),
    }


def rule_scope_matches(
    rule: dict[str, Any], camera_id: str = '', zone_id: str = ''
) -> bool:
    """True when *rule* applies to this camera/zone combination.

    An empty ``camera_id``/``zone_id`` on the rule means "any"; a set value
    must equal the detection's stamped camera/zone exactly.
    """
    rule_camera = str(rule.get('camera_id') or '').strip()
    rule_zone = str(rule.get('zone_id') or '').strip()
    if rule_camera and rule_camera != str(camera_id or '').strip():
        return False
    if rule_zone and rule_zone != str(zone_id or '').strip():
        return False
    return True


def validate_face_detection_rules(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a face-detection-rules payload."""
    if not isinstance(payload, dict):
        return {'rules': []}
    raw_rules = payload.get('rules')
    if not isinstance(raw_rules, list):
        return {'rules': []}
    validated = []
    seen_ids: set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, dict):
            continue
        rule = _validate_rule(raw)
        if not rule['id']:
            continue
        if rule['id'] in seen_ids:
            continue  # duplicate id — first entry wins
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


def enabled_rules_for_label(
    label: str, camera_id: str = '', zone_id: str = ''
) -> dict[str, Any] | None:
    """Return the first enabled rule matching *label* (case-insensitive).

    ``label`` is the ``person_name`` annotation stamped by
    ``annotate_face_identities`` (e.g. ``"Alice"``). ``camera_id`` /
    ``zone_id`` scope the lookup: rules carrying a camera/zone value only
    match detections stamped with that same camera/zone (legacy unscoped
    rules match everywhere). Returns ``None`` when no enabled rule matches.
    """
    rules = effective_face_detection_rules().get('rules') or []
    label_lower = label.strip().lower()
    for rule in rules:
        if not _coerce_bool(rule.get('enabled'), False):
            continue
        rule_name = str(rule.get('name') or '').strip().lower()
        if rule_name != label_lower:
            continue
        if not rule_scope_matches(rule, camera_id, zone_id):
            continue
        return rule
    return None


def is_unknown_rule(rule: dict[str, Any]) -> bool:
    """True for unknown-person system rules (global ``_unknown`` or a
    zone-scoped ``_unknown:<zone>`` variant created by the Zones page)."""
    rid = str(rule.get('id') or '')
    return rid == UNKNOWN_RULE_ID or rid.startswith(f'{UNKNOWN_RULE_ID}:')


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
        # Zone-scoped rules only fire inside their zone: the live pipeline
        # stamps face detections with their containing zone before calling.
        rule = enabled_rules_for_label(
            person_name,
            camera_id=camera_id,
            zone_id=str(detection.get('zone_id') or ''),
        )
        if rule is None:
            continue
        # Per-rule confidence gate: when the rule sets ``min_confidence``, only
        # detections at/above it trigger the alert. Blank = alert on any
        # detection the detector already reports (the global Face Confidence
        # setting still applies at the detector itself).
        det_conf = _coerce_detection_confidence(detection.get('confidence'))
        normalized_min_conf = _coerce_optional_float(rule.get('min_confidence'))
        if normalized_min_conf is not None and det_conf < normalized_min_conf:
            continue
        # Cooldown: ``cooldown_minutes`` between alerts for the same track.
        # Read-and-claim the slot under the module's cooldown lock so two
        # callers processing the same camera concurrently (thread overlap
        # during a camera restart, API-triggered passes) cannot BOTH see an
        # expired window between the read and the write and double-alert.
        cooldown_sec = max(0, int(rule.get('cooldown_minutes') or 5)) * 60
        with _face_rule_cooldown_lock:
            last_fired = cooldowns.get(track_id, 0)
            if now - last_fired < cooldown_sec:
                continue
            cooldowns[track_id] = now
        new_alerts.append({
            'rule_name': person_name,
            # Exact rule id so dispatch resolves recipients for THIS rule even
            # when several scoped rules share the same person name.
            'face_rule_id': str(rule.get('id') or ''),
            'zone_id': str(detection.get('zone_id') or ''),
            'label': 'face',
            'confidence': det_conf,
            'message': f'Alert triggered: {person_name} detected',
        })
    # Prune stale cooldown entries so the dict cannot grow unbounded. Under the
    # same lock as the claim above: without it, iterating this camera's dict here
    # while a concurrent caller claims a new slot could raise "dict changed size
    # during iteration".
    with _face_rule_cooldown_lock:
        stale = [tid for tid, ts in cooldowns.items() if now - ts > 3600 * 6]
        for tid in stale:
            cooldowns.pop(tid, None)
    return new_alerts


def face_rule_notify_active_now(rule: dict[str, Any]) -> bool:
    """Return ``True`` when the rule's email/push is globally enabled."""
    return _coerce_bool(rule.get('email_enabled'), False) or _coerce_bool(rule.get('push_enabled'), False)


def face_rule_email_recipients(rule: dict[str, Any]) -> list[str]:
    """Parse the ``email_recipients`` field into a list of addresses.

    Handles both clean comma-separated strings and corrupted Python repr
    strings (e.g. ``"['glen@daygle.net']"``) that were stored before the
    array-to-string fix in ``_normalize_recipients_field``.
    """
    raw = _normalize_recipients_field(rule.get('email_recipients'))
    if not raw:
        return []
    return [addr.strip() for addr in raw.split(',') if addr.strip()]
