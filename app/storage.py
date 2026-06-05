from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, config: dict[str, Any]) -> None:
        storage_config = config.get('storage', {})
        self.data_dir = Path(storage_config.get('data_dir', 'data'))
        self.snapshots_dir = Path(storage_config.get('snapshots_dir', 'data/snapshots'))
        self.events_dir = Path(storage_config.get('events_dir', 'data/events'))
        self.recordings_dir = Path(storage_config.get('recordings_dir', self.data_dir / 'recordings'))
        self.ensure_directories()

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    def save_mock_snapshot(self, frame: dict[str, Any], detections: list[dict[str, Any]]) -> str:
        created = datetime.now(timezone.utc)
        filename = created.strftime('%Y%m%d_%H%M%S_%f') + '.json'
        path = self.snapshots_dir / filename
        payload = {
            'created_at': created.isoformat(),
            'frame': frame,
            'detections': detections,
            'note': 'Mock snapshot metadata. Real image snapshots will be added with the camera backend.'
        }
        path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        return str(path)

    def save_image_snapshot(self, image_bytes: bytes, original_filename: str | None = None) -> str:
        created = datetime.now(timezone.utc)
        suffix = Path(original_filename or '').suffix.lower()
        if suffix not in {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}:
            suffix = '.jpg'
        filename = created.strftime('%Y%m%d_%H%M%S_%f') + suffix
        path = self.snapshots_dir / filename
        path.write_bytes(image_bytes)
        return str(path)
