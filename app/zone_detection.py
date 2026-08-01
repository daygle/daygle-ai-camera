"""Zone detection orchestration extracted from ``app/main.py`` (Phase-23).

The 21 helpers shipped here cluster around the **zone detection pipeline
that runs inside the live-stream alert monitor**: per-detection geometry
(point-in-polygon + rectangle overlap), per-zone object/motion rule
matching, label aliasing through the per-zone ``object_labels`` allow-lists,
recording-rule decisions, and the live-detection box normalization that
threads detector output through the per-camera settings dict.

This is the largest remaining pure cluster in ``app/main.py`` after the
prior 7 hybrid phases. Two of its 21 helpers (``get_camera_instance``,
``_zone_pixel_motion_fraction``) cross the cluster boundary with state
or numpy on main.py; the other 19 reach siblings only.

Like the prior extractions (``app/auth_gates.py`` Phase-16,
``app/config_facades.py`` Phase-17, ``app/camera_config.py`` Phase-18,
``app/recording_settings.py`` Phase-19, ``app/ai_settings.py`` Phase-20,
``app/zone_schema.py`` Phase-21, ``app/payload_validators.py`` Phase-22),
these are extracted using the **hybrid-pattern template**:

- Cluster functions reach ``main.<attr>`` at *call time* (NOT import
  time) for their cross-module dependencies, so they continue to work
  seamlessly when ``app/main.py`` is partially loaded during the
  Pool A rebind loop.
- The Pool A from-import rebinds live at the TOP of ``app/main.py``
  (in the existing rebind section) so that the eager-evaluation
  order at module load has ``main.<name>`` wired correctly before
  any sibling body references it as a bare name.
- Default-value expressions (``gate_fraction`` / ``scale_fraction``
  in ``zone_motion_detections``) bind to ``main._MOTION_GATE_FRACTION``
  / ``main._MOTION_SCALE_FRACTION`` at module-load time of this
  module. Both constants are defined on ``app.main`` BEFORE the rebind
  block executes, so they are populated when the function is defined.

Cluster membership (21 helpers, 290 lines original):

- ``get_camera_instance`` -- resolves ``camera_id`` -> live OpenCvStreamCamera
  instance via ``main.get_camera_config`` (Phase-17 route) and
  ``main.camera_instances`` (main module state, mutated by ``apply_cameras_settings``).
  Raises ``fastapi.HTTPException(404)`` when the instance is missing so
  router handlers can return a clean 404 instead of a stack trace.

- ``point_in_polygon`` -- bounding-ray even-odd test for arbitrary
  polygon ``zone['points']``. Tolerates non-numeric point dicts by
  skipping them (continues the loop from the next iteration).

- ``point_on_segment`` -- exact cross-product segment membership with
  a 1e-9 float tolerance so polygon edges are closed.

- ``detection_center_in_zone`` -- dispatches to ``point_in_polygon``
  when the zone has 3+ points, otherwise falls back to the rectangle
  containment test using ``zone['x']/['width']`` / ``zone['y']/['height']``.

- ``detection_overlap_ratio_with_zone_rect`` -- rectangle intersection
  ratio (intersection / detection_box_area) so a partially-occluded
  detection can still match a rectangle zone via overlap, not center-only.

- ``detection_matches_zone`` -- combines center-in + rectangle overlap
  into a single ``min_overlap_ratio`` decision; polygon zones only
  match by center (the partial-overlap heuristic makes no sense for
  polygons).

- ``_zone_pixel_motion_fraction`` -- numpy-based per-zone motion
  pixel fraction using the boolean ``diff_mask`` from
  ``detect_frame_motion`` at ``main._MOTION_FRAME_H × main._MOTION_FRAME_W``
  resolution. Reads ``main._MOTION_FRAME_W`` and
  ``main._MOTION_FRAME_H``. Falls back to bounds derived from
  ``zone['points']`` when the rectangle fields are missing so
  polygon-only zones can still be measured.

- ``zone_motion_detections`` -- per-camera motion-to-detection
  converter. Reads each zone's motion rule (``zone_motion_min_confidence``
  from Phase-21) and emits a pseudo-detection with the zone footprint
  when motion clears the gate. Default values ``gate_fraction`` /
  ``scale_fraction`` bind to ``main._MOTION_GATE_FRACTION`` /
  ``main._MOTION_SCALE_FRACTION`` at module-load time.

- ``filter_detections_for_camera_zones`` -- the per-zone filter
  combining ``detection_matches_zone`` + ``detection_label_allowed_for_zone``.
  When no zones match the monitor key, falls back to the camera-level
  ``object_labels`` allow-list (so a camera with NO zones still filters
  by its labels) -- unless ``require_zones=True`` was requested.

- ``filter_detections_for_camera`` -- thin wrapper that calls
  ``filter_detections_for_camera_zones`` with ``zone_monitor_key='monitor_objects'``.

- ``detection_label_allowed_for_zone`` -- checks ``zone['object_labels']``
  (call ``normalize_label_list`` from Phase-21 + ``_LABEL_ALIASES``)
  against the detection label, falling back to the full camera label
  set when the zone has no allow-list.

- ``zone_object_rule_matches`` -- returns ``[(zone, rule)]`` pairs
  matching the detection label + confidence at or above the rule's
  ``min_confidence``, filtered by ``action`` ('alert' = email/push on,
  'record' = record_on_detect on). Uses ``_LABEL_ALIASES`` for label
  normalization so 'human' aliases to 'person'.

- ``zone_object_alert_rules`` -- builds the per-zone AlertEngine rule
  list with ``cooldown_key``, contact fields, and 4 notification-window
  strings. Routes ``email_recipients`` through
  ``main.normalize_email_recipients`` and ``email_enabled`` /
  ``push_enabled`` through ``main.normalize_bool_setting``.

- ``zone_rule_name`` -- the human-readable rule name
  ``<camera> / <zone> / <label>`` used by AlertEngine and
  /api/alerts listing.

- ``zone_alert_detections`` -- applies ``zone_object_rule_matches``
  with action='alert' to filter detections down to those that match
  at least one alert rule; attaches ``zone_id`` / ``zone_name`` for
  the timeline / recordings overlay.

- ``zone_name_for_detection`` -- the FIRST matching zone for either
  action='alert' or action='record'; used by the recordings overlay
  when stamping the zone hint onto a detection.

- ``zone_record_on_detect`` -- ``bool(zone_object_rule_matches(... 'record'))``
  so a single record rule fires the recorder; back-compat with the
  flat ``record_objects`` flag removed in Phase-9.

- ``zone_motion_record_on_detect`` -- the motion-axis counterpart:
  checks the ``monitor_motion=True`` zone(s) for a motion rule with
  ``record_on_detect=True``. With the optional ``zone_id`` only that
  zone is considered (per-zone recording: motion in a record-off zone
  cannot piggyback on a record-on rule elsewhere); without it any
  matching zone counts (legacy callers/tests). Intentionally separate
  from ``zone_record_on_detect`` which filters on
  ``monitor_objects=True``.

- ``zone_detection_alert_rule_names`` -- ``{zone_rule_name(...)}`` set
  of matched rule names so the renderer / status payload can show
  which rules fired for each detection.

- ``detection_has_matching_record_rule`` -- similar to
  ``zone_object_rule_matches`` but reads rules from a precomputed flat
  AlertEngine rule list (received from ``zone_object_alert_rules``).
  Ignores cooldown so a recording fires on every matching detection,
  not only when a notification is emitted.

- ``normalize_detection_boxes_for_frame`` -- converts pixel-coord
  boxes to normalised [0,1] coords using ``frame['width'] /
  frame['height']``. Returns the input list unchanged when the
  frame dimensions are missing or the box already looks normalised
  (``max <= 1``).

Pool C reach sites (resolved via ``main.<attr>`` at call time):

- ``main.get_camera_config`` (``get_camera_instance`` -- Phase-17 rebind)
- ``main.camera_instances`` (``get_camera_instance`` -- module-level
  dict state, mutated in place by ``apply_cameras_settings``; main.py
  retains ownership as the source of truth so the recorder + admin
  handlers and the helper all read/write the same dict)
- ``main.HTTPException`` (``get_camera_instance`` -- re-exported from
  ``fastapi`` at the top of main.py)
- ``main._MOTION_FRAME_W`` / ``main._MOTION_FRAME_H``
  (``_zone_pixel_motion_fraction`` -- read at call time inside the
  function body, not as a default, so Pool C call-time resolution is
  sufficient)
- ``main._MOTION_GATE_FRACTION`` / ``main._MOTION_SCALE_FRACTION``
  (``zone_motion_detections`` -- bound to default-arg expressions at
  module-load time of this module; both constants are populated on
  ``app.main`` before this rebind block fires)
- ``main.normalize_label_list`` (Phase-21 rebind, called 2x)
- ``main._LABEL_ALIASES`` (Phase-21 dict rebind, used 3x)
- ``main.zone_motion_min_confidence`` (Phase-21 rebind, called 1x)
- ``main.normalize_email_recipients`` (still on main.py, called 1x)
- ``main.normalize_bool_setting`` (still on main.py, called 2x)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
import numpy as np

import app.state as _state

logger = logging.getLogger('daygle.ai')
from app.config_facades import get_camera_config

# Tracks zones that have already logged an unexpected pixel-motion error so we
# don't flood logs on every frame. Cleared on success to allow self-healing.
_zone_pixel_motion_errors: set[str] = set()
from app.utils import normalize_email_recipients
from app.zone_schema import _LABEL_ALIASES, normalize_label_list, zone_motion_min_confidence


def get_camera_instance(camera_id: str | None = None):
    configured = get_camera_config(camera_id)
    instance = _state.camera_instances.get(str(configured['id']))
    if instance is None:
        raise HTTPException(status_code=404, detail='Camera not found')
    return instance


def point_in_polygon(x: float, y: float, points: list[dict[str, Any]]) -> bool:
    if len(points) < 3:
        return False
    inside = False
    previous = points[-1]
    for current in points:
        try:
            current_x = float(current.get('x') or 0)
            current_y = float(current.get('y') or 0)
            previous_x = float(previous.get('x') or 0)
            previous_y = float(previous.get('y') or 0)
        except (TypeError, ValueError):
            previous = current
            continue
        if point_on_segment(x, y, previous_x, previous_y, current_x, current_y):
            return True
        intersects = (current_y > y) != (previous_y > y)
        if intersects:
            slope_x = (previous_x - current_x) * (y - current_y) / (previous_y - current_y or 1e-12) + current_x
            if x < slope_x:
                inside = not inside
        previous = current
    return inside


def point_on_segment(x: float, y: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    cross = (y - y1) * (x2 - x1) - (x - x1) * (y2 - y1)
    if abs(cross) > 1e-09:
        return False
    return min(x1, x2) - 1e-09 <= x <= max(x1, x2) + 1e-09 and min(y1, y2) - 1e-09 <= y <= max(y1, y2) + 1e-09


def detection_center_in_zone(detection: dict[str, Any], zone: dict[str, Any]) -> bool:
    box = detection.get('box') or {}
    center_x = float(box.get('x') or 0) + float(box.get('width') or 0) / 2
    center_y = float(box.get('y') or 0) + float(box.get('height') or 0) / 2
    points = zone.get('points') or []
    if isinstance(points, list) and len(points) >= 3:
        return point_in_polygon(center_x, center_y, points)
    return float(zone.get('x') or 0) <= center_x <= float(zone.get('x') or 0) + float(zone.get('width') or 0) and float(zone.get('y') or 0) <= center_y <= float(zone.get('y') or 0) + float(zone.get('height') or 0)


def detection_overlap_ratio_with_zone_rect(detection: dict[str, Any], zone: dict[str, Any]) -> float:
    box = detection.get('box') or {}
    x = float(box.get('x') or 0)
    y = float(box.get('y') or 0)
    width = max(0.0, float(box.get('width') or 0))
    height = max(0.0, float(box.get('height') or 0))
    if width <= 0 or height <= 0:
        return 0.0
    dx1 = x
    dy1 = y
    dx2 = x + width
    dy2 = y + height
    zx1 = float(zone.get('x') or 0)
    zy1 = float(zone.get('y') or 0)
    zw = max(0.0, float(zone.get('width') or 0))
    zh = max(0.0, float(zone.get('height') or 0))
    zx2 = zx1 + zw
    zy2 = zy1 + zh
    ix1 = max(dx1, zx1)
    iy1 = max(dy1, zy1)
    ix2 = min(dx2, zx2)
    iy2 = min(dy2, zy2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    detection_area = width * height
    return intersection / detection_area if detection_area > 0 else 0.0


def detection_matches_zone(detection: dict[str, Any], zone: dict[str, Any], *, min_overlap_ratio: float = 0.2) -> bool:
    if detection_center_in_zone(detection, zone):
        return True
    points = zone.get('points') or []
    if isinstance(points, list) and len(points) >= 3:
        return False
    return detection_overlap_ratio_with_zone_rect(detection, zone) >= min_overlap_ratio


def _zone_pixel_motion_fraction(diff_mask: Any, zone: dict[str, Any]) -> float:
    """Return the fraction of pixels inside a zone's bounding box that changed.

    ``diff_mask`` is the boolean (H×W) array from ``detect_frame_motion`` at
    ``main._MOTION_FRAME_H × main._MOTION_FRAME_W`` resolution.  Zone coordinates are
    normalised (0-1) and are converted to pixel indices before slicing.
    """
    zone_id = str(zone.get('id') or zone.get('name') or id(zone))
    try:
        x = zone.get('x')
        y = zone.get('y')
        w = zone.get('width')
        h = zone.get('height')
        points = zone.get('points') or []
        if (x is None or w is None) and isinstance(points, list) and (len(points) >= 2):
            xs = [float(p.get('x', 0)) for p in points if isinstance(p, dict)]
            ys = [float(p.get('y', 0)) for p in points if isinstance(p, dict)]
            if xs and ys:
                x = x if x is not None else min(xs)
                y = y if y is not None else min(ys)
                w = w if w is not None else max(xs) - float(x)
                h = h if h is not None else max(ys) - float(y)
        x = float(x if x is not None else 0)
        y = float(y if y is not None else 0)
        w = float(w if w is not None else 1)
        h = float(h if h is not None else 1)
        px1 = max(0, int(x * _state._MOTION_FRAME_W))
        py1 = max(0, int(y * _state._MOTION_FRAME_H))
        px2 = min(_state._MOTION_FRAME_W, max(px1 + 1, int(round((x + w) * _state._MOTION_FRAME_W))))
        py2 = min(_state._MOTION_FRAME_H, max(py1 + 1, int(round((y + h) * _state._MOTION_FRAME_H))))
        result = float(np.mean(diff_mask[py1:py2, px1:px2]))
        _zone_pixel_motion_errors.discard(zone_id)
        return result
    except (TypeError, ValueError, IndexError, AttributeError) as exc:
        # Expected malformed zone/mask inputs; fail open with zero motion.
        logger.debug('Expected error computing pixel motion for zone %r: %s', zone_id, exc)
        return 0.0
    except Exception as exc:
        if zone_id not in _zone_pixel_motion_errors:
            logger.warning('Unexpected error computing pixel motion for zone %r: %s', zone_id, exc)
            _zone_pixel_motion_errors.add(zone_id)
        return 0.0


def zone_motion_detections(
    settings: dict[str, Any],
    frame_motion_confidence: float = 0.5,
    *,
    diff_mask: Any = None,
    gate_fraction: float | None = None,
    scale_fraction: float | None = None,
) -> list[dict[str, Any]]:
    if gate_fraction is None:
        gate_fraction = _state._MOTION_GATE_FRACTION
    if scale_fraction is None:
        scale_fraction = _state._MOTION_SCALE_FRACTION
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get('monitor_motion', True)]
    if not zones:
        return []
    seen_zones: set[str] = set()
    result: list[dict[str, Any]] = []
    for zone in zones:
        zone_id = str(zone.get('id') or zone.get('name') or id(zone))
        if zone_id in seen_zones:
            continue
        zone_fraction = -1.0
        if diff_mask is not None:
            zone_fraction = _zone_pixel_motion_fraction(diff_mask, zone)
            if zone_fraction < gate_fraction:
                continue
            zone_confidence = round(min(1.0, zone_fraction / max(scale_fraction, 1e-09)), 3)
        else:
            zone_confidence = frame_motion_confidence
        conf_threshold = zone_motion_min_confidence(zone)
        if zone_confidence < conf_threshold:
            logger.debug(
                'Motion zone %r: zone_fraction=%.4f zone_confidence=%.3f below conf_threshold=%.3f (scale_fraction=%.4f)',
                zone_id, zone_fraction, zone_confidence, conf_threshold, scale_fraction,
            )
            continue
        seen_zones.add(zone_id)
        result.append({
            'confidence': zone_confidence,
            'zone_id': zone_id,
            'zone_name': zone.get('name') or zone_id,
            'box': {
                'x': float(zone.get('x', 0)),
                'y': float(zone.get('y', 0)),
                'width': float(zone.get('width', 1)),
                'height': float(zone.get('height', 1)),
            },
        })
    return result


def detection_label_allowed_for_zone(detection: dict[str, Any], zone: dict[str, Any], camera_labels: set[str]) -> bool:
    zone_labels = set(normalize_label_list(zone.get('object_labels', [])))
    allowed_labels = zone_labels or camera_labels
    if not allowed_labels:
        return True
    label = str(detection.get('label') or '').strip().lower()
    return _LABEL_ALIASES.get(label, label) in allowed_labels


def filter_detections_for_camera_zones(
    detections: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    zone_monitor_key: str,
    require_zones: bool = False,
) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get(zone_monitor_key, True)]
    camera_labels = set(normalize_label_list(detection_settings.get('object_labels', [])))
    if not zones:
        if zone_monitor_key == 'monitor_objects' and camera_labels and (not require_zones):
            return [detection for detection in detections if str(detection.get('label') or '').strip().lower() in camera_labels]
        # No zones and no camera labels: keep legacy "accept all" behavior so a
        # camera with object detection enabled but unconfigured still records
        # and alerts. Log the fallback once per call to aid debugging.
        if not camera_labels and not require_zones:
            logger.debug('filter_detections_for_camera_zones: no zones or camera labels configured; returning %d detections unfiltered', len(detections))
        return [] if require_zones else detections
    return [
        detection
        for detection in detections
        if any(
            detection_matches_zone(detection, zone)
            and (zone_monitor_key != 'monitor_objects' or detection_label_allowed_for_zone(detection, zone, camera_labels))
            for zone in zones
        )
    ]


def filter_detections_for_camera(detections: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    if not detection_settings.get('object_detection_enabled', True):
        return []
    return filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_objects')


def zone_object_rule_matches(settings: dict[str, Any], detection: dict[str, Any], *, action: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if action not in ('alert', 'record'):
        raise ValueError(f"action must be 'alert' or 'record', got {action!r}")
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get('monitor_objects', True)]
    label = str(detection.get('label') or '').strip().lower()
    label = _LABEL_ALIASES.get(label, label)
    if not label:
        return []
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for zone in zones:
        if not detection_matches_zone(detection, zone):
            continue
        for rule in zone.get('object_rules') or []:
            if not rule.get('enabled', True):
                continue
            if action == 'alert' and (not (rule.get('email_enabled') or rule.get('push_enabled'))):
                continue
            if action == 'record' and (not rule.get('record_on_detect', True)):
                continue
            # Canonicalise the rule label through the same alias map as the
            # detection label, so a rule configured as ``human``/``people``/
            # ``pedestrian`` matches a ``person`` detection. The recording
            # path (``detection_has_matching_record_rule``) and the
            # AlertEngine both alias both sides; without this the alert
            # pre-filter silently dropped aliased rules.
            rule_label = str(rule.get('label') or '').strip().lower()
            if _LABEL_ALIASES.get(rule_label, rule_label) != label:
                continue
            if float(detection.get('confidence') or 0) < float(rule.get('min_confidence', 0.5)):
                continue
            matches.append((zone, rule))
    return matches


def zone_object_alert_rules(settings: dict[str, Any]) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get('monitor_objects', True)]
    rules: list[dict[str, Any]] = []
    camera_key = str(settings.get('id') or settings.get('name') or 'camera').strip() or 'camera'
    for zone in zones:
        zone_id = str(zone.get('id') or zone.get('name') or 'zone')
        for rule in zone.get('object_rules') or []:
            if not rule.get('enabled', True) or not (rule.get('email_enabled') or rule.get('push_enabled')):
                continue
            label = str(rule.get('label') or '').strip().lower()
            if not label:
                continue
            rules.append({
                'name': zone_rule_name(settings, zone, rule),
                'cooldown_key': f'{camera_key}::{zone_id}::{label}',
                'object': label,
                'zone_id': zone_id,
                'min_confidence': rule.get('min_confidence', 0.5),
                'cooldown_seconds': rule.get('cooldown_seconds', 60),
                'enabled': True,
                'email_enabled': bool(rule.get('email_enabled', False)),
                'email_recipients': normalize_email_recipients(rule.get('email_recipients', [])),
                'push_enabled': bool(rule.get('push_enabled', False)),
                'active_start': rule.get('active_start'),
                'active_end': rule.get('active_end'),
                'notify_start': rule.get('notify_start'),
                'notify_end': rule.get('notify_end'),
            })
    return rules


def zone_rule_name(settings: dict[str, Any], zone: dict[str, Any], rule: dict[str, Any]) -> str:
    camera_name = str(settings.get('name') or settings.get('id') or 'Camera')
    zone_name = str(zone.get('name') or zone.get('id') or 'Zone')
    label = str(rule.get('label') or '').strip().lower()
    return f'{camera_name} / {zone_name} / {label}'


def zone_alert_detections(settings: dict[str, Any], detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for index, detection in enumerate(detections):
        for zone, _rule in zone_object_rule_matches(settings, detection, action='alert'):
            zone_id = str(zone.get('id') or zone.get('name') or 'zone')
            key = (index, zone_id)
            if key in seen:
                continue
            seen.add(key)
            matched.append({**detection, 'zone_id': zone_id, 'zone_name': zone.get('name') or zone_id})
    return matched


def zone_name_for_detection(settings: dict[str, Any], detection: dict[str, Any]) -> str | None:
    for action in ('alert', 'record'):
        matches = zone_object_rule_matches(settings, detection, action=action)
        if matches:
            zone = matches[0][0]
            zone_name = str(zone.get('name') or zone.get('id') or '').strip()
            return zone_name or None
    return None


def zone_record_on_detect(detection: dict[str, Any], settings: dict[str, Any]) -> bool:
    return bool(zone_object_rule_matches(settings, detection, action='record'))


def zone_motion_record_on_detect(settings: dict[str, Any], zone_id: str | None = None) -> bool:
    """Return True if the motion rule covering ``zone_id`` has record_on_detect=True.

    zone_record_on_detect / zone_object_rule_matches filter by monitor_objects=True and therefore
    skip motion-only zones (monitor_objects=False, monitor_motion=True). This helper checks the
    correct monitor_motion axis so motion-only zones are not silently excluded from recording.

    When ``zone_id`` is provided only that zone is considered, so the recording
    decision is per-zone: motion in a record-off zone cannot piggyback on a
    record-on rule in a different zone. Without ``zone_id`` (legacy callers,
    tests) any matching zone counts.
    """
    detection_settings = settings.get('detection') or {}
    for zone in detection_settings.get('zones', []):
        if not zone.get('enabled', True) or not zone.get('monitor_motion', True):
            continue
        zone_key = str(zone.get('id') or zone.get('name') or id(zone))
        if zone_id is not None and zone_key != str(zone_id):
            continue
        for rule in zone.get('object_rules') or []:
            if not rule.get('enabled', True):
                continue
            if str(rule.get('label') or '').strip().lower() == 'motion' and rule.get('record_on_detect', True):
                return True
    return False


def zone_detection_alert_rule_names(settings: dict[str, Any], detection: dict[str, Any]) -> set[str]:
    return {zone_rule_name(settings, zone, rule) for zone, rule in zone_object_rule_matches(settings, detection, action='alert')}


def detection_has_matching_record_rule(detection: dict[str, Any], rules: list[dict[str, Any]]) -> bool:
    """Return True if any enabled alert rule covers this detection by label and confidence.

    Cooldown and time-window are intentionally ignored so a recording is created on every
    matching detection, not only when a new alert notification is emitted.
    """
    label = str(detection.get('label') or '').strip().lower()
    label = _LABEL_ALIASES.get(label, label)
    if not label:
        return False
    confidence = float(detection.get('confidence') or 0)
    for rule in rules:
        if not rule.get('enabled', True):
            continue
        rule_object = str(rule.get('object') or '').strip().lower()
        rule_object = _LABEL_ALIASES.get(rule_object, rule_object)
        if rule_object != label:
            continue
        try:
            min_conf = float(rule.get('min_confidence', 0.0 if label == 'motion' else 0.5))
        except (TypeError, ValueError):
            min_conf = 0.0 if label == 'motion' else 0.5
        if confidence >= min_conf:
            return True
    return False


def normalize_detection_boxes_for_frame(detections: list[dict[str, Any]], frame: dict[str, Any]) -> list[dict[str, Any]]:
    width = float(frame.get('width') or 0)
    height = float(frame.get('height') or 0)
    if width <= 0 or height <= 0:
        return detections
    normalized: list[dict[str, Any]] = []
    for detection in detections:
        box = detection.get('box') or {}
        if not isinstance(box, dict):
            normalized.append(detection)
            continue
        box_x = float(box.get('x') or 0)
        box_y = float(box.get('y') or 0)
        box_width = float(box.get('width') or 0)
        box_height = float(box.get('height') or 0)
        if max(box_x, box_y, box_width, box_height) <= 1:
            normalized.append(detection)
            continue
        normalized.append({
            **detection,
            'box': {
                'x': round(box_x / width, 4),
                'y': round(box_y / height, 4),
                'width': round(box_width / width, 4),
                'height': round(box_height / height, 4),
            },
        })
    return normalized
