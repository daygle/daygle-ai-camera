from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class RecordingService:
    """Event recording facade with policy selection and generated test footage."""

    VALID_MODES = {'off', 'continuous', 'motion', 'human', 'objects'}
    VALID_SOURCES = {'mock', 'camera', 'upload', 'rtsp'}
    PLAYBACK_FORMAT = 'mp4'

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
        mode = str(self.recording_config.get('mode', 'motion')).lower()
        return mode if mode in self.VALID_MODES else 'motion'

    def enabled_for(self, recording_config: dict[str, Any] | None = None) -> bool:
        config = recording_config or self.recording_config
        mode = self.mode_for(config)
        return bool(config.get('enabled', True)) and mode != 'off'

    def mode_for(self, recording_config: dict[str, Any] | None = None) -> str:
        config = recording_config or self.recording_config
        mode = str(config.get('mode', 'motion')).lower()
        return mode if mode in self.VALID_MODES else 'motion'

    def should_record(self, detections: list[dict[str, Any]], recording_config: dict[str, Any] | None = None) -> tuple[bool, str, str | None]:
        config = recording_config or self.recording_config
        mode = self.mode_for(config)
        if not self.enabled_for(config):
            return False, 'off', None
        labels = [str(detection.get('label') or '').lower() for detection in detections]
        labels = [label for label in labels if label]
        object_labels = {str(label).lower() for label in config.get('record_on_objects', [])}

        if bool(config.get('continuous')) or mode == 'continuous':
            return True, 'continuous', labels[0] if labels else None
        if bool(config.get('record_on_alert', False)):
            alert_labels = [
                str(detection.get('label') or '').lower()
                for detection in detections
                if detection.get('alert_triggered') and detection.get('label')
            ]
            if alert_labels:
                return True, 'alert', alert_labels[0]
            return False, 'none', None
        if (mode == 'motion' or bool(config.get('record_on_motion', True))) and labels:
            return True, 'motion', labels[0]
        if (mode == 'human' or bool(config.get('record_on_human', True))) and 'person' in labels:
            return True, 'human', 'person'
        for label in labels:
            if label in object_labels or (mode == 'objects' and not object_labels):
                return True, 'object', label
        return False, 'none', None

    def event_recording_metadata(
        self,
        event_id: int,
        event_time: str,
        source: str,
        detections: list[dict[str, Any]],
        *,
        write_clip: bool = True,
        recording_config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        should_record, trigger_type, trigger_label = self.should_record(detections, recording_config)
        if not should_record:
            return None
        active_config = recording_config or self.recording_config

        try:
            created = datetime.fromisoformat(event_time)
        except ValueError:
            created = datetime.now(timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        pre_seconds = max(0, int(active_config.get('pre_event_seconds', 5)))
        post_seconds = max(0, int(active_config.get('post_event_seconds', 10)))
        max_clip_seconds = max(1, int(active_config.get('max_clip_seconds', 60)))
        duration_seconds = min(max_clip_seconds, max(1, pre_seconds + post_seconds))
        started_at = created - timedelta(seconds=min(pre_seconds, duration_seconds))
        ended_at = started_at + timedelta(seconds=duration_seconds)
        extension = self.recording_format()
        filename = f"event_{event_id}_{created.strftime('%Y%m%d_%H%M%S_%f')}.{extension}"
        file_path = self.recordings_dir / filename
        if write_clip:
            self.write_event_clip(file_path, event_id, detections, duration_seconds, trigger_type, trigger_label)

        mapped_source = 'upload' if source in {'test-image', 'upload'} else 'rtsp' if source == 'rtsp' else 'mock' if source.startswith('mock') else 'camera'
        if mapped_source not in self.VALID_SOURCES:
            mapped_source = 'mock'
        return {
            'event_id': event_id,
            'camera_id': None,
            'started_at': started_at.isoformat(),
            'ended_at': ended_at.isoformat(),
            'duration_seconds': duration_seconds,
            'file_path': str(file_path),
            'thumbnail_path': None,
            'source': mapped_source,
            'trigger_type': trigger_type,
            'trigger_label': trigger_label,
        }

    def write_rtsp_clip(self, stream_url: str, file_path: Path, duration_seconds: float) -> None:
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise RuntimeError('ffmpeg is required to record RTSP clips.')
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_name(f'{file_path.stem}.recording.tmp{file_path.suffix}')
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        command = [
            ffmpeg,
            '-y',
            '-rtsp_transport',
            'tcp',
            '-i',
            stream_url,
            '-t',
            f'{float(duration_seconds):.3f}',
            '-an',
            '-c:v',
            'libx264',
            '-preset',
            'veryfast',
            '-pix_fmt',
            'yuv420p',
            str(tmp_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=max(30, int(duration_seconds) + 20), check=False)
        if result.returncode != 0:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            error_detail = self.redact_stream_credentials(f'{result.stderr[:500]}\n...\n{result.stderr[-1000:]}')
            raise RuntimeError(f'ffmpeg failed to record RTSP clip: {error_detail}')
        if not tmp_path.exists():
            raise RuntimeError('ffmpeg did not create an RTSP recording file.')
        tmp_path.replace(file_path)

    @staticmethod
    def redact_stream_credentials(message: str) -> str:
        return re.sub(r'(rtsps?://[^:\s/@]+):[^@\s/]+@', r'\1:***@', message)

    def recording_format(self) -> str:
        configured = str(self.recording_config.get('format', self.PLAYBACK_FORMAT)).strip().lstrip('.').lower()
        return self.PLAYBACK_FORMAT if configured in {'', 'avi'} else configured

    def write_event_clip(
        self,
        file_path: Path,
        event_id: int,
        detections: list[dict[str, Any]],
        duration_seconds: float,
        trigger_type: str,
        trigger_label: str | None,
    ) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._write_opencv_clip(file_path, event_id, detections, duration_seconds, trigger_type, trigger_label)
        except Exception:
            payload = {
                'event_id': event_id,
                'detections': detections,
                'duration_seconds': duration_seconds,
                'trigger_type': trigger_type,
                'trigger_label': trigger_label,
                'note': 'Video encoder unavailable; metadata fallback was written.',
            }
            file_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')

    def _write_opencv_clip(
        self,
        file_path: Path,
        event_id: int,
        detections: list[dict[str, Any]],
        duration_seconds: float,
        trigger_type: str,
        trigger_label: str | None,
    ) -> None:
        import cv2  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]

        width = 640
        height = 360
        fps = 10
        frame_count = max(10, min(120, int(duration_seconds * fps)))
        suffix = file_path.suffix.lower()
        fourcc = cv2.VideoWriter_fourcc(*('mp4v' if suffix == '.mp4' else 'MJPG'))
        writer = cv2.VideoWriter(str(file_path), fourcc, fps, (width, height))
        if not writer.isOpened():
            raise RuntimeError('Video writer could not open output file.')
        try:
            labels = ', '.join(str(detection.get('label')) for detection in detections) or 'continuous'
            for index in range(frame_count):
                frame = np.zeros((height, width, 3), dtype=np.uint8)
                frame[:, :] = (22, 30, 44)
                sweep = int((index / max(1, frame_count - 1)) * width)
                cv2.rectangle(frame, (0, 0), (sweep, height), (32, 80, 96), -1)
                cv2.putText(frame, 'Daygle AI Camera', (28, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (237, 243, 255), 2)
                cv2.putText(frame, f'Event #{event_id}', (28, 98), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (73, 230, 163), 2)
                cv2.putText(frame, f'Trigger: {trigger_type} {trigger_label or ""}'.strip(), (28, 136), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (71, 214, 255), 2)
                cv2.putText(frame, f'Detections: {labels}', (28, 174), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (210, 220, 235), 2)
                for detection in detections:
                    box = detection.get('box', {})
                    x = int(float(box.get('x', 0.12)) * width)
                    y = int(float(box.get('y', 0.2)) * height)
                    w = int(float(box.get('width', 0.28)) * width)
                    h = int(float(box.get('height', 0.28)) * height)
                    cv2.rectangle(frame, (x, y), (min(width - 1, x + w), min(height - 1, y + h)), (73, 230, 163), 2)
                    cv2.putText(frame, str(detection.get('label', 'object')), (x, max(20, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (73, 230, 163), 1)
                writer.write(frame)
        finally:
            writer.release()
