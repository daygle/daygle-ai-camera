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
  ``detect_frame_motion``. Geometry is taken from the mask's own shape so
  cameras may use different motion thumbnail sizes concurrently. Falls back to
  bounds derived from ``zone['points']`` when the rectangle fields are missing
  so polygon-only zones can still be measured.

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
- ``app.state._MOTION_FRAME_W`` / ``app.state._MOTION_FRAME_H`` remain only
  as legacy defaults for standalone motion callers. Live camera profiles pass a
  local size to ``detect_frame_motion``; zone scoring uses the returned mask
  shape and does not read those globals.
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
import math
from typing import Any

from fastapi import HTTPException
import numpy as np

import app.state as _state

logger = logging.getLogger('daygle.ai')
from app.config_facades import get_camera_config
from app.camera_policy import camera_policy

# Tracks zones that have already logged an unexpected pixel-motion error so we
# don't flood logs on every frame. Cleared on success to allow self-healing.
_zone_pixel_motion_errors: set[str] = set()
from app.utils import normalize_email_recipients
from app.zone_schema import (
    canonical_label,
    detection_label_in_allowed,
    label_matches,
    normalize_label_list,
    zone_motion_gate_fraction,
    zone_motion_max_confidence,
    zone_motion_min_confidence,
    zone_motion_scale_fraction,
)


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


def _detection_matches_compiled_zone(detection: dict[str, Any], compiled_zone: Any) -> bool:
    """Broad-phase bounds check followed by the exact polygon/rectangle test."""
    box = detection.get('box') or {}
    try:
        dx, dy = float(box.get('x') or 0), float(box.get('y') or 0)
        dw, dh = float(box.get('width') or 0), float(box.get('height') or 0)
        left, top, width, height = compiled_zone.bounds
        if dx + max(0.0, dw) < left or dx > left + width or dy + max(0.0, dh) < top or dy > top + height:
            return False
    except (TypeError, ValueError):
        pass
    return detection_matches_zone(detection, compiled_zone.zone)


def _detection_matches_zone_uncached(detection: dict[str, Any], zone: dict[str, Any], min_overlap_ratio: float) -> bool:
    if detection_center_in_zone(detection, zone):
        return True
    points = zone.get('points') or []
    if isinstance(points, list) and len(points) >= 3:
        return False
    return detection_overlap_ratio_with_zone_rect(detection, zone) >= min_overlap_ratio


def detection_matches_zone(detection: dict[str, Any], zone: dict[str, Any], *, min_overlap_ratio: float = 0.2) -> bool:
    """Whether a detection's box falls inside a zone (centre-in, or rect overlap).

    The same (detection, zone) pair is tested several times per cycle -- the
    camera/zone filter, alert-rule matching, the record-on-detect check, and the
    zone-name stamping for playback all ask independently. The verdict is a pure
    function of the detection box and the zone geometry, so it is memoised on the
    detection: the result is cached under a key of (zone id, box) so a ``{**det}``
    copy that keeps its box reuses the answer while one that changes its box
    recomputes. Only the default ``min_overlap_ratio`` (every hot-path caller
    uses it) is cached, and the ``_zone_match_memo`` field is internal -- history
    and event serialisation both whitelist detection fields, so it never leaves
    the process.
    """
    zone_id = zone.get('id') or zone.get('name')
    box = detection.get('box')
    if min_overlap_ratio != 0.2 or zone_id is None or not isinstance(detection, dict) or not isinstance(box, dict):
        return _detection_matches_zone_uncached(detection, zone, min_overlap_ratio)
    key = (zone_id, box.get('x'), box.get('y'), box.get('width'), box.get('height'))
    memo = detection.get('_zone_match_memo')
    if memo is None:
        memo = {}
        detection['_zone_match_memo'] = memo
    cached = memo.get(key)
    if cached is not None:
        return cached
    result = _detection_matches_zone_uncached(detection, zone, min_overlap_ratio)
    memo[key] = result
    return result


def _zone_pixel_bounds(diff_mask: Any, zone: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """Return the zone's pixel slice ``(px1, py1, px2, py2)`` inside the
    motion thumbnail (the mask's own ``H × W`` shape), or ``None`` when the zone
    geometry cannot be resolved.

    Zone coordinates are normalised (0-1) and are converted to pixel indices
    before slicing, falling back to ``zone.points`` when the rectangle fields
    are missing (same fallback as the fraction / bounds helpers).
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
        # Clamp the start index to at most W-1 / H-1 so a degenerate zone at the
        # far edge (x=1.0 or y=1.0, e.g. a zero-width rectangle) can never
        # produce ``px1 == W`` and an empty ``[W:W]`` slice. An empty slice makes
        # ``np.mean`` return NaN, and NaN survives every ``<``/``>`` gate in
        # ``zone_motion_detections`` -- emitting a spurious motion detection with
        # NaN confidence that later serialises to invalid JSON. The ``max(px1+1,
        # ...)`` below then guarantees ``px2 > px1`` (and ``py2 > py1``), so the
        # slice is always at least 1px and the fraction is always finite.
        frame_h, frame_w = diff_mask.shape
        px1 = max(0, min(frame_w - 1, int(x * frame_w)))
        py1 = max(0, min(frame_h - 1, int(y * frame_h)))
        px2 = min(frame_w, max(px1 + 1, int(round((x + w) * frame_w))))
        py2 = min(frame_h, max(py1 + 1, int(round((y + h) * frame_h))))
        return (px1, py1, px2, py2)
    except (TypeError, ValueError) as exc:
        logger.debug('Expected error resolving pixel bounds for zone %r: %s', zone_id, exc)
        return None


def _zone_motion_pixel_box(diff_mask: Any, zone: dict[str, Any]) -> dict[str, float] | None:
    """Return the tight normalised bounding box of the changed pixels inside
    a zone, or ``None`` when the mask/geometry can't produce one.

    Motion pseudo-detections used to carry the zone's FULL rectangle as their
    box, so every green overlay box (annotated snapshots, recording fallback
    clips, playback overlays) covered the whole area even when movement was
    confined to one corner. With the diff mask available, the box is shrunk to
    the actual changed region so the overlay points at where motion happened.
    """
    zone_id = str(zone.get('id') or zone.get('name') or id(zone))
    try:
        bounds = _zone_pixel_bounds(diff_mask, zone)
        if bounds is None:
            return None
        px1, py1, px2, py2 = bounds
        changed = np.where(diff_mask[py1:py2, px1:px2])
        if not changed[0].size:
            return None
        x0 = int(changed[1].min()) + px1
        y0 = int(changed[0].min()) + py1
        x1 = int(changed[1].max()) + px1
        y1 = int(changed[0].max()) + py1
        frame_h, frame_w = diff_mask.shape
        return {
            'x': round(max(0.0, min(1.0, x0 / frame_w)), 4),
            'y': round(max(0.0, min(1.0, y0 / frame_h)), 4),
            'width': round(max(0.001, min(1.0, (x1 - x0 + 1) / frame_w)), 4),
            'height': round(max(0.001, min(1.0, (y1 - y0 + 1) / frame_h)), 4),
        }
    except (TypeError, ValueError, IndexError, AttributeError) as exc:
        logger.debug('Expected error computing motion pixel box for zone %r: %s', zone_id, exc)
        return None
    except Exception as exc:
        if zone_id not in _zone_pixel_motion_errors:
            logger.warning('Unexpected error computing motion pixel box for zone %r: %s', zone_id, exc)
            _zone_pixel_motion_errors.add(zone_id)
        return None


def _zone_pixel_motion_fraction(diff_mask: Any, zone: dict[str, Any]) -> float:
    """Return the fraction of pixels inside a zone's bounding box that changed.

    ``diff_mask`` is the boolean (H×W) array from ``detect_frame_motion`` at
    the camera-local motion thumbnail resolution.
    """
    zone_id = str(zone.get('id') or zone.get('name') or id(zone))
    try:
        # A stale/corrupt mask must never be treated as evidence. In particular,
        # slicing a mask with the configured coordinates can produce an empty
        # array when a camera resolution or motion-frame size changed between
        # producers; ``np.mean(empty)`` is NaN, which bypasses normal threshold
        # comparisons and can leak NaN confidence into an event payload.
        if getattr(diff_mask, 'ndim', None) != 2 or not all(int(value) > 0 for value in diff_mask.shape):
            logger.debug('Ignoring invalid pixel-motion mask for zone %r with shape %s', zone_id, getattr(diff_mask, 'shape', None))
            return 0.0
        bounds = _zone_pixel_bounds(diff_mask, zone)
        if bounds is None:
            return 0.0
        px1, py1, px2, py2 = bounds
        region = diff_mask[py1:py2, px1:px2]
        if region.size == 0:
            return 0.0
        result = float(np.mean(region))
        if not np.isfinite(result):
            return 0.0
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
        # Per-zone sensitivity: a zone may override the camera/global gate and
        # scale on its motion rule so a sensitive doorway and a noisy tree-line
        # can coexist on one camera. ``None`` inherits the passed-in value.
        zone_gate = zone_motion_gate_fraction(zone)
        if zone_gate is None:
            zone_gate = gate_fraction
        zone_scale = zone_motion_scale_fraction(zone)
        if zone_scale is None:
            zone_scale = scale_fraction
        zone_fraction = -1.0
        if diff_mask is not None:
            zone_fraction = _zone_pixel_motion_fraction(diff_mask, zone)
            if zone_fraction < zone_gate:
                continue
            zone_confidence = round(min(1.0, zone_fraction / max(zone_scale, 1e-09)), 3)
        else:
            zone_confidence = frame_motion_confidence
        conf_threshold = zone_motion_min_confidence(zone)
        if zone_confidence < conf_threshold:
            logger.debug(
                'Motion zone %r: zone_fraction=%.4f zone_confidence=%.3f below conf_threshold=%.3f (scale_fraction=%.4f)',
                zone_id, zone_fraction, zone_confidence, conf_threshold, zone_scale,
            )
            continue
        # Upper bound of the motion rule's [min, max] window. Dropping the
        # detection HERE -- the single source of truth for motion, before it is
        # appended to alert_detections/recording_detections in live_monitor --
        # keeps alerts AND recordings consistent with the window (the
        # AlertEngine gate alone only suppressed the notification, not the
        # zone_motion_record_on_detect recording path). Defaults to 1.0, so
        # zones without a configured max are unaffected.
        max_threshold = zone_motion_max_confidence(zone)
        if zone_confidence > max_threshold:
            logger.debug(
                'Motion zone %r: zone_confidence=%.3f above max_confidence=%.3f -- ignored',
                zone_id, zone_confidence, max_threshold,
            )
            continue
        seen_zones.add(zone_id)
        # Shrink the box to the changed-pixel region when the diff mask is
        # available so overlays point at where movement actually happened;
        # fall back to the zone rectangle (first frame, fail-open, forced
        # scan) exactly as before.
        box = None
        if diff_mask is not None:
            box = _zone_motion_pixel_box(diff_mask, zone)
        result.append({
            'confidence': zone_confidence,
            'zone_id': zone_id,
            'zone_name': zone.get('name') or zone_id,
            'box': box or {
                'x': float(zone.get('x', 0)),
                'y': float(zone.get('y', 0)),
                'width': float(zone.get('width', 1)),
                'height': float(zone.get('height', 1)),
            },
        })
    return result


def _has_enabled_face_rule(zone: dict[str, Any]) -> bool:
    return any(
        str(rule.get('label') or '').strip().lower() == 'face'
        and rule.get('enabled', True)
        for rule in zone.get('object_rules') or []
    )


def _zone_key(zone: dict[str, Any]) -> str:
    return str(zone.get('id') or zone.get('name') or '')


def camera_face_zone_keys(settings: dict[str, Any]) -> set[str]:
    """Keys of enabled zones that carry an enabled ``face`` rule.

    Non-empty means the operator scoped face processing to those zones: the
    live pipeline drops faces detected outside them before the recognition
    pass. Empty means legacy/global behaviour (every face on the camera).
    """
    return {
        _zone_key(zone)
        for zone in (settings.get('detection') or {}).get('zones', [])
        if zone.get('enabled', True) and _has_enabled_face_rule(zone)
    }


def detection_label_allowed_for_zone(detection: dict[str, Any], zone: dict[str, Any], camera_labels: set[str]) -> bool:
    zone_labels = set(normalize_label_list(zone.get('object_labels', [])))
    allowed_labels = zone_labels or camera_labels
    if not allowed_labels:
        return True
    # ``detection_label_in_allowed`` canonicalizes the detection label AND
    # expands any umbrella group in the allow-list (e.g. ``animal`` matches a
    # ``cat`` detection), so a group configured on a zone/camera works the same
    # way a concrete label does.
    return detection_label_in_allowed(detection.get('label'), allowed_labels)


def filter_detections_for_camera_zones(
    detections: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    zone_monitor_key: str,
    require_zones: bool = False,
) -> list[dict[str, Any]]:
    policy = camera_policy(settings)
    compiled_zones = tuple(
        zone
        for zone in (policy.object_zones if zone_monitor_key == 'monitor_objects' else policy.motion_zones)
        if zone.zone.get(zone_monitor_key, True)
    )
    camera_labels = set(policy.camera_labels)
    # Track-id preservation: the live overlay renders each box's stable track
    # id beside its label so operators can verify tracker/classifier behavior
    # (and two same-label objects never look like one). Detection dicts pass
    # through this filter on the ~4 Hz hot path, so the id rides along with the
    # dict itself -- no reconstruction needed.
    # Face scoping: when the camera defines zones carrying an enabled ``face``
    # rule (including face-only zones with ``monitor_objects`` off), a ``face``
    # detection must land geometrically inside one of them -- its confidence
    # window is enforced later by the matching rule. When no zone carries a
    # face rule, faces follow the exact legacy path below.
    face_scope_zones = tuple(policy.face_zones) if zone_monitor_key == 'monitor_objects' else ()

    def _face_allowed(detection: dict[str, Any]) -> bool | None:
        """None = no face scoping configured (legacy path); True/False = scoped."""
        if not face_scope_zones or canonical_label(detection.get('label')) != 'face':
            return None
        return any(_detection_matches_compiled_zone(detection, zone) for zone in face_scope_zones)

    if not compiled_zones:
        if not require_zones:
            if face_scope_zones:
                # Every zone is face-only: the object axis has no zones, but
                # face scoping must still bite while non-face detections keep
                # the legacy camera-label / accept-all fallbacks.
                kept: list[dict[str, Any]] = []
                for detection in detections:
                    verdict = _face_allowed(detection)
                    if verdict is False:
                        continue
                    if (
                        verdict is None
                        and camera_labels
                        and not detection_label_in_allowed(detection.get('label'), camera_labels)
                    ):
                        continue
                    kept.append(detection)
                return kept
        if zone_monitor_key == 'monitor_objects' and camera_labels and (not require_zones):
            # Canonicalise the detection label through ``_LABEL_ALIASES`` exactly
            # like ``detection_label_allowed_for_zone`` does on the zones path.
            # ``camera_labels`` was already aliased by ``normalize_label_list``
            # ('human' -> 'person'), so a raw comparison here would silently drop
            # a 'human' detection that the same camera accepts once a zone is
            # configured -- the two paths must agree on what 'person' means.
            return [
                detection
                for detection in detections
                if detection_label_in_allowed(detection.get('label'), camera_labels)
            ]  # dict pass-through carries track_id
        # No zones and no camera labels: keep legacy "accept all" behavior so a
        # camera with object detection enabled but unconfigured still records
        # and alerts. Log the fallback once per call to aid debugging.
        if not camera_labels and not require_zones:
            logger.debug('filter_detections_for_camera_zones: no zones or camera labels configured; returning %d detections unfiltered', len(detections))
        return [] if require_zones else detections
    # Pre-compute each zone's allow-list once instead of re-parsing
    # ``zone['object_labels']`` for every (detection, zone) pair on the ~4 Hz
    # hot path. Empty sets preserve the legacy "no allow-list anywhere ->
    # accept all" behavior of ``detection_label_allowed_for_zone``.
    need_labels = zone_monitor_key == 'monitor_objects'
    zones_with_labels = [
        (zone, frozenset(zone.object_labels) or frozenset(camera_labels)) if need_labels else (zone, None)
        for zone in compiled_zones
    ]
    matched: list[dict[str, Any]] = []
    for detection in detections:
        verdict = _face_allowed(detection)
        if verdict is not None:
            if verdict:
                matched.append(detection)
            continue
        if any(
            _detection_matches_compiled_zone(detection, zone)
            and (
                not need_labels
                or not labels
                or detection_label_in_allowed(detection.get('label'), labels)
            )
            for zone, labels in zones_with_labels
        ):
            matched.append(detection)
    # Detection dicts (with their track_id annotation) pass through unchanged.
    return matched


def filter_detections_for_camera(detections: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    if not detection_settings.get('object_detection_enabled', True):
        return []
    return filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_objects')


def zone_object_rule_matches(settings: dict[str, Any], detection: dict[str, Any], *, action: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Zone/rule pairs a detection matches for ``action`` (``alert``/``record``).

    The same detection is asked this several times per cycle -- ``zone_alert_detections``,
    ``zone_record_on_detect`` (called ~3x), ``zone_name_for_detection`` and
    ``zone_detection_alert_rule_names`` all funnel through here. The result is a
    pure function of the settings, the action, and the detection's label /
    confidence / box, so it is memoised on the detection under those exact keys.
    ``id(settings)`` keys the (per-cycle stable) settings object so a different
    settings dict recomputes; the ``_rule_match_memo`` field is internal and
    dropped by history / event serialisation. Callers only read the returned
    list, so returning the cached list is safe.
    """
    if action not in ('alert', 'record'):
        raise ValueError(f"action must be 'alert' or 'record', got {action!r}")
    box = detection.get('box') if isinstance(detection, dict) else None
    if not isinstance(box, dict):
        return _zone_object_rule_matches_uncached(settings, detection, action)
    key = (
        id(settings), action, canonical_label(detection.get('label')),
        round(float(detection.get('confidence') or 0), 4),
        box.get('x'), box.get('y'), box.get('width'), box.get('height'),
    )
    memo = detection.get('_rule_match_memo')
    if memo is None:
        memo = {}
        detection['_rule_match_memo'] = memo
    if key in memo:
        return memo[key]
    result = _zone_object_rule_matches_uncached(settings, detection, action)
    memo[key] = result
    return result


def _zone_object_rule_matches_uncached(settings: dict[str, Any], detection: dict[str, Any], action: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    # Face-only zones (monitor_objects=False carrying a ``face`` rule) must be
    # reachable here too: face detections stamped with such a zone's id match
    # their rule through this matcher. Object detections can never carry that
    # zone's id -- they are filtered out by the monitor_objects axis upstream
    # -- so including these zones cannot make an object rule fire.
    compiled_zones = [
        compiled_zone for compiled_zone in camera_policy(settings).zones
        if compiled_zone.zone.get('monitor_objects', True) or _has_enabled_face_rule(compiled_zone.zone)
    ]
    label = canonical_label(detection.get('label'))
    if not label:
        return []
    # Hoisted out of the zone/rule loop: one coercion per detection, not one
    # per matched rule.
    confidence = float(detection.get('confidence') or 0)
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for compiled_zone in compiled_zones:
        zone = compiled_zone.zone
        if not _detection_matches_compiled_zone(detection, compiled_zone):
            continue
        for rule in compiled_zone.rules:
            if not rule.get('enabled', True):
                continue
            if action == 'alert':
                schedules = rule.get('alert_schedules')
                deliverable = any(
                    isinstance(schedule, dict) and (schedule.get('email_enabled') or schedule.get('push_enabled'))
                    for schedule in schedules
                ) if isinstance(schedules, list) and schedules else (rule.get('email_enabled') or rule.get('push_enabled'))
                if not deliverable:
                    continue
            if action == 'record' and (not rule.get('record_on_detect', True)):
                continue
            # ``label_matches`` canonicalizes the rule label the same way the
            # detection label is (so ``human``/``people``/``pedestrian`` all
            # match a ``person`` detection) AND expands umbrella group rules
            # (an ``animal``/``pet`` rule matches a ``cat`` detection). The
            # recording path (``detection_has_matching_record_rule``) and the
            # AlertEngine apply the same matcher, so all three agree.
            if not label_matches(label, rule.get('label')):
                continue
            # Per-rule confidence window (confidence hoisted above): the
            # detection must sit inside
            # ``[min_confidence, max_confidence]``. ``max_confidence`` defaults
            # to 1.0 (no upper limit) so rules that never configured it behave
            # exactly as before. This window is authoritative over the global
            # ONNX confidence slider -- a higher ``min`` gates here even though
            # the detector floor is lower, and a ``max`` below 1.0 drops
            # over-confident detections the global slider would otherwise pass.
            if confidence < float(rule.get('min_confidence', 0.5)):
                continue
            if confidence > float(rule.get('max_confidence', 1.0)):
                continue
            matches.append((zone, rule))
    return matches


def zone_object_alert_rules(settings: dict[str, Any]) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    # A zone that monitors ONLY motion (``monitor_objects=False`` but
    # ``monitor_motion=True``) must still contribute its motion rules to the
    # AlertEngine rule list. Filtering on ``monitor_objects`` alone silently
    # disabled email/push motion alerts for motion-only zones -- the recording
    # axis (``zone_motion_record_on_detect``) already honours those zones, so
    # the alert axis must too. Object rules inside a motion-only zone remain
    # harmless: object detections are filtered through the ``monitor_objects``
    # axis before they can carry that zone's id, so they can never match here.
    zones = [
        zone for zone in detection_settings.get('zones', [])
        if zone.get('enabled', True)
        and (zone.get('monitor_objects', True) or zone.get('monitor_motion', True) or _has_enabled_face_rule(zone))
    ]
    rules: list[dict[str, Any]] = []
    camera_key = str(settings.get('id') or settings.get('name') or 'camera').strip() or 'camera'
    for zone in zones:
        zone_id = str(zone.get('id') or zone.get('name') or 'zone')
        label_totals: dict[str, int] = {}
        for _rule in zone.get('object_rules') or []:
            _label = str(_rule.get('label') or '').strip().lower()
            has_delivery = any(
                isinstance(_schedule, dict) and (_schedule.get('email_enabled') or _schedule.get('push_enabled'))
                for _schedule in (
                    _rule.get('alert_schedules')
                    if isinstance(_rule.get('alert_schedules'), list) and _rule.get('alert_schedules')
                    else [_rule]
                )
            )
            _monitored = (
                zone.get('monitor_objects', True)
                or (_label == 'motion' and zone.get('monitor_motion', True))
                or (_label == 'face' and _has_enabled_face_rule(zone))
            )
            if _label and _rule.get('enabled', True) and has_delivery and _monitored:
                label_totals[_label] = label_totals.get(_label, 0) + 1
        label_seen: dict[str, int] = {}
        for rule in zone.get('object_rules') or []:
            if not rule.get('enabled', True):
                continue
            label = str(rule.get('label') or '').strip().lower()
            if not label:
                continue
            # A motion-only zone (``monitor_objects=False``) contributes ONLY its
            # motion rules to the alert list. Object rules inside it stay inert:
            # object detections are filtered through the ``monitor_objects`` axis
            # and can never carry this zone's id, so including them would just
            # add dead rules to the engine's list.
            if not zone.get('monitor_objects', True) and label not in ('motion', 'face'):
                continue
            raw_schedules = rule.get('alert_schedules')
            schedules = raw_schedules if isinstance(raw_schedules, list) and raw_schedules else [rule]
            deliverable_schedules = [
                (schedule_index, schedule)
                for schedule_index, schedule in enumerate(schedules, start=1)
                if isinstance(schedule, dict) and (schedule.get('email_enabled') or schedule.get('push_enabled'))
            ]
            if not deliverable_schedules:
                continue
            label_seen[label] = label_seen.get(label, 0) + 1
            rule_suffix = f" [{rule.get('id') or label_seen[label]}]" if label_totals.get(label, 0) > 1 else ''
            base_name = zone_rule_name(settings, zone, rule) + rule_suffix
            cooldown_key = f'{camera_key}::{zone_id}::{label}'
            if label_totals.get(label, 0) > 1:
                cooldown_key += f'::{rule.get("id") or label_seen[label]}'
            for schedule_index, schedule in deliverable_schedules:
                schedule_id = str(schedule.get('id') or f'schedule-{schedule_index}')
                schedule_name = f'{base_name} [Schedule {schedule_index}]' if len(schedules) > 1 else base_name
                rules.append({
                    'name': schedule_name,
                    'schedule_name': base_name,
                    'cooldown_key': f'{cooldown_key}::{schedule_id}' if len(schedules) > 1 else cooldown_key,
                    'object': label,
                    'zone_id': zone_id,
                    # Motion's and face's canonical confidence default is 0.45
                    # (see zone_motion_min_confidence / the Face Confidence
                    # setting); object classes default to 0.5. Matching the
                    # detection axis here keeps a rule missing min_confidence
                    # gating alerts at the same threshold that produced the
                    # detection in the first place.
                    'min_confidence': rule.get('min_confidence', 0.45 if label in ('motion', 'face') else 0.5),
                    'max_confidence': rule.get('max_confidence', 1.0),
                    'cooldown_seconds': rule.get('cooldown_seconds', 60),
                    'enabled': True,
                    'email_enabled': bool(schedule.get('email_enabled', False)),
                    'email_recipients': normalize_email_recipients(schedule.get('email_recipients', [])),
                    'push_enabled': bool(schedule.get('push_enabled', False)),
                    'active_start': schedule.get('active_start'),
                    'active_end': schedule.get('active_end'),
                    'notify_start': schedule.get('notify_start'),
                    'notify_end': schedule.get('notify_end'),
                    'schedule_id': schedule_id,
                    'schedule': schedule,
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
    matched = zone_object_rule_matches(settings, detection, action='alert')
    names: set[str] = set()
    for zone, rule in matched:
        label = str(rule.get('label') or '').strip().lower()
        same_label_rules = [
            candidate for candidate in zone.get('object_rules') or []
            if candidate.get('enabled', True)
            and str(candidate.get('label') or '').strip().lower() == label
            and any(
                isinstance(schedule, dict) and (schedule.get('email_enabled') or schedule.get('push_enabled'))
                for schedule in (
                    candidate.get('alert_schedules')
                    if isinstance(candidate.get('alert_schedules'), list) and candidate.get('alert_schedules')
                    else [candidate]
                )
            )
        ]
        if len(same_label_rules) > 1:
            rule_position = next((index for index, candidate in enumerate(same_label_rules, start=1) if candidate is rule), 1)
            suffix = f" [{rule.get('id') or rule_position}]"
        else:
            suffix = ''
        base_name = zone_rule_name(settings, zone, rule) + suffix
        schedules = rule.get('alert_schedules')
        schedules = schedules if isinstance(schedules, list) and schedules else [rule]
        for schedule_index, schedule in enumerate(schedules, start=1):
            if not isinstance(schedule, dict) or not (
                schedule.get('email_enabled') or schedule.get('push_enabled')
            ):
                continue
            names.add(f'{base_name} [Schedule {schedule_index}]' if len(schedules) > 1 else base_name)
    return names


def detection_has_matching_record_rule(detection: dict[str, Any], rules: list[dict[str, Any]]) -> bool:
    """Return True if any enabled alert rule covers this detection by label and confidence.

    Cooldown and time-window are intentionally ignored so a recording is created on every
    matching detection, not only when a new alert notification is emitted.
    """
    label = canonical_label(detection.get('label'))
    if not label:
        return False
    confidence = float(detection.get('confidence') or 0)
    for rule in rules:
        if not rule.get('enabled', True):
            continue
        # ``label_matches`` expands an umbrella group rule (``animal``/``pet``)
        # to its member labels, so a group record rule captures a ``cat`` the
        # same way the alert path does.
        if not label_matches(label, rule.get('object')):
            continue
        try:
            min_conf = float(rule.get('min_confidence', 0.0 if label == 'motion' else 0.5))
        except (TypeError, ValueError):
            min_conf = 0.0 if label == 'motion' else 0.5
        try:
            max_conf = float(rule.get('max_confidence', 1.0))
        except (TypeError, ValueError):
            max_conf = 1.0
        if min_conf <= confidence <= max_conf:
            return True
    return False


def filter_motion_detections_by_objects(
    motion_detections: list[dict[str, Any]],
    object_detections: list[dict[str, Any]],
    *,
    min_motion_overlap: float = 0.15,
) -> list[dict[str, Any]]:
    """Use object detections as the authoritative label over generic motion.

    A motion box is a fallback signal. When a concrete object box covers a
    meaningful part of that motion box, keeping both creates a misleading
    second ``Motion`` subject (and can make motion appear to replace the
    object). Motion boxes that do not overlap an object remain available for
    motion-only recording and alerts.

    ``min_motion_overlap`` is measured against the motion box, not the object
    box: an object may be much smaller than a configured zone while still
    explaining the changed pixels inside it.
    """
    if not motion_detections or not object_detections:
        return motion_detections
    try:
        threshold = min(1.0, max(0.0, float(min_motion_overlap)))
    except (TypeError, ValueError):
        threshold = 0.15

    def _area(box: Any) -> float:
        if not isinstance(box, dict):
            return 0.0
        try:
            return max(0.0, float(box.get('width') or 0)) * max(0.0, float(box.get('height') or 0))
        except (TypeError, ValueError):
            return 0.0

    def _overlap_ratio(motion_box: Any, object_box: Any) -> float:
        motion_area = _area(motion_box)
        object_area = _area(object_box)
        if motion_area <= 0 or object_area <= 0:
            return 0.0
        try:
            mx1 = float(motion_box.get('x') or 0)
            my1 = float(motion_box.get('y') or 0)
            mx2 = mx1 + float(motion_box.get('width') or 0)
            my2 = my1 + float(motion_box.get('height') or 0)
            ox1 = float(object_box.get('x') or 0)
            oy1 = float(object_box.get('y') or 0)
            ox2 = ox1 + float(object_box.get('width') or 0)
            oy2 = oy1 + float(object_box.get('height') or 0)
        except (TypeError, ValueError):
            return 0.0
        intersection = max(0.0, min(mx2, ox2) - max(mx1, ox1)) * max(0.0, min(my2, oy2) - max(my1, oy1))
        return intersection / motion_area

    concrete_boxes = [
        detection.get('box')
        for detection in object_detections
        if isinstance(detection, dict)
        and detection.get('box')
        and str(detection.get('label') or '').strip().lower() not in {'', 'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous'}
        and not detection.get('motion_event')
    ]
    if not concrete_boxes:
        return motion_detections
    return [
        motion for motion in motion_detections
        if not any(_overlap_ratio(motion.get('box'), object_box) >= threshold for object_box in concrete_boxes)
    ]


def normalize_detection_boxes_for_frame(detections: list[dict[str, Any]], frame: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize detector boxes without letting one malformed row kill a cycle.

    Detector plugins and crop-based inference both feed this boundary. Invalid,
    non-finite, or zero-area geometry is not useful to any downstream zone,
    tracker, snapshot, or recording consumer, so those rows are discarded.
    Valid normalized boxes are clipped to the frame; pixel-space boxes are
    converted first and then receive the same clipping.
    """
    try:
        width = float(frame.get('width') or 0)
        height = float(frame.get('height') or 0)
    except (AttributeError, TypeError, ValueError):
        return detections
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        return detections
    normalized: list[dict[str, Any]] = []
    for detection in detections:
        if not isinstance(detection, dict):
            continue
        box = detection.get('box')
        if not isinstance(box, dict):
            normalized_detection = dict(detection)
            if 'confidence' in normalized_detection:
                try:
                    confidence = float(normalized_detection.get('confidence') or 0)
                    normalized_detection['confidence'] = round(confidence, 3) if math.isfinite(confidence) else 0.0
                except (TypeError, ValueError):
                    normalized_detection['confidence'] = 0.0
            normalized.append(normalized_detection)
            continue
        try:
            box_x = float(box.get('x') or 0)
            box_y = float(box.get('y') or 0)
            box_width = float(box.get('width') or 0)
            box_height = float(box.get('height') or 0)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (box_x, box_y, box_width, box_height)):
            continue
        if box_width <= 0 or box_height <= 0:
            continue
        is_fully_normalized = (
            max(abs(box_x), abs(box_y), box_width, box_height) <= 1
            and 0 <= box_x <= 1
            and 0 <= box_y <= 1
            and box_x + box_width <= 1
            and box_y + box_height <= 1
        )
        confidence_is_safe = True
        if 'confidence' in detection:
            try:
                confidence_is_safe = math.isfinite(float(detection.get('confidence') or 0))
            except (TypeError, ValueError):
                confidence_is_safe = False
        if is_fully_normalized and confidence_is_safe:
            normalized.append(detection)
            continue
        if max(abs(box_x), abs(box_y), box_width, box_height) > 1:
            box_x /= width
            box_y /= height
            box_width /= width
            box_height /= height
        x1 = max(0.0, min(1.0, box_x))
        y1 = max(0.0, min(1.0, box_y))
        x2 = max(x1, min(1.0, box_x + box_width))
        y2 = max(y1, min(1.0, box_y + box_height))
        if x2 <= x1 or y2 <= y1:
            continue
        normalized_detection = {
            **detection,
            'box': {
                'x': round(x1, 4),
                'y': round(y1, 4),
                'width': round(x2 - x1, 4),
                'height': round(y2 - y1, 4),
            },
        }
        if 'confidence' in normalized_detection:
            try:
                confidence = float(normalized_detection.get('confidence') or 0)
                normalized_detection['confidence'] = round(confidence, 3) if math.isfinite(confidence) else 0.0
            except (TypeError, ValueError):
                normalized_detection['confidence'] = 0.0
        normalized.append(normalized_detection)
    return normalized
