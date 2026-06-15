from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
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
    # A prebuffer render is "degenerate" when it produced far less video than the
    # window we asked for - a sign the source segments held no usable continuous
    # footage (sparse keyframes / corrupt stream). It surfaces as a near-still
    # clip whose stored duration lies about its real length, so we re-capture
    # live instead. Only judged for windows long enough to be meaningful.
    DEGENERATE_MIN_REQUEST_SECONDS = 8.0
    DEGENERATE_MAX_RENDERED_SECONDS = 3.0
    DEGENERATE_MAX_RENDERED_FRACTION = 0.25
    # Decoded-frame rate the shared ingest writes to latest.jpg for object
    # detection. The live monitor samples at ~2 Hz by default, so 4 fps keeps a
    # fresh frame available without spending CPU on frames nothing reads.
    INGEST_FRAME_FPS = 4
    # How long sidecar audio segments are retained before pruning (sound
    # detection consumes them within ~1s; keep a small safety margin).
    AUDIO_SEGMENT_RETENTION_SECONDS = 20

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.recording_config = config.get('recording', {})
        storage_config = config.get('storage', {})
        self.recordings_dir = Path(storage_config.get('recordings_dir') or Path(storage_config.get('data_dir', 'data')) / 'recordings')
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.prebuffer_dir = self.recordings_dir / '.prebuffer'
        self.prebuffer_dir.mkdir(parents=True, exist_ok=True)
        # Shared-ingest sidecar outputs (one ffmpeg per camera fans out to all
        # consumers, so each camera holds a single RTSP connection):
        #   .frames/<key>/latest.jpg  -> object detection + live snapshots
        #   .audio/<key>/aud-*.wav    -> sound detection (16 kHz mono PCM)
        self.frames_dir = self.recordings_dir / '.frames'
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = self.recordings_dir / '.audio'
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._prebuffer_lock = threading.Lock()
        self._prebuffer_workers: dict[str, dict[str, Any]] = {}
        self._continuous_lock = threading.Lock()
        self._continuous_workers: dict[str, dict[str, Any]] = {}
        self._missing_ffmpeg_warnings: set[str] = set()
        # Optional hook the application sets to surface operational events
        # (e.g. prebuffer fallbacks) into the camera diagnostics log. Signature:
        # callback(camera_id, event_type, message, severity, details).
        self.diagnostic_callback: Callable[..., None] | None = None

    def _emit_diagnostic(
        self,
        camera_id: str | None,
        event_type: str,
        message: str,
        *,
        severity: str = 'info',
        details: dict[str, Any] | None = None,
    ) -> None:
        callback = self.diagnostic_callback
        if callback is None:
            return
        try:
            callback(camera_id, event_type, message, severity=severity, details=details)
        except Exception as exc:
            logger.debug('Camera diagnostic callback failed for %s/%s: %s', camera_id, event_type, exc)

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
            # Keep glitchy-but-present video on a flaky stream rather than
            # discarding corrupt packets (which can empty the clip of video);
            # -err_detect ignore_err keeps the capture alive without dropping it.
            '-err_detect',
            'ignore_err',
            '-rtsp_transport',
            'tcp',
            '-i',
            stream_url,
            '-t',
            f'{float(duration_seconds):.3f}',
            '-map',
            '0:v:0',
            '-map',
            '0:a:0?',
            '-c:v',
            'libx264',
            '-c:a',
            'aac',
            '-b:a',
            '128k',
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
            # Stream may have dropped mid-clip; keep partial footage if ffmpeg
            # wrote a decodable file before dying rather than discarding it.
            if tmp_path.exists() and tmp_path.stat().st_size > 0 and self.clip_has_video_stream(tmp_path):
                logger.warning('ffmpeg exited non-zero for RTSP clip but partial footage is usable; keeping it.')
                tmp_path.replace(file_path)
                return
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

    @staticmethod
    def _stop_worker(worker: dict[str, Any], join_timeout: float = 2.0) -> None:
        stop_event = worker.get('stop_event')
        thread = worker.get('thread')
        if isinstance(stop_event, threading.Event):
            stop_event.set()
        if isinstance(thread, threading.Thread):
            thread.join(timeout=join_timeout)

    def stop_prebuffer_workers(self) -> None:
        with self._prebuffer_lock:
            workers = list(self._prebuffer_workers.values())
            self._prebuffer_workers = {}
        for worker in workers:
            self._stop_worker(worker)

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
        if not self._worker_ffmpeg_available('continuous_chunk_recording'):
            return False
        camera_key = self._camera_key(camera_id)
        chunks_dir = self.recordings_dir / f'continuous-{camera_key}'
        chunks_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_continuous_chunk_worker(camera_key, stream_url, chunks_dir, chunk_seconds, on_chunk_complete)
        return True

    def stop_continuous_chunk_recording(self, camera_id: str) -> None:
        camera_key = self._camera_key(camera_id)
        with self._continuous_lock:
            worker = self._continuous_workers.pop(camera_key, None)
        if worker:
            self._stop_worker(worker, join_timeout=5)

    def stop_all_continuous_recordings(self) -> None:
        with self._continuous_lock:
            workers = list(self._continuous_workers.values())
            self._continuous_workers = {}
        for worker in workers:
            self._stop_worker(worker, join_timeout=3)

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
                '-map', '0:v:0',
                '-map', '0:a:0?',
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-b:a', '128k',
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
        # The per-camera ingest is the SINGLE RTSP connection that feeds event
        # pre-roll, object detection (latest.jpg) and sound detection (audio
        # segments), so it runs whenever the camera has a stream — not only when
        # pre_event_seconds > 0.
        config = recording_config or self.recording_config
        if not self._worker_ffmpeg_available('camera_ingest'):
            return False
        camera_key = self._camera_key(camera_id)
        self._ensure_prebuffer_worker(camera_key, stream_url, self.prebuffer_window_seconds(config), camera_id=camera_id)
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
            return self._live_capture(stream_url, file_path, max_duration_seconds)

        # Use the same window the priming path computed, so re-ensuring the worker
        # here never restarts it mid-capture over a mismatched buffer size.
        if buffer_seconds is None:
            buffer_seconds = self.prebuffer_window_seconds()
        buffer_seconds = max(int(buffer_seconds), pre_seconds + post_seconds + 5, pre_seconds + 10, 15)
        camera_key = self._camera_key(camera_id)
        self._ensure_prebuffer_worker(camera_key, stream_url, buffer_seconds, camera_id=camera_id)

        end_capture_at = triggered_at.timestamp() + post_seconds
        delay = end_capture_at - time.time()
        if delay > 0:
            time.sleep(delay)

        start_ts = triggered_at.timestamp() - pre_seconds
        end_ts = end_capture_at
        segments, content_start_ts = self._collect_prebuffer_segments(camera_key, start_ts, end_ts)
        if not segments:
            logger.info('No prebuffer segments available for %s; falling back to direct RTSP clip capture.', camera_key)
            self._emit_diagnostic(
                camera_id,
                'prebuffer_fallback',
                'No pre-event buffer was available, so the clip was captured live from the trigger forward — '
                'the moments before the trigger are missing.',
                severity='warning',
                details={'reason': 'no_segments'},
            )
            return self._live_capture(stream_url, file_path, max_duration_seconds)

        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self._emit_diagnostic(
                camera_id,
                'prebuffer_fallback',
                'ffmpeg is not installed, so the pre-event buffer could not be rendered and the clip was captured live.',
                severity='warning',
                details={'reason': 'ffmpeg_missing', 'segment_count': len(segments)},
            )
            return self._live_capture(stream_url, file_path, max_duration_seconds)

        if content_start_ts is None:
            content_start_ts = start_ts
        # Render exactly the footage between where the first selected segment
        # starts and the capture deadline. The keyframe-aligned lead before
        # start_ts is kept (and reported via content_start_ts) rather than
        # silently eating the same amount off the end of the clip.
        content_seconds = max(1.0, min(end_ts - content_start_ts, max_duration_seconds + 10.0))

        list_path = file_path.with_name(f'{file_path.stem}.concat.txt')
        tmp_path = file_path.with_name(f'{file_path.stem}.prebuffer.tmp{file_path.suffix}')
        list_content = ''.join(self._concat_file_line(segment) for segment in segments)
        list_path.write_text(list_content, encoding='utf-8')
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        command = [
            ffmpeg,
            '-y',
            # Do NOT use +discardcorrupt / -err_detect ignore_err on this concat.
            # Those are for the flaky *live* RTSP feed; on the rolling buffer's own
            # segments they treat any captured packet corruption as a reason to
            # drop frames, and once a stream carries some corruption (e.g. a flaky
            # link) they discard EVERY video packet, yielding a valid-but-videoless
            # clip (audio only). Verified: corrupted segments rendered 0 video
            # frames with these flags vs. recovering frames without them. Keep
            # corrupt frames and just repair timestamps so the video decodes.
            '-fflags',
            '+genpts',
            '-f',
            'concat',
            '-safe',
            '0',
            '-i',
            str(list_path),
            '-map',
            '0:v:0',
            '-map',
            '0:a:0?',
            # The rolling prebuffer segments are already browser-oriented H.264
            # video with AAC audio. Remux them instead of decoding/re-encoding:
            # it is much cheaper on small boards and preserves recoverable video
            # packets from imperfect RTSP segments.
            '-c',
            'copy',
            '-avoid_negative_ts',
            'make_zero',
            '-movflags',
            '+faststart',
            '-t',
            f'{content_seconds:.3f}',
            str(tmp_path),
        ]
        try:
            # Remuxing should be far faster than realtime, but keep a generous
            # ceiling for slow disks and large max_clip_seconds settings.
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(60, int(content_seconds) + 30),
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = None
        finally:
            list_path.unlink(missing_ok=True)
        output_has_video = tmp_path.exists() and tmp_path.stat().st_size > 0 and self.clip_has_video_stream(tmp_path)
        if result is not None and result.returncode != 0 and output_has_video:
            logger.warning('ffmpeg exited non-zero for prebuffer render of %s but partial footage is usable; keeping it.', camera_key)

        if result is None or not output_has_video:
            tmp_path.unlink(missing_ok=True)
            logger.warning('Failed to render clip from prebuffer for %s; falling back to direct RTSP capture.', camera_key)
            if result is None:
                render_reason = 'timeout'
                stderr_tail = ''
                return_code = None
            else:
                stderr_tail = self.redact_stream_credentials((result.stderr or '')[-1000:])
                return_code = result.returncode
                render_reason = 'no_video_frames' if 'frame= 0' in stderr_tail or 'video:0KiB' in stderr_tail else 'render_failed'
            self._emit_diagnostic(
                camera_id,
                'prebuffer_fallback',
                'Pre-event buffer could not be rendered, so the clip was captured live from the trigger forward — '
                'the moments before the trigger are missing.',
                severity='warning',
                details={
                    'reason': render_reason,
                    'returncode': return_code,
                    'segment_count': len(segments),
                    'requested_seconds': round(content_seconds, 1),
                    'window_start': datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
                    'window_end': datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
                    'content_start': datetime.fromtimestamp(content_start_ts, tz=timezone.utc).isoformat(),
                    'stderr_tail': stderr_tail,
                },
            )
            return self._live_capture(stream_url, file_path, max_duration_seconds)

        # The render can pass the existence/decodability checks above yet still
        # contain far less video than the requested window (e.g. only a single
        # surviving keyframe when the stream's keyframes are sparse or corrupt).
        # Keep that partial prebuffer footage instead of starting a live fallback
        # after the event window has passed; a short clip from the right time is
        # more useful than a full-length clip of the wrong time.
        rendered_seconds = self.clip_duration_seconds(tmp_path)
        if self._clip_is_degenerate(rendered_seconds, content_seconds):
            logger.warning(
                'Prebuffer render for %s produced only %.1fs of video for a %.0fs window; keeping partial event footage.',
                camera_key, rendered_seconds or 0.0, content_seconds,
            )
            self._emit_diagnostic(
                camera_id,
                'prebuffer_partial',
                f'Pre-event buffer rendered only {rendered_seconds or 0.0:.1f}s of playable video for a '
                f'{content_seconds:.0f}s window (likely sparse keyframes or a corrupt stream); saved the partial footage.',
                severity='warning',
                details={'rendered_seconds': round(rendered_seconds or 0.0, 1), 'requested_seconds': round(content_seconds, 1)},
            )

        tmp_path.replace(file_path)
        # Report the clip's real duration, not the requested window — keyframe
        # alignment and short source footage make them differ, and a mismatch
        # shows up as playback that ends well before the stated length.
        return content_start_ts, (rendered_seconds or content_seconds)

    @staticmethod
    def _camera_key(camera_id: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_-]+', '-', str(camera_id or '').strip().lower()).strip('-') or 'camera'

    @staticmethod
    def _concat_file_line(file_path: Path) -> str:
        """Return an ffmpeg concat-demuxer file line for this path.

        The concat demuxer parses backslashes as escapes and resolves relative
        paths from the list file location. Windows paths like
        ``data\\recordings\\.prebuffer\\...`` can therefore point at the wrong
        file or fail to parse, causing otherwise healthy prebuffer segments to
        fall back to late direct RTSP capture.
        """
        escaped = file_path.resolve().as_posix().replace("'", r"'\''")
        return f"file '{escaped}'\n"

    def _worker_ffmpeg_available(self, purpose: str) -> bool:
        if shutil.which('ffmpeg'):
            self._missing_ffmpeg_warnings.discard(purpose)
            return True
        if purpose not in self._missing_ffmpeg_warnings:
            logger.warning('ffmpeg is required for %s but is not installed.', purpose.replace('_', ' '))
            self._missing_ffmpeg_warnings.add(purpose)
        return False

    def _ensure_prebuffer_worker(self, camera_key: str, stream_url: str, buffer_seconds: int, camera_id: str | None = None) -> None:
        restart_reason: str | None = None
        with self._prebuffer_lock:
            existing = self._prebuffer_workers.get(camera_key)
            if existing and existing.get('stream_url') == stream_url and existing.get('buffer_seconds') == buffer_seconds:
                thread = existing.get('thread')
                if isinstance(thread, threading.Thread) and thread.is_alive():
                    return
            if existing:
                existing_thread = existing.get('thread')
                if isinstance(existing_thread, threading.Thread) and existing_thread.is_alive():
                    # Replacing a LIVE worker discards its rolling buffer. This is
                    # expected once after a settings change, but if it keeps
                    # happening the buffer never fills and events render as near-
                    # still clips — so surface why, to catch config churn / collisions.
                    if existing.get('stream_url') != stream_url:
                        restart_reason = 'stream_url_changed'
                    elif existing.get('buffer_seconds') != buffer_seconds:
                        restart_reason = 'buffer_seconds_changed'
                if isinstance(existing.get('stop_event'), threading.Event):
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
        if restart_reason:
            logger.info('Prebuffer worker for %s restarted (%s); rolling buffer was reset.', camera_key, restart_reason)
            self._emit_diagnostic(
                camera_id or camera_key,
                'prebuffer_restart',
                f'Pre-event buffer worker restarted ({restart_reason}); the rolling buffer was reset. '
                'Frequent restarts leave events without pre-roll footage.',
                severity='warning',
                details={'reason': restart_reason, 'camera_key': camera_key},
            )

    def _run_prebuffer_worker(self, camera_key: str, stream_url: str, buffer_seconds: int, stop_event: threading.Event) -> None:
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            logger.warning('ffmpeg is required for rolling prebuffer but is not installed.')
            return
        camera_dir = self.prebuffer_dir / camera_key
        camera_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = camera_dir / 'segment-%Y%m%dT%H%M%S.ts'
        # Sidecar outputs for the other consumers of this single connection.
        frames_dir = self.frames_dir / camera_key
        frames_dir.mkdir(parents=True, exist_ok=True)
        latest_frame_path = frames_dir / 'latest.jpg'
        audio_camera_dir = self.audio_dir / camera_key
        audio_camera_dir.mkdir(parents=True, exist_ok=True)
        audio_pattern = audio_camera_dir / 'aud-%Y%m%dT%H%M%S.wav'

        while not stop_event.is_set():
            command = [
                ffmpeg,
                '-nostdin',
                '-hide_banner',
                '-loglevel',
                'error',
                '-rtsp_transport',
                'tcp',
                # Do NOT use +discardcorrupt here. On a flaky camera link it drops
                # every corrupt VIDEO packet as it is captured, leaving near
                # audio-only segments, so the event render later finds no video
                # (frame=0) and falls back to a late live capture. Keep the
                # glitchy-but-present frames instead (same principle as the concat
                # render fix); -err_detect ignore_err keeps ffmpeg alive on the
                # bad stream without throwing the video away.
                '-err_detect',
                'ignore_err',
                '-i',
                stream_url,
                # Output 1: rolling video+audio segments for event clips.
                '-map',
                '0:v:0',
                '-map',
                '0:a:0?',
                '-c:v',
                'copy',
                '-c:a',
                'aac',
                '-b:a',
                '128k',
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
                # Output 2: latest decoded frame for object detection + snapshots.
                # Written to a temp name then atomically renamed so a reader never
                # sees a half-written JPEG. -update overwrites the same target.
                '-map',
                '0:v:0',
                '-vf',
                f'fps={self.INGEST_FRAME_FPS}',
                '-update',
                '1',
                '-atomic_writing',
                '1',
                '-f',
                'image2',
                '-y',
                str(latest_frame_path),
                # Output 3: 1s mono 16 kHz PCM-WAV segments for sound detection.
                '-map',
                '0:a:0?',
                '-vn',
                '-acodec',
                'pcm_s16le',
                '-ar',
                '16000',
                '-ac',
                '1',
                '-f',
                'segment',
                '-segment_time',
                '1',
                '-segment_format',
                'wav',
                '-reset_timestamps',
                '1',
                '-strftime',
                '1',
                str(audio_pattern),
            ]
            stderr_file = tempfile.NamedTemporaryFile(mode='w+', suffix='.log', delete=False, dir=str(self.prebuffer_dir))
            stderr_path = Path(stderr_file.name)
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr_file)
            stderr_file.close()
            try:
                while process.poll() is None and not stop_event.is_set():
                    self._prune_prebuffer_segments(camera_dir, buffer_seconds)
                    self._prune_audio_segments(audio_camera_dir)
                    time.sleep(1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                try:
                    stderr_content = stderr_path.read_text(encoding='utf-8', errors='replace')
                    if stderr_content.strip():
                        logger.debug('Prebuffer ffmpeg %s: %s', camera_key, stderr_content.strip()[:1000])
                except OSError:
                    pass
                stderr_path.unlink(missing_ok=True)
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

    def _prune_audio_segments(self, audio_camera_dir: Path) -> None:
        cutoff = time.time() - self.AUDIO_SEGMENT_RETENTION_SECONDS
        for segment in audio_camera_dir.glob('aud-*.wav'):
            try:
                if segment.stat().st_mtime < cutoff:
                    segment.unlink(missing_ok=True)
            except OSError:
                continue

    # ── Shared-ingest accessors ──────────────────────────────────────
    def latest_frame_jpeg(self, camera_id: str, *, max_age_seconds: float = 10.0) -> tuple[bytes, float] | None:
        """Most recent decoded frame the ingest wrote for this camera, as
        (jpeg_bytes, captured_ts). ``captured_ts`` is the file mtime — when
        ffmpeg wrote the frame — so detection samples and playback overlays stay
        aligned. Returns None when no fresh frame is available (ingest warming
        up, camera offline, or ffmpeg unavailable)."""
        path = self.frames_dir / self._camera_key(camera_id) / 'latest.jpg'
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if max_age_seconds and (time.time() - mtime) > max_age_seconds:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        return data, mtime

    def audio_segments_after(self, camera_id: str, after_ts: float) -> list[tuple[Path, float]]:
        """Audio WAV segments written strictly after ``after_ts``, oldest first,
        as (path, mtime). Lets the sound detector consume each 1s chunk once
        without reopening its own RTSP connection."""
        audio_camera_dir = self.audio_dir / self._camera_key(camera_id)
        if not audio_camera_dir.exists():
            return []
        out: list[tuple[Path, float]] = []
        for segment in audio_camera_dir.glob('aud-*.wav'):
            try:
                mtime = segment.stat().st_mtime
            except OSError:
                continue
            if mtime > after_ts:
                out.append((segment, mtime))
        out.sort(key=lambda item: item[1])
        return out

    def _collect_prebuffer_segments(self, camera_key: str, start_ts: float, end_ts: float) -> tuple[list[Path], float | None]:
        """Return the segments whose footage overlaps [start_ts, end_ts] plus
        the wall-clock timestamp where the first segment's content begins.

        A segment's mtime marks when ffmpeg finished writing it - its content
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
        return [item[0] for item in selected], selected[0][1] if selected else None

    @staticmethod
    def redact_stream_credentials(message: str) -> str:
        return re.sub(r'(rtsps?://[^:\s/@]+):[^@]+@', r'\1:***@', message)

    @staticmethod
    def clip_has_video_stream(file_path: Path) -> bool:
        """True if the clip actually contains a decodable video stream.

        ffmpeg can exit 0 while discarding every corrupt video frame (we pass
        +discardcorrupt / ignore_err to survive flaky RTSP), leaving a non-empty
        file with no video stream, or with a declared video stream but zero video
        packets. Such a clip is unplayable, so callers verify the output rather
        than trusting the return code alone."""
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
        if result.returncode != 0 or not result.stdout.strip():
            return False
        packet_count = RecordingService.clip_video_packet_count(file_path)
        return packet_count is None or packet_count > 0

    @staticmethod
    def clip_video_packet_count(file_path: Path) -> int | None:
        """Return video packet count when ffprobe can determine it.

        Some failed renders produce a tiny MP4 with a video stream declaration
        but no video packets (`frame=0`, `video:0KiB`). Codec-only probing treats
        that as valid; packet counting catches it.
        """
        if not file_path.exists() or file_path.stat().st_size <= 0:
            return 0
        ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            return None
        command = [
            ffprobe,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-count_packets',
            '-show_entries', 'stream=nb_read_packets',
            '-of', 'csv=p=0',
            str(file_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        raw_value = result.stdout.strip()
        if not raw_value or raw_value.upper() == 'N/A':
            return None
        try:
            return max(0, int(raw_value))
        except ValueError:
            return None

    @staticmethod
    def clip_duration_seconds(file_path: Path) -> float | None:
        """Actual decodable video duration of a clip in seconds, or None if it
        can't be determined. Used to store an honest duration (the requested
        window and the real rendered length diverge when the source footage is
        short or keyframe-sparse) and to detect degenerate near-still clips."""
        video_duration = RecordingService.clip_video_duration_seconds(file_path)
        if video_duration is not None:
            return video_duration
        return RecordingService.clip_container_duration_seconds(file_path)

    @staticmethod
    def clip_video_duration_seconds(file_path: Path) -> float | None:
        """Duration of the first video stream, ignoring audio/container length."""
        if not file_path.exists() or file_path.stat().st_size <= 0:
            return None
        ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            return None
        command = [
            ffprobe,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=duration',
            '-of', 'csv=p=0',
            str(file_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            value = float(result.stdout.strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def clip_container_duration_seconds(file_path: Path) -> float | None:
        """Container duration fallback for files whose video stream duration is
        not available. This is less reliable for corrupted renders because audio
        can keep the container duration long after video frames stop."""
        if not file_path.exists() or file_path.stat().st_size <= 0:
            return None
        ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            return None
        command = [
            ffprobe,
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'csv=p=0',
            str(file_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            value = float(result.stdout.strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @classmethod
    def _clip_is_degenerate(cls, rendered_seconds: float | None, requested_seconds: float) -> bool:
        if rendered_seconds is None or rendered_seconds <= 0:
            return False
        minimum_usable_seconds = max(
            cls.DEGENERATE_MAX_RENDERED_SECONDS,
            requested_seconds * cls.DEGENERATE_MAX_RENDERED_FRACTION,
        )
        return (
            requested_seconds >= cls.DEGENERATE_MIN_REQUEST_SECONDS
            and rendered_seconds < minimum_usable_seconds
        )

    def _live_capture(self, stream_url: str, file_path: Path, max_duration_seconds: float) -> tuple[float, float]:
        """Capture live from now and report the clip's real duration. Used as the
        fallback when the prebuffer can't supply usable pre-event footage."""
        start_ts = time.time()
        self.write_rtsp_clip(stream_url, file_path, max_duration_seconds)
        return start_ts, (self.clip_duration_seconds(file_path) or max_duration_seconds)

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
