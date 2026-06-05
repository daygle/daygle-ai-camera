from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class RecordingService:
    """Small event recording facade, ready for frame-writing backends later."""

    VALID_MODES = {'off', 'event', 'continuous'}
    VALID_SOURCES = {'mock', 'camera', 'upload', 'rtsp'}

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.recording_config = config.get('recording', {})
        storage_config = config.get('storage', {})
        self.recordings_dir = Path(storage_config.get('recordings_dir') or Path(storage_config.get('data_dir', 'data')) / 'recordings')
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return bool(self.recording_config.get('enabled', True)) and self.mode != 'off'

    @property
    def mode(self) -> str:
        mode = str(self.recording_config.get('mode', 'event')).lower()
        return mode if mode in self.VALID_MODES else 'event'

    def event_recording_enabled(self) -> bool:
        return self.enabled and self.mode == 'event'

    def mock_event_metadata(self, event_id: int, event_time: str, source: str) -> dict[str, Any] | None:
        """Return placeholder metadata for event clips without requiring video tools.

        Future camera, RTSP, and upload backends can replace this with a writer that
        persists encoded frames and passes the resulting media path to the database.
        """
        if not self.event_recording_enabled():
            return None

        try:
            created = datetime.fromisoformat(event_time)
        except ValueError:
            created = datetime.now(timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        pre_seconds = max(0, int(self.recording_config.get('pre_event_seconds', 5)))
        post_seconds = max(0, int(self.recording_config.get('post_event_seconds', 10)))
        max_clip_seconds = max(1, int(self.recording_config.get('max_clip_seconds', 60)))
        duration_seconds = min(max_clip_seconds, pre_seconds + post_seconds)
        started_at = created - timedelta(seconds=min(pre_seconds, duration_seconds))
        ended_at = started_at + timedelta(seconds=duration_seconds)
        extension = str(self.recording_config.get('format', 'mp4')).lstrip('.') or 'mp4'
        filename = f"event_{event_id}_{created.strftime('%Y%m%d_%H%M%S_%f')}.{extension}"
        mapped_source = 'upload' if source in {'test-image', 'upload'} else 'mock' if source.startswith('mock') else 'camera'
        if mapped_source not in self.VALID_SOURCES:
            mapped_source = 'mock'
        return {
            'event_id': event_id,
            'camera_id': None,
            'started_at': started_at.isoformat(),
            'ended_at': ended_at.isoformat(),
            'duration_seconds': duration_seconds,
            'file_path': str(self.recordings_dir / filename),
            'thumbnail_path': None,
            'source': mapped_source,
        }
