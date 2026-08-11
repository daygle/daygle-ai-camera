"""Motion-region-guided high-resolution object detection.

Full-frame YOLO downscales the whole image to the model's input size (e.g.
640x640), so a person or car that is only a handful of pixels far from the
camera can vanish below the detector's resolution. This module recovers those:
it reads the motion ``diff_mask`` the gate already produced, crops the
full-resolution frame to the regions that actually moved, and re-runs the
detector on each crop. Because a small crop is letterboxed up to the full model
input, the distant subject is effectively detected at much higher resolution.
The crop detections are mapped back to full-frame coordinates and merged with
the full-frame pass, de-duplicated by class-aware IoU.

Cost scales with the number of motion regions (capped), so this is opt-in
(``object_detection_region_boost``) — most valuable on cameras watching a large
area where subjects appear small.
"""
from __future__ import annotations

from typing import Any


def _motion_region_boxes(
    diff_mask: Any,
    *,
    max_regions: int,
    min_area_frac: float,
    pad_frac: float,
) -> list[tuple[float, float, float, float]]:
    """Return up to ``max_regions`` padded, normalized ``(x, y, w, h)`` boxes
    around the connected motion blobs in ``diff_mask`` (largest first).

    Regions covering more than half the frame are skipped — they gain nothing
    from cropping (they are already ~full-frame) and would just double the cost.
    """
    import cv2
    import numpy as np

    mask = np.asarray(diff_mask)
    if mask.ndim != 2 or not mask.any():
        return []
    mask_h, mask_w = mask.shape
    mask_u8 = mask.astype(np.uint8) * 255
    # Dilate so a subject broken into a few blobs (limbs, gaps) merges into one
    # region instead of producing several tiny crops.
    kernel = np.ones((3, 3), np.uint8)
    mask_u8 = cv2.dilate(mask_u8, kernel, iterations=1)
    contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    scored: list[tuple[float, float, float, float, float]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area_frac = (w * h) / float(mask_w * mask_h)
        if area_frac < min_area_frac or area_frac > 0.5:
            continue
        scored.append((area_frac, x / mask_w, y / mask_h, w / mask_w, h / mask_h))
    scored.sort(reverse=True)

    boxes: list[tuple[float, float, float, float]] = []
    for _area, nx, ny, nw, nh in scored[:max_regions]:
        px = max(0.0, nx - pad_frac * nw)
        py = max(0.0, ny - pad_frac * nh)
        pw = min(1.0 - px, nw * (1.0 + 2.0 * pad_frac))
        ph = min(1.0 - py, nh * (1.0 + 2.0 * pad_frac))
        if pw > 0 and ph > 0:
            boxes.append((px, py, pw, ph))
    return boxes


def _dedup_by_iou(detections: list[dict[str, Any]], iou_threshold: float) -> list[dict[str, Any]]:
    """Class-aware greedy de-dup: keep the highest-confidence box, drop any
    later same-label box overlapping it by >= ``iou_threshold``."""
    def _iou(a: dict[str, Any], b: dict[str, Any]) -> float:
        ax1, ay1 = float(a.get("x") or 0), float(a.get("y") or 0)
        ax2, ay2 = ax1 + float(a.get("width") or 0), ay1 + float(a.get("height") or 0)
        bx1, by1 = float(b.get("x") or 0), float(b.get("y") or 0)
        bx2, by2 = bx1 + float(b.get("width") or 0), by1 + float(b.get("height") or 0)
        ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
        iw, ih = ix2 - ix1, iy2 - iy1
        if iw <= 0 or ih <= 0:
            return 0.0
        inter = iw * ih
        union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
        return inter / union if union > 0 else 0.0

    ordered = sorted(detections, key=lambda d: float(d.get("confidence") or 0.0), reverse=True)
    kept: list[dict[str, Any]] = []
    for det in ordered:
        label = str(det.get("label") or "").strip().lower()
        box = det.get("box") or {}
        if any(
            label == str(k.get("label") or "").strip().lower()
            and _iou(box, k.get("box") or {}) >= iou_threshold
            for k in kept
        ):
            continue
        kept.append(det)
    return kept


def detect_with_region_boost(
    detector: Any,
    frame: Any,
    diff_mask: Any,
    base_detections: list[dict[str, Any]],
    *,
    confidence: float | None = None,
    max_regions: int = 3,
    min_area_frac: float = 0.002,
    pad_frac: float = 0.25,
    iou_dedup: float = 0.5,
) -> list[dict[str, Any]]:
    """Augment ``base_detections`` (the full-frame pass) with detections from
    zoomed-in crops around motion, merged and de-duplicated.

    Falls back to ``base_detections`` unchanged when there is no usable mask,
    no detector ``detect_frame`` method, or no qualifying motion region — so it
    is always safe to call. Never raises out; a per-crop failure is skipped."""
    if diff_mask is None or frame is None or not hasattr(detector, "detect_frame"):
        return base_detections
    if not (hasattr(frame, "shape") and getattr(frame, "ndim", 0) == 3):
        return base_detections

    regions = _motion_region_boxes(
        diff_mask, max_regions=max_regions, min_area_frac=min_area_frac, pad_frac=pad_frac
    )
    if not regions:
        return base_detections

    frame_h, frame_w = frame.shape[:2]
    merged = list(base_detections)
    for rx, ry, rw, rh in regions:
        x1 = int(rx * frame_w)
        y1 = int(ry * frame_h)
        x2 = int((rx + rw) * frame_w)
        y2 = int((ry + rh) * frame_h)
        if x2 - x1 < 8 or y2 - y1 < 8:
            continue  # crop too small to help
        crop = frame[y1:y2, x1:x2]
        try:
            crop_detections = detector.detect_frame(crop, confidence=confidence)
        except Exception:
            continue  # a bad crop must never break the whole detection cycle
        for det in crop_detections:
            box = det.get("box") or {}
            merged.append({
                **det,
                "box": {
                    "x": round(rx + float(box.get("x") or 0) * rw, 4),
                    "y": round(ry + float(box.get("y") or 0) * rh, 4),
                    "width": round(float(box.get("width") or 0) * rw, 4),
                    "height": round(float(box.get("height") or 0) * rh, 4),
                },
                "region_boost": True,
            })
    return _dedup_by_iou(merged, iou_dedup)


def region_boost_enabled(live_settings: dict[str, Any]) -> bool:
    """Resolve the ``object_detection_region_boost`` toggle (default off)."""
    from app.utils import normalize_bool_setting
    return normalize_bool_setting(live_settings.get("object_detection_region_boost"), False)
