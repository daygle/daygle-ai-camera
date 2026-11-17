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

import copy
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


# Ignore-area suggestions: the frame is split into GRID x GRID cells; a cell
# where the AI rejected one label at least MIN_REJECTED_IN_AREA times and
# confirmed it at most MAX_CONFIRMED_IN_AREA times (or 10% of the
# rejections) is suggested as an area to ignore that label in.
GRID = 5
MIN_REJECTED_IN_AREA = 6
MAX_CONFIRMED_IN_AREA = 1
AREA_PADDING = 0.03
MIN_AREA_SIDE = 0.06
MAX_AREAS_PER_LABEL = 3


def _center(row: dict[str, Any]) -> tuple[float, float] | None:
    try:
        x, y, w, h = (float(row[key]) for key in ('x', 'y', 'width', 'height'))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        return None
    return x + w / 2, y + h / 2


def _inside(point: tuple[float, float], area: dict[str, float]) -> bool:
    return (area['x'] <= point[0] <= area['x'] + area['width']
            and area['y'] <= point[1] <= area['y'] + area['height'])


def _zone_contains(zone: dict[str, Any], point: tuple[float, float]) -> bool:
    from app.zone_detection import point_in_polygon

    points = zone.get('points') or []
    if isinstance(points, list) and len(points) >= 3:
        return point_in_polygon(point[0], point[1], points)
    return _inside(point, {key: float(zone.get(key) or 0) for key in ('x', 'y', 'width', 'height')})


def suggest_areas(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Areas to ignore one label in, from that label's judged detections on
    one camera (``rows`` carry the box, ``ai_status`` and ``event_id``)."""
    cells: dict[tuple[int, int], list[tuple[tuple[float, float], dict[str, Any]]]] = defaultdict(list)
    confirmed: list[tuple[float, float]] = []
    for row in rows:
        center = _center(row)
        if center is None:
            continue
        if row.get('ai_status') == 'confirmed':
            confirmed.append(center)
        elif row.get('ai_status') == 'rejected':
            cell = (min(GRID - 1, int(center[0] * GRID)), min(GRID - 1, int(center[1] * GRID)))
            cells[cell].append((center, row))
    found = []
    for members in cells.values():
        if len(members) < MIN_REJECTED_IN_AREA:
            continue
        xs = [c[0] for c, _row in members]
        ys = [c[1] for c, _row in members]
        x0, x1 = max(0.0, min(xs) - AREA_PADDING), min(1.0, max(xs) + AREA_PADDING)
        y0, y1 = max(0.0, min(ys) - AREA_PADDING), min(1.0, max(ys) + AREA_PADDING)
        if x1 - x0 < MIN_AREA_SIDE:
            mid = (x0 + x1) / 2
            x0, x1 = max(0.0, mid - MIN_AREA_SIDE / 2), min(1.0, mid + MIN_AREA_SIDE / 2)
        if y1 - y0 < MIN_AREA_SIDE:
            mid = (y0 + y1) / 2
            y0, y1 = max(0.0, mid - MIN_AREA_SIDE / 2), min(1.0, mid + MIN_AREA_SIDE / 2)
        area = {'x': round(x0, 4), 'y': round(y0, 4), 'width': round(x1 - x0, 4), 'height': round(y1 - y0, 4)}
        rejected_in = sum(1 for center in (c for c, _row in members) if _inside(center, area))
        confirmed_in = sum(1 for center in confirmed if _inside(center, area))
        if confirmed_in > max(MAX_CONFIRMED_IN_AREA, int(rejected_in * 0.1)):
            continue
        latest = max((row for _c, row in members), key=lambda row: str(row.get('created_at') or ''))
        found.append({'area': area, 'rejected': rejected_in, 'confirmed': confirmed_in,
                      'sample_event_id': latest.get('event_id')})
    found.sort(key=lambda item: -item['rejected'])
    return found[:MAX_AREAS_PER_LABEL]


def _ignore_areas(camera: dict[str, Any]) -> list[tuple[dict[str, Any], int, dict[str, Any]]]:
    return [
        (zone, index, area)
        for zone in ((camera or {}).get('detection') or {}).get('zones') or []
        for index, area in enumerate(zone.get('ignore_areas') or [])
    ]


def ignore_area_suggestions(days: int = TUNING_WINDOW_DAYS) -> list[dict[str, Any]]:
    """Per camera and label, areas where the AI keeps rejecting that label
    and the camera has a zone (with a rule for it) to ignore it in."""
    since = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).isoformat()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in _state.database.ai_judged_detections(since):
        key = (str(row.get('camera_id') or ''), str(row.get('label') or '').strip().lower())
        if key[0] and key[1]:
            grouped[key].append(row)
    cameras = _cameras()
    suggestions = []
    for (camera_id, label), rows in sorted(grouped.items()):
        camera = next((c for c in cameras if str(c.get('id') or '') == camera_id), None)
        if camera is None:
            continue
        existing = [area for _zone, _index, area in _ignore_areas(camera) if area.get('label') == label]
        for found in suggest_areas(rows):
            area = found['area']
            center = (area['x'] + area['width'] / 2, area['y'] + area['height'] / 2)
            if any(_inside(center, other) for other in existing):
                continue
            zones = [
                zone for zone in (camera.get('detection') or {}).get('zones') or []
                if zone.get('enabled', True) and _zone_contains(zone, center)
                and any(str(rule.get('label') or '').strip().lower() == label for rule in zone.get('object_rules') or [])
            ]
            if not zones:
                continue
            suggestions.append({
                'camera_id': camera_id, 'camera_name': _camera_name(cameras, camera_id), 'label': label,
                'zones': [str(zone.get('name') or zone.get('id')) for zone in zones], **found,
            })
    suggestions.sort(key=lambda item: -item['rejected'])
    return suggestions


def ignored_areas() -> list[dict[str, Any]]:
    """Every ignore area in force, for listing (and removing) on the AI page."""
    return [
        {'camera_id': str(camera.get('id') or ''), 'camera_name': str(camera.get('name') or camera.get('id') or ''),
         'zone_id': str(zone.get('id') or ''), 'zone_name': str(zone.get('name') or zone.get('id') or ''),
         'index': index, **area}
        for camera in _cameras()
        for zone, index, area in _ignore_areas(camera)
    ]


def insights() -> dict[str, Any]:
    return {
        'tuning': tuning_suggestions(),
        'ignore_areas': ignore_area_suggestions(),
        'ignored_areas': ignored_areas(),
        'frequent_tags': frequent_tags(),
        'tuning_window_days': TUNING_WINDOW_DAYS,
        'tags_window_days': TAGS_WINDOW_DAYS,
    }


def _update_camera(camera_id: str, mutate, apply_cameras_settings) -> Any:
    """Change one camera's settings the way the Zones page's save does:
    under the camera-settings write lock, on a fresh copy of the settings
    (so a concurrent day/night profile switch is not overwritten), with
    only that camera validated. ``mutate(camera)`` edits the camera in
    place and returns a result; nothing is saved when it returns a falsy
    value. Raises 404 for an unknown camera."""
    from app.auth import utc_now
    from app.profile_presets import link_camera_profiles
    from app.payload_validators import validate_camera_settings

    database = _state.database
    with _state._cameras_config_write_lock:
        cameras = _cameras()
        index = next((i for i, c in enumerate(cameras) if str(c.get('id') or '') == str(camera_id)), None)
        if index is None:
            raise HTTPException(status_code=404, detail='Camera not found.')
        current = cameras[index]
        edited = copy.deepcopy(current)
        result = mutate(edited)
        if not result:
            return result
        cameras[index] = validate_camera_settings(edited, current=current, index=index + 1)
        cameras, _changed = link_camera_profiles(cameras, database.get_setting('camera_profile_presets'))
        database.set_setting('cameras', cameras, utc_now())
        if apply_cameras_settings is not None:
            apply_cameras_settings(cameras)
        return result


def _area_payload(area: Any) -> dict[str, float]:
    if not isinstance(area, dict):
        raise HTTPException(status_code=400, detail='area must be an object with x, y, width and height.')
    try:
        values = {key: float(area[key]) for key in ('x', 'y', 'width', 'height')}
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='area must have numeric x, y, width and height.') from exc
    if not all(math.isfinite(v) for v in values.values()) or values['width'] <= 0 or values['height'] <= 0 \
            or not (0 <= values['x'] <= 1 and 0 <= values['y'] <= 1):
        raise HTTPException(status_code=400, detail='area must lie within the frame (0 to 1).')
    return values


def apply_ignore_area(camera_id: str, label: str, area: Any, apply_cameras_settings) -> int:
    """Ignore ``label`` in ``area`` in every enabled zone of the camera that
    contains the area's centre and has a rule for that label. Returns how
    many zones changed."""
    label = str(label or '').strip().lower()
    if not label:
        raise HTTPException(status_code=400, detail='label is required.')
    values = _area_payload(area)
    center = (values['x'] + values['width'] / 2, values['y'] + values['height'] / 2)

    def mutate(camera: dict) -> int:
        changed = 0
        for zone in (camera.get('detection') or {}).get('zones') or []:
            if not zone.get('enabled', True) or not _zone_contains(zone, center):
                continue
            if not any(str(rule.get('label') or '').strip().lower() == label
                       for rule in zone.get('object_rules') or []):
                continue
            zone.setdefault('ignore_areas', []).append({'label': label, **values, 'source': 'ai'})
            changed += 1
        return changed

    return _update_camera(camera_id, mutate, apply_cameras_settings) or 0


def remove_ignore_area(camera_id: str, zone_id: str, index: Any, apply_cameras_settings) -> bool:
    """Stop ignoring an area: remove ignore area ``index`` from the zone."""
    if isinstance(index, bool) or not isinstance(index, int):
        raise HTTPException(status_code=400, detail='index must be an integer.')

    def mutate(camera: dict) -> bool:
        zone = next((z for z in (camera.get('detection') or {}).get('zones') or []
                     if str(z.get('id') or '') == str(zone_id)), None)
        areas = (zone or {}).get('ignore_areas') or []
        if not 0 <= index < len(areas):
            raise HTTPException(status_code=404, detail='Ignore area not found.')
        del areas[index]
        zone['ignore_areas'] = areas
        return True

    return bool(_update_camera(camera_id, mutate, apply_cameras_settings))


def apply_min_confidence(camera_id: str, label: str, min_confidence: float, apply_cameras_settings) -> int:
    """Raise every enabled ``label`` rule on the camera below ``min_confidence``
    to it, through the same validate-save-apply path as the Zones page.
    Returns how many rules changed (never lowers a rule)."""
    label = str(label or '').strip().lower()
    if not label:
        raise HTTPException(status_code=400, detail='label is required.')
    if isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)) \
            or not 0.05 <= float(min_confidence) <= 0.95:
        raise HTTPException(status_code=400, detail='min_confidence must be between 0.05 and 0.95.')
    def mutate(camera: dict) -> int:
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
        return changed

    return _update_camera(camera_id, mutate, apply_cameras_settings) or 0
