from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Detection:
    label: str
    confidence: float
    box: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "box": self.box,
        }


class MockDetector:
    """A deterministic-enough fake detector for dashboard and alert testing."""

    def __init__(self, categories: list[str], min_confidence: float = 0.45) -> None:
        self.categories = categories or ["person", "cat", "dog", "car", "package"]
        self.min_confidence = min_confidence
        self._last_detection_at = 0.0

    def detect(self, frame_number: int) -> list[dict[str, Any]]:
        now = time.time()

        # Do not create detections on every frame; this keeps the event stream readable.
        if now - self._last_detection_at < 3:
            return []

        if random.random() > 0.55:
            return []

        self._last_detection_at = now
        count = random.randint(1, 3)
        detections: list[Detection] = []

        for _ in range(count):
            label = random.choice(self.categories)
            confidence = random.uniform(max(self.min_confidence, 0.45), 0.98)
            x = random.uniform(0.05, 0.65)
            y = random.uniform(0.08, 0.65)
            width = random.uniform(0.12, 0.30)
            height = random.uniform(0.12, 0.35)
            detections.append(
                Detection(
                    label=label,
                    confidence=confidence,
                    box={
                        "x": round(x, 3),
                        "y": round(y, 3),
                        "width": round(width, 3),
                        "height": round(height, 3),
                    },
                )
            )

        return [detection.to_dict() for detection in detections]
