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

    def save_plate_crop(self, *, event_id: int, source_path: str | None, detection: dict[str, Any], index: int) -> str:
        created = datetime.now(timezone.utc)
        filename = f"plate_event_{event_id}_{index}_{created.strftime('%Y%m%d_%H%M%S_%f')}.json"
        path = self.plates_dir / filename
        payload = {
            'created_at': created.isoformat(),
            'event_id': event_id,
            'source_path': source_path,
            'detection': detection,
            'note': 'Plate crop placeholder for uploaded-image workflows. Real crop bytes can replace this in camera backends.',
        }
        path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        return str(path)
