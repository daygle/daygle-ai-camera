"""AI relabel: a rejected alert's object can raise the alert it really is.

AI verification asks "is a real person here?". When the answer is no, the
same question also asks what the object is instead, from the labels other
alert rules in the same zones watch (``relabel_candidates``). A rejected
"person" the model calls a cat then fires the zone's cat rules
(``relabel_alerts``), as a cat the detector had reported would have.

The relabel rides on the verification request, so it costs no extra model
call. A relabelled alert skips the rule's confidence window: the detector's
score was for the wrong label, and the model, not that score, vouches for
the new one. Every other rule condition still applies.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import app.state as _state
from app.zone_schema import canonical_label, detection_within_box_limits, label_matches

logger = logging.getLogger('daygle.ai')

MAX_RELABEL_CANDIDATES = 8
# Never relabel to these: no object to alert on, or an identity rule.
_NOT_RELABELS = frozenset({'motion', 'face'})


def _notifies(rule: dict[str, Any]) -> bool:
    return bool(rule.get('enabled', True) and (rule.get('email_enabled') or rule.get('push_enabled')))


def _source_alerts(triggered: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    from app.ai_verification import _is_verifiable, _verify_label

    return [alert for alert in triggered if _is_verifiable(alert) and _verify_label(alert) == label]


def _zones(alerts: list[dict[str, Any]], rules_by_name: dict[str, dict[str, Any]]) -> set[str]:
    return {
        str(rules_by_name[name].get('zone_id') or '')
        for name in (str(alert.get('rule_name') or '') for alert in alerts) if name in rules_by_name
    }


def relabel_candidates(
    triggered: list[dict[str, Any]], labels: list[str], rules: list[dict[str, Any]] | None,
) -> dict[str, list[str]]:
    """Per verified label, what its object may be instead: the objects other
    notifying rules in the same zones watch. A label with none is left out, so
    its check asks no relabel question."""
    rules_by_name = {str(rule.get('name') or ''): rule for rule in rules or []}
    candidates: dict[str, list[str]] = {}
    for label in labels:
        sources = _source_alerts(triggered, label)
        zones = _zones(sources, rules_by_name)
        detected = {canonical_label(alert.get('label')) for alert in sources}
        names = sorted({
            canonical_label(rule.get('object')) for rule in rules or []
            if str(rule.get('zone_id') or '') in zones and _notifies(rule)
            and canonical_label(rule.get('object')) not in _NOT_RELABELS
            and not label_matches(label, rule.get('object'))
            and not any(label_matches(name, rule.get('object')) for name in detected)
        } - {''})
        if names:
            candidates[label] = names[:MAX_RELABEL_CANDIDATES]
    return candidates


def _source_box(event: dict[str, Any], alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """The strongest stored detection of the rejected alerts' label (its box)."""
    names = {canonical_label(alert.get('label')) for alert in alerts}
    boxes = [detection for detection in event.get('detections') or []
             if canonical_label(detection.get('label')) in names]
    best = max(boxes, key=lambda detection: float(detection.get('confidence') or 0.0), default=None)
    if best is None:
        return {}
    return {'box': {key: best.get(key) for key in ('x', 'y', 'width', 'height')}}


def relabel_alerts(
    triggered: list[dict[str, Any]],
    record: dict[str, Any],
    rules: list[dict[str, Any]] | None,
    event: dict[str, Any],
    event_id: int,
    camera_id: Any,
) -> list[dict[str, Any]]:
    """Alerts for what the rejected objects really are; their rows are stored.

    Skipped for an object the detector also reported on the event (its own
    rules already ran) and for one already on record for this camera, which
    alerted when it was first seen.
    """
    from app.alert_dispatch import _rule_notify_active_now
    from app.alerts import AlertEngine
    from app.event_presence import claim_presence
    from app.label_groups import cached_label_groups

    rules_by_name = {str(rule.get('name') or ''): rule for rule in rules or []}
    reported = {canonical_label(detection.get('label')) for detection in event.get('detections') or []}
    groups = cached_label_groups()
    alerts: list[dict[str, Any]] = []
    for label, result in (record.get('labels') or {}).items():
        instead = canonical_label(result.get('instead'))
        if not instead or result.get('present') is not False:
            continue
        # A cat the detector reported (or any member of a group named) is
        # already handled by its own rules.
        if any(label_matches(name, instead) or label_matches(instead, name) for name in reported):
            continue
        sources = _source_alerts(triggered, label)
        zones = _zones(sources, rules_by_name)
        box = _source_box(event, sources)
        matching = [
            rule for rule in rules or []
            if str(rule.get('zone_id') or '') in zones and _notifies(rule)
            and label_matches(instead, rule.get('object'))
            and AlertEngine._is_active_now(rule)
            and _rule_notify_active_now(rule.get('schedule') or rule)
            and detection_within_box_limits(box, rule)
        ]
        if not matching:
            continue
        if not claim_presence(str(camera_id), instead, recording_id=event.get('recording_id')):
            continue
        source = sources[0]
        confidence = float(source.get('confidence') or 0.0)
        for rule in matching:
            alert: dict[str, Any] = {
                'rule_name': str(rule.get('name') or instead),
                'label': instead,
                'confidence': confidence,
                'message': f"Alert triggered: {instead} detected (AI relabel; the detector reported {source.get('label')})",
                'ai_relabel_from': canonical_label(source.get('label')),
            }
            rule_label = canonical_label(rule.get('object'))
            if rule_label != instead and rule_label in groups:
                alert['rule_label'] = rule_label
            if source.get('motion_state') in ('moving', 'still'):
                alert['motion_state'] = source['motion_state']
            alerts.append(alert)
    if alerts:
        _store(alerts, event, event_id)
    return alerts


def _store(alerts: list[dict[str, Any]], event: dict[str, Any], event_id: int) -> None:
    database = _state.database
    now = datetime.now(timezone.utc).isoformat()
    try:
        for alert in alerts:
            database.add_alert(
                created_at=now, rule_name=alert['rule_name'], event_id=int(event_id), label=alert['label'],
                confidence=alert['confidence'], message=alert['message'], recording_id=event.get('recording_id'),
            )
        database.mark_event_alert_triggered(int(event_id))
    except Exception as exc:  # noqa: BLE001 - the notification still goes out
        logger.warning('Could not store AI relabel alert for event %s: %s', event_id, exc)
    logger.info('AI relabel on event %s: %s', event_id,
                ', '.join(f"{alert['ai_relabel_from']} -> {alert['label']} ({alert['rule_name']})" for alert in alerts))
