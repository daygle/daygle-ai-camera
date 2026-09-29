"""AI tag alerts: alert when the vision model names something in a zone.

A zone's ``ai_tags`` rule (configured on the Zones page, delivery on the Alerts
page) lists terms such as ``ladder`` or ``parcel``. After the local model has
described an event (app.ai_verification), the rule fires when the event is in
the zone and the model named one of the terms:

* ``tags``        - in its tag list,
* ``description`` - in its sentence (whole words, plural forms included),
* ``both``        - in either.

Before a rule fires, each matched term gets a second, independent check: the
model is asked the same yes/no question alert verification uses ("is a real
cat visible?"), on a close-up when the detector boxed that object. A caption
from a small model can misname a distant blob (a magpie became "a black
cat"); a term it then answers "no" to does not alert. If that check cannot
run, the alert is sent, as with verification.

The alert is attached to the described event (an alert_history row, and the
event is flagged as alerted) and delivered through the normal notification
queue, led by the description. It is labelled *unconfirmed*: unlike object
detection, nothing but the language model saw the object.

Cameras with an enabled rule have every event described, whatever the global
"Describe Events" mode, because a rule can only see described events.
Backfilled (past) events never fire rules.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import app.state as _state

logger = logging.getLogger('daygle.ai')

_cooldowns: dict[str, float] = {}
_cooldown_lock = threading.Lock()


def _camera_settings(camera_id: Any) -> dict[str, Any] | None:
    if not camera_id:
        return None
    try:
        from app.config_facades import effective_cameras_config

        for camera in effective_cameras_config():
            if str(camera.get('id') or '') == str(camera_id):
                return camera
    except Exception:  # noqa: BLE001 - a settings read must not break describing
        return None
    return None


def zone_rules(camera: dict[str, Any] | None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``(zone, rule)`` for every enabled AI tag rule with terms on a camera."""
    pairs = []
    for zone in ((camera or {}).get('detection') or {}).get('zones') or []:
        rule = zone.get('ai_tags')
        if zone.get('enabled', True) and isinstance(rule, dict) and rule.get('enabled', True) and rule.get('tags'):
            pairs.append((zone, rule))
    return pairs


def camera_has_rules(camera_id: Any) -> bool:
    return bool(zone_rules(_camera_settings(camera_id)))


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith('ies'):
        return word[:-3] + 'y'
    if len(word) > 3 and word.endswith('es') and word[-3] in 'sxz':
        return word[:-2]
    if len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
        return word[:-1]
    return word


def _normalise(term: str) -> str:
    return ' '.join(_singular(word) for word in str(term or '').lower().replace('_', ' ').split())


def _in_text(term: str, text: str) -> bool:
    """Whole-word (or phrase) match, tolerant of plurals: "ladder" ~ "ladders"."""
    words = [re.escape(_singular(word)) for word in term.lower().split()]
    if not words:
        return False
    pattern = r'\b' + r'\W+'.join(f'{word}(?:e?s)?' for word in words) + r'\b'
    return re.search(pattern, text.lower()) is not None


def matched_terms(rule: dict[str, Any], tags: list[str], description: str) -> list[str]:
    """The rule's terms that the model named, in rule order."""
    mode = str(rule.get('match') or 'both')
    tag_set = {_normalise(tag) for tag in tags or []}
    hits = []
    for term in rule.get('tags') or []:
        in_tags = _normalise(term) in tag_set
        in_description = _in_text(term, description or '')
        if (mode == 'tags' and in_tags) or (mode == 'description' and in_description) or (
                mode == 'both' and (in_tags or in_description)):
            hits.append(term)
    return hits


def _full_frame(zone: dict[str, Any]) -> bool:
    try:
        x, y = float(zone.get('x') or 0), float(zone.get('y') or 0)
        width, height = float(zone.get('width') or 0), float(zone.get('height') or 0)
    except (TypeError, ValueError):
        return False
    return x <= 0.02 and y <= 0.02 and x + width >= 0.98 and y + height >= 0.98


def event_in_zone(event: dict[str, Any], zone: dict[str, Any]) -> bool:
    """True when any of the event's detections (objects or motion) was in the
    zone, or the zone covers the whole frame."""
    if _full_frame(zone):
        return True
    names = {str(zone.get('name') or '').strip().lower(), str(zone.get('id') or '').strip().lower()} - {''}
    return any(
        str(detection.get('zone_name') or detection.get('zone_id') or '').strip().lower() in names
        for detection in event.get('detections') or []
    )


def _cooldown_ok(key: str, seconds: int, *, spend: bool = True) -> bool:
    now = time.monotonic()
    with _cooldown_lock:
        last = _cooldowns.get(key)
        if last is not None and now - last < seconds:
            return False
        if spend:
            _cooldowns[key] = now
        return True


def _confirm_terms(event: dict[str, Any], terms: list[str], camera_name: str,
                   cache: dict[str, bool]) -> list[str]:
    """The terms the model still sees when asked directly. Fails open."""
    from app import ai_verification as av

    pending = [term for term in terms if term not in cache]
    if pending:
        settings = av.effective_ai_verification_settings()
        image = av._read_snapshot(event)
        if image is None:
            cache.update(dict.fromkeys(pending, True))
        else:
            verifier = av.VisionVerifier(settings)
            detections = event.get('detections') or []
            for term in pending:
                focused = av.focus_image(image, detections, term.lower()) if settings.get('focus_crop', True) else image
                try:
                    present, reason = verifier.ask(focused, term, camera_name, source='An automatic caption')
                except av.VerificationError as exc:
                    logger.warning('AI tag check unavailable for event %s (%s): %s; alerting unchecked.',
                                   event.get('id'), term, exc)
                    present = True
                else:
                    if not present:
                        logger.info('AI tag alert suppressed for event %s: %s not confirmed (%s)',
                                    event.get('id'), term, reason or 'not present')
                cache[term] = present
    return [term for term in terms if cache[term]]


def evaluate_event(event_id: int, event: dict[str, Any], record: dict[str, Any]) -> int:
    """Fire every matching AI tag rule for a freshly described event.

    Returns the number of alerts fired. Never raises.
    """
    try:
        metadata = event.get('metadata') or {}
        camera = _camera_settings(metadata.get('camera_id'))
        fired = 0
        confirmed: dict[str, bool] = {}
        for zone, rule in zone_rules(camera):
            if not event_in_zone(event, zone):
                continue
            hits = matched_terms(rule, record.get('tags') or [], record.get('text') or '')
            if not hits:
                continue
            if _fire(event_id, event, camera, zone, rule, hits, record, confirmed):
                fired += 1
        return fired
    except Exception:  # noqa: BLE001 - alerts must never break the AI pool job
        logger.exception('AI tag alert evaluation failed for event %s', event_id)
        return 0


def _fire(event_id: int, event: dict[str, Any], camera: dict[str, Any], zone: dict[str, Any],
          rule: dict[str, Any], hits: list[str], record: dict[str, Any], confirmed: dict[str, bool]) -> bool:
    from app.alert_dispatch import (
        _rule_notify_active_now,
        deliver_alert_notifications,
        submit_alert_notification,
    )
    from app.utils import normalize_bool_setting, normalize_email_recipients

    zone_name = str(zone.get('name') or zone.get('id') or 'zone')
    camera_name = str(camera.get('name') or camera.get('id') or 'camera')
    rule_name = f"{zone_name} · {rule.get('name') or 'AI tag alert'}"
    notify_rule = {
        'name': rule_name,
        'email_enabled': normalize_bool_setting(rule.get('email_enabled'), False),
        'push_enabled': normalize_bool_setting(rule.get('push_enabled'), False),
        'email_recipients': normalize_email_recipients(rule.get('email_recipients') or []),
        'notify_start': str(rule.get('notify_start') or '').strip() or None,
        'notify_end': str(rule.get('notify_end') or '').strip() or None,
    }
    if not (notify_rule['email_enabled'] or notify_rule['push_enabled']):
        return False
    if not _rule_notify_active_now(notify_rule):
        return False
    # Cooldown is spent only by an alert that is actually sent, and checked
    # before the confirming model call so a cooling-down rule costs nothing.
    key = f"{camera.get('id')}::{zone.get('id') or zone.get('name')}::ai_tags"
    cooldown = int(rule.get('cooldown_seconds') or 0)
    if not _cooldown_ok(key, cooldown, spend=False):
        return False
    hits = _confirm_terms({**event, 'id': event_id}, hits, camera_name, confirmed)
    if not hits or not _cooldown_ok(key, cooldown):
        return False
    label = hits[0]
    terms = ', '.join(term.title() for term in hits)
    message = f'AI tag alert (unconfirmed): {terms} on {camera_name} ({zone_name})'
    database = _state.database
    database.add_alert(
        created_at=datetime.now(timezone.utc).isoformat(), rule_name=rule_name, event_id=int(event_id),
        label=label, confidence=0.0, message=message,
    )
    database.mark_event_alert_triggered(int(event_id))
    payload = {
        'rule_name': rule_name, 'label': label, 'confidence': 0.0, 'message': message,
        'ai_description': f"{message}. {record.get('text') or ''}".strip(),
        'ai_tag_alert': True,
    }
    submit_alert_notification(deliver_alert_notifications, [payload], int(event_id), [notify_rule])
    logger.info('AI tag alert on %s/%s for event %s: %s', camera_name, zone_name, event_id, terms)
    return True
