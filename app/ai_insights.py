"""AI Insights: what the vision model's verdicts say about detection settings.

Two read-only reports for the Intelligence → AI page, built from data the
AI checks already store:

* **Tuning suggestions.** Every AI check writes a verdict on each detection
  (``detections.ai_status``, see app.db.false_positives). When the model
  keeps rejecting a camera's low-confidence detections of one label (say,
  "person" on the Driveway below 55%) while confirming the ones above, the
  rule's minimum confidence is set too low. :func:`tuning_suggestions`
  finds the lowest confidence that drops mostly rejections and almost no
  confirmed detections, and :func:`apply_min_confidence` raises the rule to
  it on request.
* **Frequent AI tags.** Objects the model keeps naming that the detector has
  no class for (a parcel, a ladder) and no AI tag alert watches yet, as
  candidates for an AI tag alert (:func:`frequent_tags`).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

import app.state as _state

logger = logging.getLogger('daygle.ai.analysis')

TUNING_WINDOW_DAYS = 14
# Too few verdicts say nothing; a suggestion needs this many judged
# detections for the camera and label, and this many below the new floor.
MIN_JUDGED = 12
MIN_BELOW = 5
# Below the suggested floor at least this share was rejected...
REJECTED_SHARE_BELOW = 0.75
# ...and raising the floor loses at most this share of confirmed detections.
MAX_CONFIRMED_LOST = 0.2
# Floors considered, 0.30 to 0.90 in 0.05 steps.
CANDIDATE_FLOORS = tuple(round(0.30 + step * 0.05, 2) for step in range(13))

TAGS_WINDOW_DAYS = 7
MIN_TAG_COUNT = 3
MAX_TAGS_PER_CAMERA = 5


def _cameras() -> list[dict[str, Any]]:
    from app.config_facades import effective_cameras_config

    try:
        return list(effective_cameras_config())
    except Exception:  # noqa: BLE001 - settings unreadable: no suggestions rather than an error page
        logger.warning('Could not read camera settings for AI insights', exc_info=True)
        return []


def _camera_name(cameras: list[dict[str, Any]], camera_id: str) -> str:
    camera = next((c for c in cameras if str(c.get('id') or '') == camera_id), None)
    return str((camera or {}).get('name') or camera_id)


def current_min_confidence(camera: dict[str, Any] | None, label: str) -> float | None:
    """The lowest min confidence of the enabled rules for ``label`` on the camera."""
    values = [
        float(rule.get('min_confidence'))
        for zone in ((camera or {}).get('detection') or {}).get('zones') or []
        if zone.get('enabled', True)
        for rule in zone.get('object_rules') or []
        if str(rule.get('label') or '').strip().lower() == label and rule.get('enabled', True)
        and isinstance(rule.get('min_confidence'), (int, float))
    ]
    return min(values) if values else None


def suggest_floor(judged: list[tuple[float, str]]) -> dict[str, Any] | None:
    """The suggested minimum confidence for one camera and label, or None.

    ``judged`` is ``(confidence, status)`` for each detection the AI
    judged: ``confirmed`` or ``rejected``. Among the floors below which the
    AI rejected at least REJECTED_SHARE_BELOW of detections while losing at
    most MAX_CONFIRMED_LOST of the confirmed ones, picks the one that drops
    the most rejections, and of those the lowest: no higher than the
    evidence needs, so real detections just above it are not cut off.
    """
    rows = [(c, s) for c, s in judged if s in ('confirmed', 'rejected') and math.isfinite(c)]
    if len(rows) < MIN_JUDGED:
        return None
    confirmed_total = sum(1 for _c, s in rows if s == 'confirmed')
    best: dict[str, Any] | None = None
    for floor in CANDIDATE_FLOORS:
        below = [s for c, s in rows if c < floor]
        if len(below) < MIN_BELOW:
            continue
        rejected = below.count('rejected')
        confirmed_lost = below.count('confirmed')
        if rejected / len(below) < REJECTED_SHARE_BELOW:
            continue
        if confirmed_total and confirmed_lost / confirmed_total > MAX_CONFIRMED_LOST:
            continue
        if best is not None and rejected <= best['rejected_below']:
            continue
        best = {
            'min_confidence': floor,
            'rejected_below': rejected,
            'judged_below': len(below),
            'confirmed_lost': confirmed_lost,
            'confirmed_total': confirmed_total,
            'judged': len(rows),
        }
    return best


def tuning_suggestions(days: int = TUNING_WINDOW_DAYS) -> list[dict[str, Any]]:
    """Raise-the-floor suggestions per camera and label (see the module docstring)."""
    since = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).isoformat()
    rows = _state.database.ai_judged_detections(since)
    grouped: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
    zones: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        key = (str(row['camera_id'] or ''), str(row['label'] or '').strip().lower())
        if not key[0] or not key[1]:
            continue
        grouped[key].append((float(row['confidence']), str(row['ai_status'])))
        if row.get('zone_name'):
            zones[key].add(str(row['zone_name']))
    cameras = _cameras()
    suggestions = []
    for (camera_id, label), judged in sorted(grouped.items()):
        found = suggest_floor(judged)
        if not found:
            continue
        camera = next((c for c in cameras if str(c.get('id') or '') == camera_id), None)
        current = current_min_confidence(camera, label)
        # Only a raise is a suggestion, and only for a rule that exists.
        if current is None or found['min_confidence'] <= current + 0.01:
            continue
        suggestions.append({
            'camera_id': camera_id,
            'camera_name': _camera_name(cameras, camera_id),
            'label': label,
            'zones': sorted(zones[(camera_id, label)]),
            'current_min_confidence': round(current, 2),
            **found,
        })
    suggestions.sort(key=lambda s: -s['rejected_below'])
    return suggestions


def frequent_tags(days: int = TAGS_WINDOW_DAYS) -> list[dict[str, Any]]:
    """AI tags named often on a camera that no detector class or AI tag alert covers."""
    from app.ai_tag_alerts import zone_rules
    from app.label_vocabulary import canonical_label

    since = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).isoformat()
    cameras = _cameras()
    watched: dict[str, set[str]] = {}
    for camera in cameras:
        words = {str(term).strip().lower() for _zone, rule in zone_rules(camera) for term in rule.get('tags') or []}
        watched[str(camera.get('id') or '')] = words
    per_camera: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _state.database.ai_tag_counts(since):
        camera_id, tag, count = str(row['camera_id'] or ''), str(row['tag'] or '').strip().lower(), int(row['count'])
        if not camera_id or not tag or count < MIN_TAG_COUNT:
            continue
        if canonical_label(tag) is not None or tag in watched.get(camera_id, set()):
            continue
        if len(per_camera[camera_id]) < MAX_TAGS_PER_CAMERA:
            per_camera[camera_id].append({'tag': tag, 'count': count})
    return [
        {'camera_id': camera_id, 'camera_name': _camera_name(cameras, camera_id), 'tags': tags}
        for camera_id, tags in sorted(per_camera.items())
    ]


def insights() -> dict[str, Any]:
    return {
        'tuning': tuning_suggestions(),
        'frequent_tags': frequent_tags(),
        'tuning_window_days': TUNING_WINDOW_DAYS,
        'tags_window_days': TAGS_WINDOW_DAYS,
    }


def apply_min_confidence(camera_id: str, label: str, min_confidence: float, apply_cameras_settings) -> int:
    """Raise every enabled ``label`` rule on the camera below ``min_confidence``
    to it, through the same validate-save-apply path as the Zones page.
    Returns how many rules changed (never lowers a rule)."""
    from app.api.cameras_router import _persist_and_apply_cameras
    from app.payload_validators import validate_cameras_settings

    label = str(label or '').strip().lower()
    if not label:
        raise HTTPException(status_code=400, detail='label is required.')
    if isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)) \
            or not 0.05 <= float(min_confidence) <= 0.95:
        raise HTTPException(status_code=400, detail='min_confidence must be between 0.05 and 0.95.')
    cameras = _cameras()  # a fresh copy of the settings (cached_snapshot)
    camera = next((c for c in cameras if str(c.get('id') or '') == str(camera_id)), None)
    if camera is None:
        raise HTTPException(status_code=404, detail='Camera not found.')
    changed = 0
    for zone in (camera.get('detection') or {}).get('zones') or []:
        for rule in zone.get('object_rules') or []:
            if str(rule.get('label') or '').strip().lower() != label:
                continue
            current = rule.get('min_confidence')
            if isinstance(current, (int, float)) and current >= float(min_confidence):
                continue
            rule['min_confidence'] = round(float(min_confidence), 2)
            changed += 1
    if not changed:
        return 0
    settings = validate_cameras_settings({'cameras': cameras})
    _persist_and_apply_cameras(settings, _state.database, apply_cameras_settings)
    return changed
