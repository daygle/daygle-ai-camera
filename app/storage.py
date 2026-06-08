from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Storage:
    def __init__(self, config: dict[str, Any]) -> None:
        storage_config = config.get('storage', {})
        self.data_dir = Path(storage_config.get('data_dir', 'data'))
        self.snapshots_dir = Path(storage_config.get('snapshots_dir', 'data/snapshots'))
        self.events_dir = Path(storage_config.get('events_dir', 'data/events'))
        self.recordings_dir = Path(storage_config.get('recordings_dir', self.data_dir / 'recordings'))
        self.plates_dir = Path(storage_config.get('plates_dir', self.data_dir / 'plates'))
        self.ensure_directories()

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.plates_dir.mkdir(parents=True, exist_ok=True)

    def save_image_snapshot(self, image_bytes: bytes, original_filename: str | None = None) -> str:
        created = datetime.now(timezone.utc)
        suffix = Path(original_filename or '').suffix.lower()
        if suffix not in {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}:
            suffix = '.jpg'
        filename = created.strftime('%Y%m%d_%H%M%S_%f') + suffix
        path = self.snapshots_dir / filename
        path.write_bytes(image_bytes)
        return str(path)

    def save_plate_crop(self, *, event_id: int, source_path: str | None, detection: dict[str, Any], index: int) -> str | None:
        created = datetime.now(timezone.utc)
        stem = f"plate_event_{event_id}_{index}_{created.strftime('%Y%m%d_%H%M%S_%f')}"
        if source_path and Path(source_path).is_file():
            try:
                import cv2  # type: ignore[import-untyped]
                img = cv2.imread(source_path)
                if img is not None:
                    h, w = img.shape[:2]
                    box = detection.get('box') or {}
                    bx = float(box.get('x') or 0)
                    by = float(box.get('y') or 0)
                    bw = float(box.get('width') or 0)
                    bh = float(box.get('height') or 0)
                    x1 = max(0, int(bx * w))
                    y1 = max(0, int(by * h))
                    x2 = min(w, int((bx + bw) * w))
                    y2 = min(h, int((by + bh) * h))
                    if x2 > x1 and y2 > y1:
                        crop_path = self.plates_dir / f"{stem}.jpg"
                        cv2.imwrite(str(crop_path), img[y1:y2, x1:x2])
                        return str(crop_path)
            except Exception as exc:
                logger.warning("Failed to crop plate image for event %s detection %s: %s", event_id, index, exc)
            # Fall back to the full source image so OCR can still attempt extraction
            return source_path
        return None
