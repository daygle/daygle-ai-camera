"""Second look at borderline detections.

A subject that is small, dark, partly hidden or at an odd angle often scores
just under the alert threshold on the full-frame pass: the model saw *something*
but the 640px downscale left it too few pixels to be sure. Lowering the
threshold to catch it lets every other weak guess through as well.

This module gives only those near-misses a closer look instead. The full-frame
pass runs at a lower floor (``second_look_floor``), and each detection that
lands between that floor and the real threshold is re-checked:

1. crop the full-resolution frame around the box (with context), so the small
   subject fills far more of the model input,
2. run the detector on the crop, and again on a mirrored copy of the crop,
3. keep the detection only when both views, on average, clear the real
   threshold for the same label at the same place.

Everything still under the threshold after that is dropped, so downstream code
sees exactly what it saw before, plus the confirmed near-misses. The cost is
two extra inferences per candidate, capped at ``MAX_CANDIDATES`` per cycle and
spent on the strongest candidates first. Off by default
(``object_detection_second_look``).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable

logger = logging.getLogger('daygle.ai')

# The full-frame pass runs at ``threshold * FLOOR_RATIO`` (never below
# ``MIN_FLOOR``) so near-misses are visible to the re-check. 0.6 turns a 0.5
# threshold into a 0.3 floor.
FLOOR_RATIO = 0.6
MIN_FLOOR = 0.05

# Extra inferences are two per candidate, so this bounds the added cost at
# four model runs per cycle.
MAX_CANDIDATES = 2

# The crop is the box grown to this multiple of its size (for context), and
# never smaller than ``MIN_CROP_FRACTION`` of the frame on each axis so a tiny
# box still gets enough surroundings to be recognisable.
CROP_SCALE = 2.5
MIN_CROP_FRACTION = 0.15

# A re-check detection counts as the same object when its box overlaps the
# candidate by at least this IoU.
MATCH_IOU = 0.3


def second_look_enabled(live_settings: dict[str, Any]) -> bool:
    """Resolve the ``object_detection_second_look`` toggle (default off)."""
    from app.utils import normalize_bool_setting
    return normalize_bool_setting(live_settings.get('object_detection_second_look'), False)


def second_look_floor(threshold: float) -> float:
    """Confidence the full-frame pass runs at so near-misses are kept."""
    return max(MIN_FLOOR, min(float(threshold), float(threshold) * FLOOR_RATIO))


def _iou(a: dict[str, float], b: dict[str, float]) -> float:
    ax1, ay1 = a['x'], a['y']
    ax2, ay2 = ax1 + a['width'], ay1 + a['height']
    bx1, by1 = b['x'], b['y']
    bx2, by2 = bx1 + b['width'], by1 + b['height']
    iw = min(ax2, bx2) - max(ax1, bx1)
    ih = min(ay2, by2) - max(ay1, by1)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = a['width'] * a['height'] + b['width'] * b['height'] - inter
    return inter / union if union > 0 else 0.0


def _box(value: Any) -> dict[str, float] | None:
    """A finite, positive-area normalized box clipped to [0, 1], or None."""
    if not isinstance(value, dict):
        return None
    try:
        x = float(value.get('x') or 0)
        y = float(value.get('y') or 0)
        width = float(value.get('width') or 0)
        height = float(value.get('height') or 0)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, width, height)) or width <= 0 or height <= 0:
        return None
    x1, y1 = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
    x2, y2 = max(x1, min(1.0, x + width)), max(y1, min(1.0, y + height))
    if x2 <= x1 or y2 <= y1:
        return None
    return {'x': x1, 'y': y1, 'width': x2 - x1, 'height': y2 - y1}


def _crop_region(box: dict[str, float]) -> tuple[float, float, float, float]:
    """Normalized ``(x, y, w, h)`` crop centred on ``box`` with context."""
    width = min(1.0, max(box['width'] * CROP_SCALE, MIN_CROP_FRACTION))
    height = min(1.0, max(box['height'] * CROP_SCALE, MIN_CROP_FRACTION))
    cx = box['x'] + box['width'] / 2.0
    cy = box['y'] + box['height'] / 2.0
    x = max(0.0, min(1.0 - width, cx - width / 2.0))
    y = max(0.0, min(1.0 - height, cy - height / 2.0))
    return x, y, width, height


def _best_match(
    detections: list[dict[str, Any]],
    label: str,
    candidate_box: dict[str, float],
    region: tuple[float, float, float, float],
    *,
    mirrored: bool,
) -> tuple[float, dict[str, float] | None]:
    """Highest confidence (and its frame-space box) among same-label crop
    detections that overlap the candidate; ``(0.0, None)`` when none do."""
    rx, ry, rw, rh = region
    best_confidence, best_box = 0.0, None
    for det in detections or []:
        if not isinstance(det, dict) or str(det.get('label') or '').strip().lower() != label:
            continue
        local = _box(det.get('box'))
        if local is None:
            continue
        local_x = 1.0 - (local['x'] + local['width']) if mirrored else local['x']
        mapped = {
            'x': rx + local_x * rw,
            'y': ry + local['y'] * rh,
            'width': local['width'] * rw,
            'height': local['height'] * rh,
        }
        if _iou(mapped, candidate_box) < MATCH_IOU:
            continue
        try:
            confidence = float(det.get('confidence') or 0)
        except (TypeError, ValueError):
            continue
        if confidence > best_confidence:
            best_confidence, best_box = confidence, mapped
    return best_confidence, best_box


def confirm_borderline_detections(
    detector: Any,
    frame: Any,
    detections: list[dict[str, Any]],
    *,
    threshold: float,
    is_relevant: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    max_candidates: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Re-check detections scoring below ``threshold`` and keep the confirmed.

    ``detections`` is the full-frame pass run at ``second_look_floor``. Every
    detection at or above ``threshold`` passes straight through. Below it, the
    strongest ``max_candidates`` that ``is_relevant`` keeps (e.g. ones inside a
    watched zone, with a watched label) get the crop + mirror re-check; all
    other sub-threshold detections are dropped. Never raises: a failed re-check
    simply drops that candidate.
    """
    threshold = float(threshold)
    kept: list[dict[str, Any]] = []
    borderline: list[dict[str, Any]] = []
    for det in detections or []:
        if not isinstance(det, dict):
            continue
        try:
            confidence = float(det.get('confidence') or 0)
        except (TypeError, ValueError):
            continue
        (kept if confidence >= threshold else borderline).append(det)
    if not borderline or max_candidates <= 0:
        return kept
    if not (hasattr(frame, 'shape') and getattr(frame, 'ndim', 0) == 3) or not hasattr(detector, 'detect_frame'):
        return kept
    if is_relevant is not None:
        try:
            borderline = is_relevant(borderline)
        except Exception:  # noqa: BLE001 - relevance filtering is best-effort
            logger.debug('Second-look relevance filter failed', exc_info=True)
            return kept
    borderline.sort(key=lambda det: float(det.get('confidence') or 0), reverse=True)

    import numpy as np

    frame_h, frame_w = frame.shape[:2]
    floor = second_look_floor(threshold)
    for det in borderline[:max_candidates]:
        candidate_box = _box(det.get('box'))
        label = str(det.get('label') or '').strip().lower()
        if candidate_box is None or not label:
            continue
        region = _crop_region(candidate_box)
        rx, ry, rw, rh = region
        x1, y1 = int(rx * frame_w), int(ry * frame_h)
        x2, y2 = int((rx + rw) * frame_w), int((ry + rh) * frame_h)
        if x2 - x1 < 8 or y2 - y1 < 8:
            continue
        crop = frame[y1:y2, x1:x2]
        # The crop's own pixel bounds, so mapped boxes line up exactly.
        region = (x1 / frame_w, y1 / frame_h, (x2 - x1) / frame_w, (y2 - y1) / frame_h)
        try:
            crop_confidence, crop_box = _best_match(
                detector.detect_frame(crop, confidence=floor), label, candidate_box, region, mirrored=False,
            )
            mirrored = np.ascontiguousarray(crop[:, ::-1])
            mirror_confidence, _mirror_box = _best_match(
                detector.detect_frame(mirrored, confidence=floor), label, candidate_box, region, mirrored=True,
            )
        except Exception:  # noqa: BLE001 - a failed re-check drops the candidate
            logger.debug('Second-look re-check failed for %s', label, exc_info=True)
            continue
        combined = (crop_confidence + mirror_confidence) / 2.0
        logger.debug(
            'Second look: %s full=%.3f crop=%.3f mirror=%.3f combined=%.3f threshold=%.3f -> %s',
            label, float(det.get('confidence') or 0), crop_confidence, mirror_confidence,
            combined, threshold, 'kept' if combined >= threshold else 'dropped',
        )
        if combined < threshold or crop_box is None:
            continue
        kept.append({
            **det,
            'confidence': round(combined, 4),
            'box': {key: round(value, 4) for key, value in crop_box.items()},
            'second_look': True,
        })
    return kept
