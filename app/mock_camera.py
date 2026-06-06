from __future__ import annotations

import time
from typing import Any


class MockCamera:
    def __init__(self, width: int = 1280, height: int = 720, fps: int = 15) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_number = 0
        self.started_at = time.time()

    def get_frame(self) -> dict[str, Any]:
        self.frame_number += 1
        return {
            'frame_number': self.frame_number,
            'timestamp': time.time(),
            'width': self.width,
            'height': self.height,
            'uptime_seconds': round(time.time() - self.started_at, 1)
        }

    def snapshot(self) -> dict[str, Any]:
        frame = self.get_frame()
        frame['snapshot'] = True
        return frame


class OpenCvStreamCamera:
    """Camera backend for RTSP/ONVIF-compatible streams read through OpenCV.

    Many ONVIF cameras, including P6S-style IP cameras, expose the actual video
    as an RTSP URL. This backend stores that stream URL and uses OpenCV/FFmpeg
    to pull snapshots for the live view.
    """

    def __init__(self, stream_url: str, width: int = 1280, height: int = 720, fps: int = 15) -> None:
        self.stream_url = stream_url
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_number = 0
        self.started_at = time.time()
        self.last_error: str | None = None

    @property
    def backend(self) -> str:
        return 'onvif'

    def get_frame(self) -> dict[str, Any]:
        return {
            'frame_number': self.frame_number,
            'timestamp': time.time(),
            'width': self.width,
            'height': self.height,
            'uptime_seconds': round(time.time() - self.started_at, 1),
            'stream_url': self.stream_url,
            'last_error': self.last_error,
        }

    def read_jpeg(self) -> tuple[bytes, dict[str, Any]]:
        if not self.stream_url:
            raise RuntimeError('ONVIF/RTSP stream URL is not configured.')

        import cv2

        capture = cv2.VideoCapture(self.stream_url)
        try:
            if not capture.isOpened():
                self.last_error = 'Unable to open ONVIF/RTSP stream.'
                raise RuntimeError(self.last_error)
            ok, image = capture.read()
        finally:
            capture.release()

        if not ok or image is None:
            self.last_error = 'Unable to read a frame from the ONVIF/RTSP stream.'
            raise RuntimeError(self.last_error)

        height, width = image.shape[:2]
        self.width = int(width)
        self.height = int(height)
        self.frame_number += 1
        ok, encoded = cv2.imencode('.jpg', image)
        if not ok:
            self.last_error = 'Unable to encode ONVIF/RTSP frame as JPEG.'
            raise RuntimeError(self.last_error)
        self.last_error = None
        return encoded.tobytes(), self.get_frame()

    def snapshot(self) -> dict[str, Any]:
        _jpeg, frame = self.read_jpeg()
        frame['snapshot'] = True
        return frame
