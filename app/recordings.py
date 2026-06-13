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
from typing import Any, Callable


logger = logging.getLogger('daygle.ai')


class RecordingService:
    """Event recording facade with policy selection and generated test footage."""

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
        self._continuous_lock = threading.Lock()
        self._continuous_workers: dict[str, dict[str, Any]] = {}

    def should_record(self, detections: list[dict[str, Any]], recording_config: dict[str, Any] | None = None) -> tuple[bool, str, str | None]:
        config = recording_config or self.recording_config
        labels = [str(detection.get('label') or '').lower() for detection in detections]
        labels = [label for label in labels if label]

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

        if bool(config.get('continuous')):
            return True, 'continuous', labels[0] if labels else None
        # Non-continuous recording is gated per detection: a detection records
        # only when its zone/sound rule marked it alert_triggered (the rule's
        # record_on_detect flag). Detections without a matching record rule
        # must not start a recording.
        alert_detections = [detection for detection in detections if detection.get('alert_triggered') and detection.get('label')]
        alert_labels = [str(detection.get('label') or '').lower() for detection in alert_detections]
        if alert_labels:
            if alert_labels[0] == 'motion':
                specific_label = preferred_label(detections)
                return True, 'alert', specific_label or 'motion'
            return True, 'alert', alert_labels[0]
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

        mapped_source = 'upload' if source == 'upload' else 'rtsp' if source == 'rtsp' else 'camera'
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
        # stream_url may contain credentials (rtsp://user:pass@host/...). ffmpeg
        # requires them inline in the -i URL; there is no other mechanism for RTSP
        # auth. The command array is intentionally never logged for this reason.
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
            '-c:v',
            'libx264',
            '-c:a',
            'aac',
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
        if not self.clip_has_video_stream(tmp_path):
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError('Recorded RTSP clip contains no decodable video stream.')
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

    def start_continuous_chunk_recording(
        self,
        *,
        stream_url: str,
        camera_id: str,
        recording_config: dict[str, Any] | None = None,
        on_chunk_complete: Callable[[str, Path], None] | None = None,
    ) -> bool:
        config = recording_config or self.recording_config
        chunk_seconds = max(60, int(config.get('chunk_duration_seconds', 3600)))
        camera_key = self._camera_key(camera_id)
        chunks_dir = self.recordings_dir / f'continuous-{camera_key}'
        chunks_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_continuous_chunk_worker(camera_key, stream_url, chunks_dir, chunk_seconds, on_chunk_complete)
        return True

    def stop_continuous_chunk_recording(self, camera_id: str) -> None:
        camera_key = self._camera_key(camera_id)
        with self._continuous_lock:
            worker = self._continuous_workers.pop(camera_key, None)
        if not worker:
            return
        stop_event = worker.get('stop_event')
        thread = worker.get('thread')
        if isinstance(stop_event, threading.Event):
            stop_event.set()
        if isinstance(thread, threading.Thread):
            thread.join(timeout=5)

    def stop_all_continuous_recordings(self) -> None:
        with self._continuous_lock:
            workers = list(self._continuous_workers.items())
            self._continuous_workers = {}
        for _camera_key, worker in workers:
            stop_event = worker.get('stop_event')
            thread = worker.get('thread')
            if isinstance(stop_event, threading.Event):
                stop_event.set()
            if isinstance(thread, threading.Thread):
                thread.join(timeout=3)

    def _ensure_continuous_chunk_worker(
        self,
        camera_key: str,
        stream_url: str,
        chunks_dir: Path,
        chunk_seconds: int,
        on_chunk_complete: Callable[[str, Path], None] | None,
    ) -> None:
        with self._continuous_lock:
            existing = self._continuous_workers.get(camera_key)
            if existing and existing.get('stream_url') == stream_url and existing.get('chunk_seconds') == chunk_seconds:
                thread = existing.get('thread')
                if isinstance(thread, threading.Thread) and thread.is_alive():
                    return
            if existing and isinstance(existing.get('stop_event'), threading.Event):
                existing['stop_event'].set()

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_continuous_chunk_worker,
                args=(camera_key, stream_url, chunks_dir, chunk_seconds, on_chunk_complete, stop_event),
                name=f'continuous-recorder-{camera_key}',
                daemon=True,
            )
            self._continuous_workers[camera_key] = {
                'thread': thread,
                'stop_event': stop_event,
                'stream_url': stream_url,
                'chunk_seconds': chunk_seconds,
            }
            thread.start()

    def _run_continuous_chunk_worker(
        self,
        camera_key: str,
        stream_url: str,
        chunks_dir: Path,
        chunk_seconds: int,
        on_chunk_complete: Callable[[str, Path], None] | None,
        stop_event: threading.Event,
    ) -> None:
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            logger.warning('ffmpeg is required for continuous chunk recording of %s but is not installed.', camera_key)
            return
        chunks_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = chunks_dir / f'continuous_{camera_key}_%Y%m%dT%H%M%S.mp4'
        list_file = chunks_dir / '.segment_list.txt'

        while not stop_event.is_set():
            list_file.unlink(missing_ok=True)
            command = [
                ffmpeg,
                '-nostdin',
                '-hide_banner',
                '-loglevel', 'error',
                '-rtsp_transport', 'tcp',
                '-fflags', '+discardcorrupt',
                '-err_detect', 'ignore_err',
                '-i', stream_url,
                '-c:v', 'copy',
                '-c:a', 'copy',
                '-f', 'segment',
                '-segment_time', str(chunk_seconds),
                '-segment_format', 'mp4',
                '-reset_timestamps', '1',
                '-strftime', '1',
                '-segment_list', str(list_file),
                '-segment_list_type', 'flat',
                str(output_pattern),
            ]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            seen_count = 0
            try:
                while process.poll() is None and not stop_event.is_set():
                    if list_file.exists():
                        try:
                            lines = list_file.read_text(encoding='utf-8').splitlines()
                        except OSError:
                            lines = []
                        for line in lines[seen_count:]:
                            segment_name = line.strip()
                            if not segment_name:
                                continue
                            segment_path = chunks_dir / segment_name
                            try:
                                if segment_path.exists() and segment_path.stat().st_size > 0 and on_chunk_complete:
                                    on_chunk_complete(camera_key, segment_path)
                            except Exception as exc:
                                logger.warning('Continuous chunk callback failed for %s/%s: %s', camera_key, segment_name, exc)
                        seen_count = len(lines)
                    time.sleep(1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
            if not stop_event.is_set():
                logger.info('Continuous recorder for %s restarting after ffmpeg exit.', camera_key)
                time.sleep(2)

    def prebuffer_window_seconds(self, recording_config: dict[str, Any] | None = None) -> int:
        """Rolling prebuffer span. Must cover the longest possible event clip
        (pre + max_clip ceiling), not just pre+post: extended captures render the
        clip from these segments, and an undersized buffer would silently drop the
        start of the event."""
        config = recording_config or self.recording_config
        pre_seconds = max(0, int(config.get('pre_event_seconds', 0)))
        max_clip_seconds = max(1, int(config.get('max_clip_seconds', 60)))
        return max(pre_seconds + max_clip_seconds + 5, pre_seconds + 10, 15)

    def prime_rtsp_prebuffer(
        self,
        *,
        stream_url: str,
        camera_id: str,
        recording_config: dict[str, Any] | None = None,
    ) -> bool:
        config = recording_config or self.recording_config
        pre_seconds = max(0, int(config.get('pre_event_seconds', 0)))
        if pre_seconds <= 0:
            return False
        camera_key = self._camera_key(camera_id)
        self._ensure_prebuffer_worker(camera_key, stream_url, self.prebuffer_window_seconds(config))
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
        buffer_seconds: int | None = None,
    ) -> tuple[float, float]:
        """Write the clip and return ``(content_start_ts, content_seconds)``:
        the wall-clock timestamp where the written media actually begins and
        its duration. Prebuffer segments split on keyframes, so the rendered
        clip rarely starts exactly at ``triggered_at - pre_seconds``; callers
        must anchor stored timing and the detection track to the returned
        window or playback overlays drift against the video."""
        pre_seconds = max(0, int(pre_seconds))
        post_seconds = max(0, int(post_seconds))
        max_duration_seconds = max(1.0, float(max_duration_seconds))

        if pre_seconds <= 0:
            content_start_ts = time.time()
            self.write_rtsp_clip(stream_url, file_path, max_duration_seconds)
            return content_start_ts, max_duration_seconds

        # Use the same window the priming path computed, so re-ensuring the worker
        # here never restarts it mid-capture over a mismatched buffer size.
        if buffer_seconds is None:
            buffer_seconds = self.prebuffer_window_seconds()
        buffer_seconds = max(int(buffer_seconds), pre_seconds + post_seconds + 5, pre_seconds + 10, 15)
        camera_key = self._camera_key(camera_id)
        self._ensure_prebuffer_worker(camera_key, stream_url, buffer_seconds)

        end_capture_at = triggered_at.timestamp() + post_seconds
        delay = end_capture_at - time.time()
        if delay > 0:
            time.sleep(delay)

        start_ts = triggered_at.timestamp() - pre_seconds
        end_ts = end_capture_at
        segments, content_start_ts = self._collect_prebuffer_segments(camera_key, start_ts, end_ts)
        if not segments:
            logger.info('No prebuffer segments available for %s; falling back to direct RTSP clip capture.', camera_key)
            fallback_start_ts = time.time()
            self.write_rtsp_clip(stream_url, file_path, max_duration_seconds)
            return fallback_start_ts, max_duration_seconds

        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            fallback_start_ts = time.time()
            self.write_rtsp_clip(stream_url, file_path, max_duration_seconds)
            return fallback_start_ts, max_duration_seconds

        if content_start_ts is None:
            content_start_ts = start_ts
        # Render exactly the footage between where the first selected segment
        # starts and the capture deadline. The keyframe-aligned lead before
        # start_ts is kept (and reported via content_start_ts) rather than
        # silently eating the same amount off the end of the clip.
        content_seconds = max(1.0, min(end_ts - content_start_ts, max_duration_seconds + 10.0))

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
            '-c:v',
            'libx264',
            '-c:a',
            'aac',
            '-preset',
            'veryfast',
            '-pix_fmt',
            'yuv420p',
            '-movflags',
            '+faststart',
            '-t',
            f'{content_seconds:.3f}',
            str(tmp_path),
        ]
        try:
            # Re-encoding an extended clip (up to max_clip_seconds) can run slower
            # than realtime on low-power boards; an undersized timeout here would
            # discard the captured event and fall through to a too-late re-capture.
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(120, int(content_seconds) * 3 + 60),
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = None
        finally:
            list_path.unlink(missing_ok=True)
        # clip_has_video_stream (ffprobe) is checked last so it only runs once the
        # cheap return-code/size checks have passed — i.e. when ffmpeg claims success
        # but may have discarded all corrupt frames into a videoless output.
        if (
            result is None
            or result.returncode != 0
            or not tmp_path.exists()
            or tmp_path.stat().st_size <= 0
            or not self.clip_has_video_stream(tmp_path)
        ):
            tmp_path.unlink(missing_ok=True)
            logger.warning('Failed to render clip from prebuffer for %s; falling back to direct RTSP capture.', camera_key)
            fallback_start_ts = time.time()
            self.write_rtsp_clip(stream_url, file_path, max_duration_seconds)
            return fallback_start_ts, max_duration_seconds
        tmp_path.replace(file_path)
        return content_start_ts, content_seconds

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
                '-c:v',
                'copy',
                '-c:a',
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
            import tempfile as _tempfile
            _stderr_file = _tempfile.NamedTemporaryFile(mode='w+', suffix='.log', delete=False, dir=str(self.prebuffer_dir))
            _stderr_path = _stderr_file.name
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=_stderr_file)
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
                if _stderr_path:
                    try:
                        _stderr_content = Path(_stderr_path).read_text(encoding='utf-8', errors='replace')
                        if _stderr_content.strip():
                            logger.debug('Prebuffer ffmpeg %s: %s', camera_key, _stderr_content.strip()[:1000])
                    except OSError:
                        pass
                    try:
                        Path(_stderr_path).unlink(missing_ok=True)
                    except OSError:
                        pass
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

    def _collect_prebuffer_segments(self, camera_key: str, start_ts: float, end_ts: float) -> tuple[list[Path], float | None]:
        """Return the segments whose footage overlaps [start_ts, end_ts] plus
        the wall-clock timestamp where the first segment's content begins.

        A segment's mtime marks when ffmpeg finished writing it — its content
        END. Its content START is the previous segment's mtime while the
        stream is continuous (segments split on keyframes, so they can exceed
        the nominal 1s). Selecting by content overlap keeps footage from
        before the requested window out of the clip, and the returned start
        lets the caller align stored timing and the detection track with what
        the rendered video actually shows."""
        camera_dir = self.prebuffer_dir / camera_key
        if not camera_dir.exists():
            return [], None
        timed: list[tuple[Path, float, float]] = []
        prev_end: float | None = None
        for segment in sorted(camera_dir.glob('segment-*.ts')):
            try:
                end = segment.stat().st_mtime
            except OSError:
                continue
            # After a gap (worker restart) fall back to the nominal 1s length.
            start = prev_end if prev_end is not None and 0 < end - prev_end <= 10 else end - 1.0
            timed.append((segment, start, end))
            prev_end = end
        if not timed:
            return [], None
        selected = [item for item in timed if item[2] > start_ts and item[1] < end_ts]
        if not selected:
            # Fallback to most recent segments covering the requested span.
            window_seconds = max(1, int(end_ts - start_ts))
            selected = timed[-window_seconds:]
        return [item[0] for item in selected], selected[0][1]

    @staticmethod
    def redact_stream_credentials(message: str) -> str:
        return re.sub(r'(rtsps?://[^:\s/@]+):[^@]+@', r'\1:***@', message)

    @staticmethod
    def clip_has_video_stream(file_path: Path) -> bool:
        """True if the clip actually contains a decodable video stream.

        ffmpeg can exit 0 while discarding every corrupt video frame (we pass
        +discardcorrupt / ignore_err to survive flaky RTSP), leaving a non-empty
        file with no video stream. Such a clip is unplayable, so callers verify
        the output rather than trusting the return code alone."""
        if not file_path.exists() or file_path.stat().st_size <= 0:
            return False
        ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            # Can't verify without ffprobe; assume the non-empty file is usable.
            return True
        command = [
            ffprobe,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'csv=p=0',
            str(file_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return True
        return result.returncode == 0 and bool(result.stdout.strip())

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
        # Prefer MP4V for generated placeholder clips.
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
