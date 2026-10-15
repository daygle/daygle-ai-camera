"""Face detection rules: per-person alert configuration.

Each enrolled person can have a rule that fires email or push notifications
when they are detected on any camera.  An ``_unknown`` system rule covers
faces that do not match any enrolled person.

Stored under the ``face_detection_rules`` key in the settings database as::

    {"rules": [<rule>, ...]}

The live pipeline reads rules via :func:`enabled_rule_for_person` and
:func:`effective_face_detection_rules` - both reach ``app.state.database``
at call time to avoid stale caches.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.config_facades import effective_face_recognition_config
from app.runtime_config import cached_snapshot
from app.utils import normalize_bool_setting as _coerce_bool

logger = logging.getLogger('daygle.ai')

_DEFAULT_RULES: dict[str, Any] = {'rules': []}

# The synthetic id for the "unknown person" system rule that covers
# faces not matching any enrolled person.
UNKNOWN_RULE_ID = '_unknown'


def effective_face_detection_rules() -> dict[str, Any]:
    """Read face rules through the generation-aware parsed settings cache."""
    return cached_snapshot(
        _state.database, 'face_detection_rules', _build_effective_face_detection_rules,
    )


def _build_effective_face_detection_rules() -> dict[str, Any]:
    if _state.database is None:
        return dict(_DEFAULT_RULES)
    stored = _state.database.get_setting('face_detection_rules')
    if stored and isinstance(stored, dict):
        return stored
    return dict(_DEFAULT_RULES)


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
            # Corrupted Python list repr - strip brackets and quotes
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
        'min_confidence': _coerce_optional_float(rule.get('min_confidence')),
        # Optional scoping (People card on the Zones page). Empty means the
        # rule applies on every camera / any zone. A zone-scoped unknown rule uses an id of ``_unknown:<zone>``
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
    """Validate a face-detection-rules payload.

    A payload without a ``rules`` list is rejected rather than normalised to an
    empty rule set: the route persists the result, so a malformed request used
    to silently delete every face rule.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get('rules'), list):
        raise HTTPException(status_code=400, detail='Face detection rules must be an object with a "rules" list.')
    raw_rules = payload['rules']
    validated = []
    seen_ids: set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, dict):
            continue
        rule = _validate_rule(raw)
        if not rule['id']:
            continue
        if rule['id'] in seen_ids:
            continue  # duplicate id - first entry wins
        seen_ids.add(rule['id'])
        validated.append(rule)
    return {'rules': validated}


def enabled_rule_for_person(
    person_id: Any, camera_id: str = '', zone_id: str = '',
) -> dict[str, Any] | None:
    """Return the first enabled rule for this recognised person.

    Rules match on ``person_id`` only, so renaming someone keeps their rule
    working and two people who share a name never trigger each other's rules.
    Unknown-person system rules never match a recognised face.

    ``camera_id`` / ``zone_id`` scope the lookup: rules carrying a camera/zone
    value only match detections stamped with that same camera/zone (unscoped
    rules match everywhere). Returns ``None`` when no enabled rule matches.
    """
    wanted_person = str(person_id).strip() if person_id not in (None, '') else ''
    if not wanted_person:
        return None
    rules = effective_face_detection_rules().get('rules') or []
    for rule in rules:
        if not _coerce_bool(rule.get('enabled'), False):
            continue
        if is_unknown_rule(rule):
            continue
        rule_person = rule.get('person_id')
        if rule_person in (None, '') or str(rule_person).strip() != wanted_person:
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

    Every matching face is returned, each carrying its ``face_rule_id``. The
    live pipeline's presence gate (:mod:`app.event_presence`) turns these into
    ONE alert per person while they stay in view -- no cooldown.
    """
    if not effective_face_recognition_config().get('enabled'):
        return []

    new_alerts: list[dict[str, Any]] = []
    # Unknown faces are handled separately by unknown_face_alerts() (gated on
    # the _unknown system rule); this function only handles KNOWN faces.
    for detection in detections:
        label = str(detection.get('label') or '').strip().lower()
        if label != 'face':
            continue
        if not detection.get('recognized'):
            continue
        person_name = str(detection.get('person_name') or '').strip() or None
        if person_name is None:
            # Unknown face - handled by unknown_face_alerts(), skip here
            continue
        # Zone-scoped rules only fire inside their zone: the live pipeline
        # stamps face detections with their containing zone before calling.
        rule = enabled_rule_for_person(
            detection.get('person_id'),
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
