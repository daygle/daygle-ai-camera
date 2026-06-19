"""Camera-instance creation helpers extracted from ``app/main.py`` (Phase-F).

Owns three helpers for creating OpenCV/RTSP camera instances and reading
ingest frames from the recording service.

* ``create_camera(settings)`` — instantiate one ``OpenCvStreamCamera``
* ``create_camera_instances(settings_list)`` — build the full id→camera dict
* ``read_ingest_frame(camera_id)`` — pull the latest JPEG from the ingest
  prebuffer and decode it to ``(bgr_image, frame_dict)``
"""

from __future__ import annotations

import logging
from typing import Any

import app.state as _state

logger = logging.getLogger('daygle.ai')


def read_ingest_frame(camera_id: str) -> tuple[Any, dict[str, Any]] | None:
    """Decode the latest frame the shared per-camera ingest wrote.

    Returns ``(bgr_image, frame_dict)`` or ``None`` when no fresh frame is
    available yet (ingest warming up or camera offline).
    """
    sample = _state.recording_service.latest_frame_jpeg(camera_id)
    if sample is None:
        return None
    jpeg_bytes, captured_ts = sample
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    frame = {
        'frame_number': 0,
        'timestamp': captured_ts,
        'width': int(width),
        'height': int(height),
    }
    return (image, frame)


def create_camera(settings: dict[str, Any]) -> Any:
    from app.main import OpenCvStreamCamera, build_stream_url
    width = int(settings.get('width', 1280))
    height = int(settings.get('height', 720))
    fps = int(settings.get('fps', 15))
    stale = settings.get('stale_frame_grabs')
    return OpenCvStreamCamera(build_stream_url(settings), width=width, height=height, fps=fps, stale_frame_grabs=stale)


def create_camera_instances(settings_list: list[dict[str, Any]]) -> dict[str, Any]:
    return {str(settings['id']): create_camera(settings) for settings in settings_list}
