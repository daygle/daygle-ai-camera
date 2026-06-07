from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger('daygle.ai')


class RecordingService:
    """Event recording facade with policy selection and generated test footage."""

    VALID_MODES = {'off', 'continuous', 'motion', 'human', 'objects'}
    VALID_SOURCES = {'camera', 'upload', 'rtsp'}
    PLAYBACK_FORMAT = 'mp4'
    GENERIC_TRIGGER_LABELS = {'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous'}

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.recording_config = config.get('recording', {})
        storage_config = config.get('storage', {})
        self.recordings_dir = Path(storage_config.get('recordings_dir') or Path(storage_config.get('data_dir', 'data')) / 'recordings')
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.prebuffer_dir = self.recordings_dir / '.prebuffer'
        self.prebuffer_dir.mkdir(parents=True, exist_ok=True)
        self._prebuffer_lock = threading.Lock()
        self._prebuffer_workers: dict[str, dict[str, Any]] = {}

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

        def preferred_label(candidates: list[dict[str, Any]], *, allow_motion: bool = False) -> str | None:
            sorted_candidates = sorted(candidates, key=lambda detection: float(detection.get('confidence') or 0), reverse=True)
            for candidate in sorted_candidates:
                label = str(candidate.get('label') or '').strip().lower()
                if not label:
                    continue
                if not allow_motion and (label == 'motion' or label in self.GENERIC_TRIGGER_LABELS):
                    continue
                return label
            return None

        if bool(config.get('continuous')) or mode == 'continuous':
            return True, 'continuous', labels[0] if labels else None
        if bool(config.get('record_on_alert', False)):
            alert_detections = [detection for detection in detections if detection.get('alert_triggered') and detection.get('label')]
            alert_labels = [str(detection.get('label') or '').lower() for detection in alert_detections]
            if alert_labels:
                if alert_labels[0] == 'motion':
                    specific_label = preferred_label(alert_detections) or preferred_label(detections)
                    if specific_label:
                        return True, 'alert', specific_label
                return True, 'alert', alert_labels[0]
            return False, 'none', None
        if (mode == 'motion' or bool(config.get('record_on_motion', True))) and labels:
            return True, 'motion', preferred_label(detections) or labels[0]
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

        mapped_source = 'upload' if source in {'test-image', 'upload'} else 'rtsp' if source == 'rtsp' else 'camera'
        if mapped_source not in self.VALID_SOURCES:
            mapped_source = 'camera'
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
            '-fflags',
            '+discardcorrupt',
            '-err_detect',
            'ignore_err',
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
            '-movflags',
            '+faststart',
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

    def stop_prebuffer_workers(self) -> None:
        with self._prebuffer_lock:
            workers = list(self._prebuffer_workers.items())
            self._prebuffer_workers = {}
        for _camera_id, worker in workers:
            stop_event = worker.get('stop_event')
            thread = worker.get('thread')
            if isinstance(stop_event, threading.Event):
                stop_event.set()
            if isinstance(thread, threading.Thread):
                thread.join(timeout=2)

    def prime_rtsp_prebuffer(
        self,
        *,
        stream_url: str,
        camera_id: str,
        recording_config: dict[str, Any] | None = None,
    ) -> bool:
        config = recording_config or self.recording_config
        if not self.enabled_for(config):
            return False
        pre_seconds = max(0, int(config.get('pre_event_seconds', 0)))
        post_seconds = max(0, int(config.get('post_event_seconds', 0)))
        if pre_seconds <= 0:
            return False
        buffer_seconds = max(pre_seconds + post_seconds + 5, pre_seconds + 10, 15)
        camera_key = self._camera_key(camera_id)
        self._ensure_prebuffer_worker(camera_key, stream_url, buffer_seconds)
        return True

    def write_rtsp_clip_with_prebuffer(
        self,
        *,
        stream_url: str,
        camera_id: str,
        file_path: Path,
        triggered_at: datetime,
        pre_seconds: int,
        post_seconds: int,
        max_duration_seconds: float,
    ) -> None:
        pre_seconds = max(0, int(pre_seconds))
        post_seconds = max(0, int(post_seconds))
        max_duration_seconds = max(1.0, float(max_duration_seconds))

        if pre_seconds <= 0:
            self.write_rtsp_clip(stream_url, file_path, max_duration_seconds)
            return

        buffer_seconds = max(pre_seconds + post_seconds + 5, pre_seconds + 10, 15)
        camera_key = self._camera_key(camera_id)
        self._ensure_prebuffer_worker(camera_key, stream_url, buffer_seconds)

        end_capture_at = triggered_at.timestamp() + post_seconds
        delay = end_capture_at - time.time()
        if delay > 0:
            time.sleep(delay)

        start_ts = triggered_at.timestamp() - pre_seconds
        end_ts = end_capture_at
        segments = self._collect_prebuffer_segments(camera_key, start_ts, end_ts)
        if not segments:
            logger.info('No prebuffer segments available for %s; falling back to direct RTSP clip capture.', camera_key)
            self.write_rtsp_clip(stream_url, file_path, max_duration_seconds)
            return

        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.write_rtsp_clip(stream_url, file_path, max_duration_seconds)
            return

        list_path = file_path.with_name(f'{file_path.stem}.concat.txt')
        tmp_path = file_path.with_name(f'{file_path.stem}.prebuffer.tmp{file_path.suffix}')
        list_content = ''.join(f"file '{segment}'\n" for segment in segments)
        list_path.write_text(list_content, encoding='utf-8')
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        command = [
            ffmpeg,
            '-y',
            '-fflags',
            '+discardcorrupt',
            '-err_detect',
            'ignore_err',
            '-f',
            'concat',
            '-safe',
            '0',
            '-i',
            str(list_path),
            '-an',
            '-c:v',
            'libx264',
            '-preset',
            'veryfast',
            '-pix_fmt',
            'yuv420p',
            '-movflags',
            '+faststart',
            '-t',
            f'{max_duration_seconds:.3f}',
            str(tmp_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=max(60, int(max_duration_seconds) + 45), check=False)
        list_path.unlink(missing_ok=True)
        if result.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            tmp_path.unlink(missing_ok=True)
            logger.warning('Failed to render clip from prebuffer for %s; falling back to direct RTSP capture.', camera_key)
            self.write_rtsp_clip(stream_url, file_path, max_duration_seconds)
            return
        tmp_path.replace(file_path)

    @staticmethod
    def _camera_key(camera_id: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_-]+', '-', str(camera_id or '').strip().lower()).strip('-') or 'camera'

    def _ensure_prebuffer_worker(self, camera_key: str, stream_url: str, buffer_seconds: int) -> None:
        with self._prebuffer_lock:
            existing = self._prebuffer_workers.get(camera_key)
            if existing and existing.get('stream_url') == stream_url and existing.get('buffer_seconds') == buffer_seconds:
                thread = existing.get('thread')
                if isinstance(thread, threading.Thread) and thread.is_alive():
                    return
            if existing and isinstance(existing.get('stop_event'), threading.Event):
                existing['stop_event'].set()

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_prebuffer_worker,
                args=(camera_key, stream_url, int(buffer_seconds), stop_event),
                name=f'prebuffer-{camera_key}',
                daemon=True,
            )
            self._prebuffer_workers[camera_key] = {
                'thread': thread,
                'stop_event': stop_event,
                'stream_url': stream_url,
                'buffer_seconds': int(buffer_seconds),
            }
            thread.start()

    def _run_prebuffer_worker(self, camera_key: str, stream_url: str, buffer_seconds: int, stop_event: threading.Event) -> None:
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            logger.warning('ffmpeg is required for rolling prebuffer but is not installed.')
            return
        camera_dir = self.prebuffer_dir / camera_key
        camera_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = camera_dir / 'segment-%Y%m%dT%H%M%S.ts'

        while not stop_event.is_set():
            command = [
                ffmpeg,
                '-nostdin',
                '-hide_banner',
                '-loglevel',
                'error',
                '-rtsp_transport',
                'tcp',
                '-fflags',
                '+discardcorrupt',
                '-err_detect',
                'ignore_err',
                '-i',
                stream_url,
                '-an',
                '-c:v',
                'copy',
                '-f',
                'segment',
                '-segment_time',
                '1',
                '-segment_format',
                'mpegts',
                '-reset_timestamps',
                '1',
                '-strftime',
                '1',
                str(output_pattern),
            ]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                while process.poll() is None and not stop_event.is_set():
                    self._prune_prebuffer_segments(camera_dir, buffer_seconds)
                    time.sleep(1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                self._prune_prebuffer_segments(camera_dir, buffer_seconds)
            if not stop_event.is_set():
                time.sleep(1)

    def _prune_prebuffer_segments(self, camera_dir: Path, keep_seconds: int) -> None:
        cutoff = time.time() - max(keep_seconds, 5)
        for segment in camera_dir.glob('segment-*.ts'):
            try:
                if segment.stat().st_mtime < cutoff:
                    segment.unlink(missing_ok=True)
            except OSError:
                continue

    def _collect_prebuffer_segments(self, camera_key: str, start_ts: float, end_ts: float) -> list[Path]:
        camera_dir = self.prebuffer_dir / camera_key
        if not camera_dir.exists():
            return []
        selected: list[Path] = []
        for segment in sorted(camera_dir.glob('segment-*.ts')):
            try:
                modified = segment.stat().st_mtime
            except OSError:
                continue
            if modified < start_ts - 2:
                continue
            if modified > end_ts + 2:
                continue
            selected.append(segment)
        if selected:
            return selected
        # Fallback to most recent segments covering the requested span.
        segment_list = sorted(camera_dir.glob('segment-*.ts'))
        if not segment_list:
            return []
        window_seconds = max(1, int(end_ts - start_ts))
        return segment_list[-window_seconds:]

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
            # Prefer FFmpeg so fallback clips are consistently H.264/MP4 for browsers.
            self._write_ffmpeg_placeholder_clip(file_path, duration_seconds)
            return
        except Exception as exc:
            logger.warning('FFmpeg placeholder clip generation failed for %s: %s', file_path.name, exc)
        try:
            self._write_opencv_clip(file_path, event_id, detections, duration_seconds, trigger_type, trigger_label)
            return
        except Exception as exc:
            logger.warning('OpenCV clip generation failed for %s: %s', file_path.name, exc)

        # Final fallback: persist metadata beside the target path, but never as .mp4 content.
        file_path.unlink(missing_ok=True)
        metadata_path = file_path.with_name(f'{file_path.name}.meta.json')
        payload = {
            'event_id': event_id,
            'detections': detections,
            'duration_seconds': duration_seconds,
            'trigger_type': trigger_type,
            'trigger_label': trigger_label,
            'note': 'Video encoder unavailable; metadata fallback was written.',
        }
        metadata_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')

    def _write_ffmpeg_placeholder_clip(self, file_path: Path, duration_seconds: float) -> None:
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise RuntimeError('ffmpeg is not installed.')
        tmp_path = file_path.with_name(f'{file_path.stem}.tmp{file_path.suffix}')
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        command = [
            ffmpeg,
            '-y',
            '-f',
            'lavfi',
            '-i',
            'testsrc2=s=640x360:r=10',
            '-t',
            f'{float(max(1.0, duration_seconds)):.3f}',
            '-an',
            '-c:v',
            'libx264',
            '-profile:v',
            'main',
            '-level',
            '4.0',
            '-pix_fmt',
            'yuv420p',
            '-movflags',
            '+faststart',
            str(tmp_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise RuntimeError('ffmpeg failed to generate placeholder clip.')
        tmp_path.replace(file_path)

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
        # Prefer MP4V for generated placeholder clips. Probing H264 codecs on
        # ARM boards can trigger noisy hardware encoder failures (v4l2m2m).
        codec_candidates = ['mp4v'] if suffix == '.mp4' else ['MJPG']
        writer = None
        selected_codec = None
        for codec in codec_candidates:
            fourcc = cv2.VideoWriter_fourcc(*codec)
            candidate = cv2.VideoWriter(str(file_path), fourcc, fps, (width, height))
            if candidate.isOpened():
                writer = candidate
                selected_codec = codec
                break
            candidate.release()
        if writer is None:
            raise RuntimeError(f"Video writer could not open output file with codecs: {', '.join(codec_candidates)}")
        if selected_codec and selected_codec != 'mp4v':
            logger.info('Recording fallback clip %s encoded with %s', file_path.name, selected_codec)
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
