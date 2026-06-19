"""Live-snapshot rendering helpers extracted from ``app/main.py`` (Phase-25).

Both functions are pure: they take a frame dict plus detections (and, for the
SVG renderer, an optional list of zone dicts) and return either string SVG
markup (``render_live_snapshot_svg``) or annotated JPEG bytes
(``render_live_snapshot_jpeg_overlay``). Neither depends on any
module-level state in ``app.main`` - they only need ``datetime``,
``html.escape``, ``rectangle_zone_points`` from :mod:`app.zone_schema`, and
inline ``cv2`` / ``numpy`` imports for the JPEG path.

The module was carved out as the lowest-risk first move in main.py's
ongoing extraction work (the project already splits
``camera_config`` / ``config_facades`` / ``ai_settings`` /
``payload_validators`` / ``zone_schema`` / ``zone_detection`` /
``camera_health`` modules). main.py keeps a Phase-25 Pool-A rebind so
``main.render_live_snapshot_svg`` / ``main.render_live_snapshot_jpeg_overlay``
still resolve, which is what ``tests/test_api.py`` and the call site in
``deliver_email_alerts`` (L1509) need.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any

from app.zone_schema import rectangle_zone_points


def render_live_snapshot_svg(frame: dict[str, Any], detections: list[dict[str, Any]], *, overlay: bool, camera_name: str='Camera', zones: list[dict[str, Any]] | None=None) -> str:
    width = int(frame.get('width') or 1280)
    height = int(frame.get('height') or 720)
    frame_number = int(frame.get('frame_number') or 0)
    timestamp = datetime.fromtimestamp(float(frame.get('timestamp') or 0), timezone.utc).strftime('%H:%M:%S UTC')
    grid_spacing = 80
    grid_lines = []
    for x in range(0, width + grid_spacing, grid_spacing):
        grid_lines.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{height}" />')
    for y in range(0, height + grid_spacing, grid_spacing):
        grid_lines.append(f'<line x1="0" y1="{y}" x2="{width}" y2="{y}" />')
    zone_markup: list[str] = []
    if overlay:
        for zone in zones or []:
            if not zone.get('enabled', True):
                continue
            points = zone.get('points') or rectangle_zone_points(max(0.0, min(1.0, float(zone.get('x') or 0))), max(0.0, min(1.0, float(zone.get('y') or 0))), max(0.01, min(1.0, float(zone.get('width') or 0))), max(0.01, min(1.0, float(zone.get('height') or 0))))
            svg_points = []
            for point in points:
                if not isinstance(point, dict):
                    continue
                svg_points.append(f"{max(0, float(point.get('x') or 0) * width):.1f},{max(0, float(point.get('y') or 0) * height):.1f}")
            if len(svg_points) < 3:
                continue
            label_x = max(0, float(points[0].get('x') or 0) * width) + 12
            label_y = max(30, float(points[0].get('y') or 0) * height + 30)
            zone_name = escape(str(zone.get('name') or 'Monitoring area'))
            zone_markup.append(f'''<g class="monitor-zone"><polygon points="{' '.join(svg_points)}" /><text x="{label_x:.1f}" y="{label_y:.1f}">{zone_name}</text></g>''')
    detection_markup: list[str] = []
    if overlay:
        for detection in detections:
            box = detection.get('box') or {}
            x = max(0, float(box.get('x') or 0) * width)
            y = max(0, float(box.get('y') or 0) * height)
            box_width = max(1, float(box.get('width') or 0) * width)
            box_height = max(1, float(box.get('height') or 0) * height)
            label = escape(str(detection.get('label') or 'object'))
            confidence = round(float(detection.get('confidence') or 0) * 100)
            label_y = max(28, y - 10)
            detection_markup.append(f'<g class="detection-box"><rect x="{x:.1f}" y="{y:.1f}" width="{box_width:.1f}" height="{box_height:.1f}" /><text x="{x:.1f}" y="{label_y:.1f}">{label} · {confidence}%</text></g>')
    overlay_state = 'ON' if overlay else 'OFF'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n  <defs>\n    <linearGradient id="camera-bg" x1="0" x2="1" y1="0" y2="1">\n      <stop offset="0" stop-color="#101827" />\n      <stop offset="0.52" stop-color="#0b1220" />\n      <stop offset="1" stop-color="#17223a" />\n    </linearGradient>\n    <radialGradient id="lens" cx="50%" cy="45%" r="68%">\n      <stop offset="0" stop-color="#47d6ff" stop-opacity="0.22" />\n      <stop offset="0.5" stop-color="#8b5cf6" stop-opacity="0.1" />\n      <stop offset="1" stop-color="#070b13" stop-opacity="0" />\n    </radialGradient>\n    <style>\n      .grid line {{ stroke: rgba(255,255,255,.08); stroke-width: 1; }}\n      .hud {{ fill: #edf3ff; font: 700 26px Inter, Arial, sans-serif; letter-spacing: .04em; }}\n      .muted {{ fill: #91a1ba; font: 700 20px Inter, Arial, sans-serif; }}\n      .monitor-zone polygon {{ fill: rgba(71,214,255,.08); stroke: #47d6ff; stroke-width: 3; stroke-dasharray: 12 10; }}\n      .monitor-zone text {{ fill: #47d6ff; font: 800 20px Inter, Arial, sans-serif; paint-order: stroke; stroke: rgba(7,11,19,.86); stroke-width: 4; stroke-linejoin: round; }}\n      .detection-box rect {{ fill: rgba(73,230,163,.08); stroke: #49e6a3; stroke-width: 4; rx: 18; }}\n      .detection-box text {{ fill: #49e6a3; font: 800 24px Inter, Arial, sans-serif; paint-order: stroke; stroke: rgba(7,11,19,.86); stroke-width: 5; stroke-linejoin: round; }}\n    </style>\n  </defs>\n  <rect width="100%" height="100%" fill="url(#camera-bg)" />\n  <rect width="100%" height="100%" fill="url(#lens)" />\n  <g class="grid">{''.join(grid_lines)}</g>\n  <circle cx="{width * 0.74:.1f}" cy="{height * 0.34:.1f}" r="{min(width, height) * 0.16:.1f}" fill="none" stroke="rgba(71,214,255,.16)" stroke-width="3" />\n  <circle cx="{width * 0.28:.1f}" cy="{height * 0.62:.1f}" r="{min(width, height) * 0.12:.1f}" fill="none" stroke="rgba(139,92,246,.16)" stroke-width="3" />\n  {''.join(zone_markup)}\n  {''.join(detection_markup)}\n  <rect x="24" y="24" width="520" height="116" rx="20" fill="rgba(7,11,19,.58)" stroke="rgba(255,255,255,.12)" />\n  <text x="48" y="70" class="hud">{escape(camera_name).upper()}</text>\n  <text x="48" y="112" class="muted">Frame #{frame_number} · {timestamp} · Overlay {overlay_state}</text>\n</svg>'''


def render_live_snapshot_jpeg_overlay(image_bytes: bytes, detections: list[dict[str, Any]]) -> bytes:
    if not detections:
        return image_bytes
    try:
        import cv2
        import numpy as np
    except ImportError:
        return image_bytes
    data = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        return image_bytes
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
        label = str(detection.get('label') or 'object')
        confidence = round(float(detection.get('confidence') or 0) * 100)
        text = f'{label} {confidence}%'
        cv2.rectangle(image, (x, y), (x2, y2), (73, 230, 163), 2)
        text_y = max(22, y - 8)
        (text_width, text_height), _baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.62, 2)
        cv2.rectangle(image, (x, text_y - text_height - 8), (min(width - 1, x + text_width + 10), text_y + 4), (7, 11, 19), -1)
        cv2.putText(image, text, (x + 5, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (73, 230, 163), 2, cv2.LINE_AA)
    ok, encoded = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return encoded.tobytes() if ok else image_bytes
