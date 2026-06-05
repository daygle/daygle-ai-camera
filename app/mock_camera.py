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
