"""Shared alert-notification formatting.

Email (:mod:`app.email_alerts`) and push (:mod:`app.push_notifications`)
notifications describe the same alert, so they build the same title and body
from one place here. Keeping the layout in a single builder guarantees the two
channels stay identical (title case, camera suffix, field order) instead of
drifting as they did when each channel formatted its own text.

The builder returns every field both channels need: the subject/title line, the
plain-text body (used verbatim as the push body and the email ``text/plain``
part), and the individual pieces the email HTML table renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AlertContent:
    """Formatted notification content shared by the email and push channels."""

    subject: str
    headline: str
    subject_label: str
    alert_message: str
    camera_display: str
    zone_name: str
    detection_type: str
    rule_display: str
    detected_at_display: str | None
    all_triggers_line: str | None
    confidence: float
    event_id: int
    plain_text: str


def _ordered_unique_labels(triggered_labels: list[str] | None) -> list[str]:
    """De-duplicate the triggered labels case-insensitively, keeping order."""
    ordered: list[str] = []
    if triggered_labels:
        seen: set[str] = set()
        for raw in triggered_labels:
            label = str(raw or '').strip()
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(label)
    return ordered


def build_alert_content(
    alert: dict[str, Any],
    *,
    event_id: int,
    camera_name: str | None = None,
    triggered_labels: list[str] | None = None,
    detected_at: str | None = None,
) -> AlertContent:
    """Build the shared title + body for one alert notification.

    Labels are title-cased for display (``cat`` -> ``Cat``, ``Cat_Meow`` ->
    ``Cat Meow``) while the underlying label strings are left untouched for any
    downstream lookups. The body lists Camera, optional Zone, Detection Type,
    Rule, optional Detected time, optional All-triggers, Confidence and Event ID
    - the same order the email HTML table renders.
    """
    camera_display = str(camera_name or '').strip() or 'Unknown camera'

    ordered_labels = _ordered_unique_labels(triggered_labels)
    primary_label = str(alert.get('label', 'object') or 'object').strip() or 'object'
    display_labels = [label.replace('_', ' ').title() for label in ordered_labels]
    display_primary = primary_label.replace('_', ' ').title()
    subject_label = ', '.join(display_labels) if display_labels else display_primary

    subject = f"Daygle AI Camera Alert: {subject_label} Detected ({camera_display})"

    multi = len(display_labels) > 1
    headline = f"{subject_label} Detected" if multi else subject_label
    all_triggers_line = f"All triggers: {subject_label}" if multi else None

    detected_at_display = str(detected_at).strip() if detected_at else None

    # Detection type from the alert label: motion, a configured sound class
    # (e.g. ``dog_bark``), or otherwise an object. Membership is tested against
    # the authoritative ``SOUND_CLASSES`` catalogue rather than an underscore
    # heuristic: the old heuristic misclassified ``doorbell`` (no underscore ->
    # "Object") and ``car_alarm`` (excluded by the ``car`` prefix -> "Object"),
    # and inversely tagged underscored object labels (e.g. ``traffic_light``)
    # as "Sound". Imported lazily to keep this pure formatter free of a
    # module-level dependency on the sound backend (mirrors app.api.sound_router).
    label_val = str(alert.get('label') or '').strip()
    label_lower = label_val.lower()
    if label_lower == 'motion':
        detection_type = 'Motion'
    else:
        from app.sound_detector import SOUND_CLASSES
        detection_type = 'Sound' if label_lower in SOUND_CLASSES else 'Object'

    # Zone (and a fallback label) come from the canonical
    # "Camera / Zone / Label" rule name when present.
    zone_name = ''
    rule_name = str(alert.get('rule_name') or '').strip()
    if ' / ' in rule_name:
        parts = rule_name.split(' / ')
        if len(parts) >= 2:
            zone_name = parts[1].strip()
        if len(parts) >= 3 and not label_val:
            label_val = parts[-1].strip()
    if not label_val:
        label_val = detection_type
    rule_display = label_val.replace('_', ' ').title()

    alert_message = str(alert.get('message') or 'Alert triggered.').title()
    confidence = float(alert.get('confidence') or 0)

    plain_lines = [alert_message, '', f"Camera: {camera_display}"]
    if zone_name:
        plain_lines.append(f"Zone: {zone_name}")
    plain_lines.append(f"Detection Type: {detection_type}")
    plain_lines.append(f"Rule: {rule_display}")
    if detected_at_display:
        plain_lines.append(f"Detected: {detected_at_display}")
    if all_triggers_line:
        plain_lines.append(all_triggers_line)
    plain_lines.append(f"Confidence: {confidence:.2%}")
    plain_lines.append(f"Event ID: {event_id}")
    plain_text = '\n'.join(plain_lines)

    return AlertContent(
        subject=subject,
        headline=headline,
        subject_label=subject_label,
        alert_message=alert_message,
        camera_display=camera_display,
        zone_name=zone_name,
        detection_type=detection_type,
        rule_display=rule_display,
        detected_at_display=detected_at_display,
        all_triggers_line=all_triggers_line,
        confidence=confidence,
        event_id=event_id,
        plain_text=plain_text,
    )
