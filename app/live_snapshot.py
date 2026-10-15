"""Live-snapshot rendering helpers extracted from ``app/main.py`` (Phase-25).

``render_live_snapshot_jpeg_overlay`` is pure: it takes JPEG bytes plus
detections and returns annotated JPEG bytes, using inline ``cv2`` / ``numpy``
imports and no module-level state in ``app.main``.

The module was carved out as the lowest-risk first move in main.py's
ongoing extraction work (the project already splits
``camera_config`` / ``config_facades`` / ``ai_settings`` /
``payload_validators`` / ``zone_schema`` / ``zone_detection`` /
``camera_health`` modules).
"""

from __future__ import annotations

from html import escape
from typing import Any

from app.detection_status import GENERIC_TRIGGER_LABELS


def _box_overlap_ratio(outer: Any, inner: Any) -> float:
    """Return the intersection area as a fraction of ``outer``'s area."""
    if not isinstance(outer, dict) or not isinstance(inner, dict):
        return 0.0
    try:
        ox = float(outer.get('x') or 0)
        oy = float(outer.get('y') or 0)
        ow = max(0.0, float(outer.get('width') or 0))
        oh = max(0.0, float(outer.get('height') or 0))
        ix = float(inner.get('x') or 0)
        iy = float(inner.get('y') or 0)
        iw = max(0.0, float(inner.get('width') or 0))
        ih = max(0.0, float(inner.get('height') or 0))
    except (TypeError, ValueError):
        return 0.0
    outer_area = ow * oh
    if outer_area <= 0 or iw <= 0 or ih <= 0:
        return 0.0
    intersection = max(0.0, min(ox + ow, ix + iw) - max(ox, ix)) * max(0.0, min(oy + oh, iy + ih) - max(oy, iy))
    return intersection / outer_area


def filter_object_priority_detections(
    detections: list[dict[str, Any]],
    *,
    min_motion_overlap: float = 0.15,
) -> list[dict[str, Any]]:
    """Hide generic motion overlays when a concrete object explains them.

    Motion remains a useful fallback: a motion box is removed only when its
    changed-pixel region overlaps a concrete object box. Unrelated motion boxes
    remain visible, and this helper affects rendering only (not motion alerts or
    recording decisions).
    """
    if not detections:
        return detections
    try:
        threshold = min(1.0, max(0.0, float(min_motion_overlap)))
    except (TypeError, ValueError):
        threshold = 0.15
    object_boxes = [
        detection.get('box')
        for detection in detections
        if isinstance(detection, dict)
        and detection.get('box')
        and str(detection.get('label') or '').strip().lower() not in GENERIC_TRIGGER_LABELS
        and not detection.get('motion_event')
    ]
    if not object_boxes:
        return detections
    return [
        detection
        for detection in detections
        if not (
            isinstance(detection, dict)
            and (
                detection.get('motion_event')
                or str(detection.get('label') or '').strip().lower() == 'motion'
            )
            and any(_box_overlap_ratio(detection.get('box'), object_box) >= threshold for object_box in object_boxes)
        )
    ]


SNAPSHOT_THUMB_MAX_WIDTH = 416


def overlay_detection_rows(detections: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize detections into the shape the JPEG overlay draws.

    Accepts both payload shapes: capture-time detections (coordinates nested
    under ``box``) and the flat ``x``/``y``/``width``/``height`` columns the
    event API returns. Only label/confidence/box/motion_event survive, which is
    what makes a capture-time thumbnail and an on-the-fly render of the same
    event draw the exact same boxes - the alert_matched/alert_triggered flags
    carried by capture-time rows would otherwise make the two paths disagree.
    """
    rows: list[dict[str, Any]] = []
    for detection in detections or []:
        box = detection.get('box') or {}
        # ``box`` coordinates win; fall back to the flat columns. Inlined (no
        # per-detection closure) so this stays cheap on the per-frame path and
        # cannot late-bind the loop variables.
        coords = {}
        for name in ('x', 'y', 'width', 'height'):
            value = box.get(name)
            coords[name] = detection.get(name) if value is None else value
        rows.append({
            'label': detection.get('label'),
            'confidence': detection.get('confidence'),
            'box': coords,
            'motion_event': detection.get('motion_event', False),
        })
    return rows


def build_event_thumbnail(image_bytes: bytes, detections: list[dict[str, Any]] | None) -> bytes | None:
    """Render the gallery thumbnail for a captured frame, or ``None``.

    Called ONCE at capture time so ``GET /api/events/{id}/snapshot?thumb=1``
    can serve a stored file instead of decoding, annotating and re-encoding the
    full-resolution frame per gallery row. The pipeline is deliberately the
    same one the endpoint runs (same overlay rows, same box priority filter,
    same downscale), so a stored thumbnail is visually identical to what an
    on-the-fly render would have produced.

    Returns ``None`` when the frame cannot be rendered (no ``cv2``, undecodable
    bytes) so callers skip the file entirely - storing the untouched
    full-resolution frame as a "thumbnail" would only double the disk cost, and
    the endpoint still falls back to rendering on the fly.
    """
    thumb = render_live_snapshot_jpeg_overlay(
        image_bytes,
        filter_object_priority_detections(overlay_detection_rows(detections)),
        max_width=SNAPSHOT_THUMB_MAX_WIDTH,
    )
    return None if thumb is image_bytes else thumb


def render_live_snapshot_jpeg_overlay(
    image_bytes: bytes,
    detections: list[dict[str, Any]],
    *,
    max_width: int | None = None,
) -> bytes:
    """Return the frame with detection boxes drawn on it.

    ``max_width`` downscales the frame before the boxes are drawn, so a
    thumbnail request decodes and encodes exactly once and ships a fraction of
    the bytes (the Snapshots gallery renders 208px-wide thumbs from
    full-resolution frames). Box coordinates are normalized, so the overlay
    lands correctly at any size. Callers that want the original frame pass
    nothing.
    """
    detections = filter_object_priority_detections(detections)
    # Nothing to draw AND nothing to resize: hand the original bytes back so
    # the common case never pays for a decode/encode round trip.
    if not detections and not max_width:
        return image_bytes
    import cv2
    import numpy as np
    data = np.frombuffer(image_bytes, dtype=np.uint8)
    try:
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    except cv2.error:
        # cv2 RAISES on an empty buffer and returns None on a malformed one;
        # either way there is no frame to draw on or shrink.
        return image_bytes
    if image is None:
        return image_bytes
    height, width = image.shape[:2]
    if max_width and width > max_width:
        scale = max_width / width
        image = cv2.resize(
            image,
            (int(max_width), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
        height, width = image.shape[:2]
    for detection in detections:
        if detection.get('alert_matched') is False and detection.get('alert_triggered') is False:
            continue
        box = detection.get('box') or {}
        x = int(max(0, min(1, float(box.get('x') or 0))) * width)
        y = int(max(0, min(1, float(box.get('y') or 0))) * height)
        box_width = int(max(0.001, min(1, float(box.get('width') or 0))) * width)
        box_height = int(max(0.001, min(1, float(box.get('height') or 0))) * height)
        x2 = min(width - 1, x + box_width)
        y2 = min(height - 1, y + box_height)
        # Same underscore-strip as the SVG renderer so snapshot JPEG labels
        # match the email/push title case (``Cat_Meow`` -> ``Cat Meow``).
        label = escape(str(detection.get('label') or 'object').replace('_', ' ').title())
        confidence = round(float(detection.get('confidence') or 0) * 100)
        text = f'{label} {confidence}%'
        cv2.rectangle(image, (x, y), (x2, y2), (73, 230, 163), 2)
        text_y = max(22, y - 8)
        (text_width, text_height), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.62, 2)
        cv2.rectangle(image, (x, text_y - text_height - 8), (min(width - 1, x + text_width + 10), text_y + 4), (7, 11, 19), -1)
        cv2.putText(image, text, (x + 5, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (73, 230, 163), 2, cv2.LINE_AA)
    ok, encoded = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return encoded.tobytes() if ok else image_bytes
