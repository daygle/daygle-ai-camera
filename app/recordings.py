from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import app.state as _state


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
    DEGENERATE_MAX_RENDERED_FRACTION = 0.50
    # Decoded-frame rate the shared ingest writes to latest.jpg for object
    # detection. The live monitor samples at ~2 Hz by default, so 4 fps keeps a
    # fresh frame available without spending CPU on frames nothing reads.
    INGEST_FRAME_FPS = 4
    # JPEG quality for live snapshots on mjpeg's 2-31 scale (lower = better,
    # larger files). 2 matches the old OpenCV-encoded quality; ffmpeg's mjpeg
    # default is noticeably more compressed. Operator-overridable via the
    # Detection & Live "Snapshot Quality" setting.
    SNAPSHOT_QUALITY = 2
    # Event prebuffer video segment length. Tiny 1s stream-copy segments are
    # fragile with sparse-keyframe RTSP streams; 4s reduces concat boundaries
    # while keeping event timing reasonably granular.
    PREBUFFER_SEGMENT_SECONDS = 4
    PREBUFFER_SEGMENT_GLOB = 'segment-*.mp4'
    # Floor for sidecar audio retention. The worker actually retains audio for
    # the full prebuffer window (pre + max_clip) so long event clips get audio
    # for their whole length; this is just the minimum when that window is tiny.
    AUDIO_SEGMENT_RETENTION_SECONDS = 20
    # Minimum effective pre-roll for RTSP *event* rendering. The per-camera
    # rolling prebuffer runs continuously for every RTSP camera (see
    # ``app.live_monitor.run_live_alert_monitor_once`` -> ``prime_rtsp_prebuffer``),
    # so the trigger moment is always buffered. A configured ``pre_event_seconds``
    # of 0 would otherwise bypass that buffer and capture live *after* the event
    # window has elapsed -- recording the aftermath rather than the event. Flooring
    # the pre-roll to a couple of seconds keeps the trigger inside the rendered
    # window and absorbs detection + RTSP-connect latency; live capture remains
    # the fallback (below) when the buffer genuinely holds no usable segments.
    RTSP_EVENT_MIN_PRE_SECONDS = 2
    # How much requested pre-roll can go unrendered before it is worth a
    # diagnostic. The rendered clip starts at the oldest buffered segment
    # overlapping the window; keyframe alignment normally makes that start AT or
    # BEFORE the requested point (extra lead-in, never a shortfall). A start
    # materially LATER than requested means the rolling buffer had not filled
    # back to ``triggered_at - pre_seconds`` yet - a genuinely short clip. A
    # couple of seconds of slack absorbs segment-boundary jitter so only real
    # shortfalls (buffer not yet full after a restart / reconnect) are surfaced.
    PREBUFFER_SHORT_PREROLL_TOLERANCE_SECONDS = 2.0
    # Worst-case ceiling for ``_stop_worker``'s ``thread.join`` when the worker
    # that's being joined is the per-camera ingest / prebuffer ffmpeg (1s loop
    # sleep + up to 2s ``process.wait(timeout=2)`` for SIGTERM grace + SIGKILL
    # fallback in ``_run_prebuffer_worker``). Same value is used wherever this
    # worker is replaced or explicitly stopped so ``stop_*`` are synchronously
    # bound to the same ceiling as the ``_ensure_*`` paths.
    PREBUFFER_WORKER_JOIN_TIMEOUT_SECONDS = 4.0
    # Worst-case ceiling for ``_stop_worker``'s ``thread.join`` when the worker
    # that's being joined is the continuous-chunk ffmpeg (1s loop sleep + up to
    # 5s ``process.wait(timeout=5)`` for SIGTERM grace + SIGKILL fallback in
    # ``_run_continuous_chunk_worker``). Same value is used wherever this
    # worker is replaced or explicitly stopped so ``stop_*`` are synchronously
    # bound to the same ceiling as the ``_ensure_*`` paths.
    CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS = 7.0
    # Minimum interval between prebuffer restarts for the same camera.
    # When two callers (e.g. live-monitor loop and an API-driven prime)
    # alternate different stream URLs they restart each other on every
    # cycle. This debounce suppresses rapid toggles: a genuine admin-saved
    # URL change still restarts the worker once the window expires.
    PREBUFFER_RESTART_DEBOUNCE_SECONDS: float = 10.0

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
        # Per-camera timestamp of the last prebuffer restart; used to debounce
        # rapid ping-pong restarts when two callers (e.g. live-monitor loop and
        # an API-driven prime) alternate different stream URLs for the same
        # camera. Without this guard each caller restarts the other's worker on
        # every cycle, the rolling buffer never fills, and events render with no
        # pre-roll footage.
        self._prebuffer_last_restart: dict[str, float] = {}
        # High-res recording prebuffer: parallel video-only worker for the
        # recording stream URL when a camera exposes dual streams. Captures
        # full-resolution segments so event clips render at recording quality
        # throughout (pre-roll + post), eliminating the resolution jump.
        self._rec_prebuffer_lock = threading.Lock()
        self._rec_prebuffer_workers: dict[str, dict[str, Any]] = {}
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
                if not allow_motion and label in self.GENERIC_TRIGGER_LABELS:
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
        # Normalize to UTC so the derived ``started_at`` / ``ended_at``
        # strings compare cleanly against the SQL cutoff / day-window
        # timestamps in ``DB.purge_recordings`` / ``list_recordings_for_camera_day``
        # -- which are bound in canonical UTC ``+00:00`` form. Without
        # this, a source event with a negative tz offset (e.g.
        # ``-05:00``) sorts lexicographically BEFORE the cutoff and the
        # recording gets purged up to TZ-offset hours early.
        created = created.astimezone(timezone.utc)

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
            # Preserve the source frame timestamps when the high-resolution
            # prebuffer is unavailable and we must capture directly. No scale or
            # output-rate filter is applied, so source resolution/FPS remain the
            # recording stream's values rather than the detection ingest rate.
            '-fps_mode',
            'passthrough',
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
            stderr = result.stderr or ''
            error_detail = self.redact_stream_credentials(stderr if len(stderr) <= 500 else f'{stderr[:500]}\n...\n{stderr[-1000:]}')
            raise RuntimeError(f'ffmpeg failed to record RTSP clip: {error_detail}')
        if not tmp_path.exists():
            raise RuntimeError('ffmpeg did not create an RTSP recording file.')
        if not self.clip_has_video_stream(tmp_path):
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError('Recorded RTSP clip contains no decodable video stream.')
        tmp_path.replace(file_path)

    @staticmethod
    def _stop_worker(worker: dict[str, Any], join_timeout: float | None = None) -> None:
        # Default to the larger of the two worker ceilings so a caller that
        # forgets to pass an explicit value can't silently regress to a tight
        # 2s join that masks a still-running ffmpeg. Callers should always
        # pass the worker-appropriate constant explicitly; this fallback is
        # only here for safety on legacy / monkeypatched call paths.
        if join_timeout is None:
            join_timeout = RecordingService.CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS
        stop_event = worker.get('stop_event')
        thread = worker.get('thread')
        if isinstance(stop_event, threading.Event):
            stop_event.set()
        if isinstance(thread, threading.Thread):
            join_start = time.monotonic()
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                # Bug 5: a worker thread that ignored ``stop_event`` and
                # survived the full ``join_timeout`` window is a hung ffmpeg
                # teardown (network stall, SIGTERM-handler crash, kill
                # blocked on a flush), NOT a graceful shutdown. The
                # replacement worker is still started from the caller's
                # perspective because denying camera ingest would be worse
                # than a brief overlap, but operators deserve visibility -
                # surface this through the same ``diagnostic_callback``
                # already used by ``prebuffer_restart`` / ``ingest_restart``
                # / ``prebuffer_fallback`` so a hung camera shows up in the
                # camera-diagnostics log alongside the other ingest
                # failures. Capture ``join_start`` before the join so
                # ``alive_after_seconds`` measures the actual wall time we
                # spent waiting before giving up (≤ ``join_timeout``).
                alive_seconds = time.monotonic() - join_start
                # Worker kind is derived from the dict shape rather than a
                # call-site flag so diagnostic emission stays an internal
                # concern of ``_stop_worker``: prebuffer workers carry
                # ``buffer_seconds`` (the rolling-window size), continuous
                # chunk workers carry ``chunk_seconds`` (the segment size).
                worker_kind = 'prebuffer' if 'buffer_seconds' in worker else 'continuous'
                # ``camera_id`` is always populated by the ensure path
                # (prebuffer stores the friendly name, continuous stores
                # the sanitized key when no friendly name exists).
                camera_id = worker.get('camera_id')
                event_type = f'worker_stop_join_timeout_{worker_kind}'
                message = (
                    f'{worker_kind.capitalize()} worker thread for {camera_id or "(unknown camera)"} '
                    f'did not exit within {join_timeout}s of stop_event.set; thread {thread.name!r} '
                    f'was still alive after waiting {alive_seconds:.3f}s. The replacement worker has '
                    f'been started anyway and may briefly overlap with the previous ffmpeg, which was '
                    f'probably hung in its SIGTERM teardown.'
                )
                details = {
                    'camera_id': camera_id,
                    'requested_timeout_seconds': join_timeout,
                    'alive_after_seconds': round(alive_seconds, 3),
                    'worker_kind': worker_kind,
                    'thread_name': thread.name,
                }
                diagnostic_callback = worker.get('diagnostic_callback')
                if diagnostic_callback is not None:
                    try:
                        diagnostic_callback(
                            camera_id,
                            event_type,
                            message,
                            severity='warning',
                            details=details,
                        )
                    except Exception as exc:
                        # A misbehaving diagnostic surface must not crash
                        # the shutdown path - the worker thread is hung, we
                        # are racing to start the replacement, every error
                        # here is operator-noise at best.
                        logger.debug(
                            'Camera diagnostic callback failed for %s: %s',
                            event_type, exc,
                        )
                else:
                    # No application-level diagnostic surface is configured
                    # (e.g. in unit-test contexts); still log a warning so
                    # the assumption "the operator saw a structured event"
                    # doesn't silently degrade to "no signal at all".
                    logger.warning('%s details=%s', message, details)

    def stop_prebuffer_workers(self) -> None:
        # Bug 4 fix: hold ``_prebuffer_lock`` through the per-worker join so
        # a concurrent ``_ensure_prebuffer_worker`` cannot start a new ffmpeg
        # while the old ffmpeg is still in its SIGTERM teardown. Joining
        # under the same lock mirrors the pattern ``_ensure_prebuffer_worker``
        # already uses internally to close the Bug 2 race, applied here to
        # the explicit-shutdown entry points. The join_timeout matches
        # ``_ensure_prebuffer_worker``'s worst-case ceiling
        # (``PREBUFFER_WORKER_JOIN_TIMEOUT_SECONDS``) so an explicit shutdown
        # is synchronously bound to the same bound the ensure path enforces;
        # a multi-camera shutdown holds the lock for up to N * that value,
        # which is acceptable because this is a one-shot admin/shutdown path.
        with self._prebuffer_lock:
            workers = list(self._prebuffer_workers.values())
            self._prebuffer_workers = {}
            for worker in workers:
                self._stop_worker(worker, join_timeout=self.PREBUFFER_WORKER_JOIN_TIMEOUT_SECONDS)

    def start_continuous_chunk_recording(
        self,
        *,
        stream_url: str,
        camera_id: str,
        recording_config: dict[str, Any] | None = None,
        on_chunk_complete: Callable[[str, Path], None] | None = None,
    ) -> bool:
        # Bug 6 follow-up: acquire ``_state._apply_settings_lock`` so a
        # concurrent monitor poll that calls this method blocks while
        # ``apply_storage_and_recording_settings`` is mid-swap (publishing
        # the sentinel, stopping OLD workers, then publishing the NEW
        # service). Without this, a concurrent
        # ``start_continuous_chunk_recording`` could see the OLD service
        # mid-teardown and land a fresh ffmpeg in the just-vacated
        # ``continuous-<key>/`` directory while the OLD ffmpeg is still
        # alive in its SIGTERM teardown -- two workers writing into the
        # same per-camera directory at the same time. The lock is
        # reentrant (``RLock``) so the apply_* functions can already be
        # holding it when they call ``RecordingService`` methods that
        # would otherwise need to re-acquire it.
        with _state._apply_settings_lock:
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
        # Bug 4 fix: hold ``_continuous_lock`` through the join so a
        # concurrent ``_ensure_continuous_chunk_worker`` cannot start a new
        # ffmpeg writing into the just-vacated ``continuous-{key}/`` directory
        # while the old ffmpeg is still alive in its SIGTERM teardown. The
        # join_timeout matches ``_ensure_continuous_chunk_worker``'s
        # worst-case ceiling (``CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS``)
        # so this explicit shutdown is synchronously bound to the same bound
        # the ensure path enforces.
        with self._continuous_lock:
            worker = self._continuous_workers.pop(camera_key, None)
            if worker:
                self._stop_worker(worker, join_timeout=self.CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS)

    def stop_all_continuous_recordings(self) -> None:
        # Bug 4 fix: same pattern as ``stop_continuous_chunk_recording`` -
        # hold ``_continuous_lock`` through the per-worker join so a
        # concurrent ensure for any key can't race in. The join_timeout
        # (``CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS``) matches
        # ``_ensure_continuous_chunk_worker``'s worst-case ceiling.
        with self._continuous_lock:
            workers = list(self._continuous_workers.values())
            self._continuous_workers = {}
            for worker in workers:
                self._stop_worker(worker, join_timeout=self.CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS)

    def stop_camera_workers(self, camera_id: str) -> None:
        """Stop the prebuffer and continuous workers for a single camera.

        Called when a camera is removed from the active configuration so that
        its ffmpeg ingest process is torn down immediately rather than continuing
        to run (and emitting ingest_restart diagnostics) until the whole
        RecordingService is rebuilt.
        """
        camera_key = self._camera_key(camera_id)
        with self._prebuffer_lock:
            worker = self._prebuffer_workers.pop(camera_key, None)
            if worker:
                self._stop_worker(worker, join_timeout=self.PREBUFFER_WORKER_JOIN_TIMEOUT_SECONDS)
        with self._rec_prebuffer_lock:
            worker = self._rec_prebuffer_workers.pop(camera_key, None)
            if worker:
                self._stop_worker(worker, join_timeout=self.PREBUFFER_WORKER_JOIN_TIMEOUT_SECONDS)
        with self._continuous_lock:
            worker = self._continuous_workers.pop(camera_key, None)
            if worker:
                self._stop_worker(worker, join_timeout=self.CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS)

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
            # Bug 2 fix: same pattern as ``_ensure_prebuffer_worker`` -
            # join the old worker's thread before starting a replacement so
            # two ffmpegs can't write into the same ``continuous-{key}/``
            # directory concurrently and a chunk callback can't observe a
            # segment the new ffmpeg is still writing. Continuous workers
            # have a longer per-iteration teardown (ffmpeg SIGTERM waits up
            # to 5s before SIGKILL in ``_run_continuous_chunk_worker``), so
            # the join timeout uses
            # ``CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS`` and covers the
            # worst-case shutdown plus ~1s of headroom over OS scheduling
            # jitter, without padding the lock long enough to noticeably
            # delay concurrent settings changes for other cameras.
            if existing:
                self._stop_worker(existing, join_timeout=self.CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS)

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
                # Continuous workers previously had no ``camera_id`` on the
                # dict; the sanitized camera_key was only available via the
                # outer dict key. Add it so ``_stop_worker``'s hang-on-stop
                # diagnostic (Bug 5) can identify which camera hung.
                'camera_id': camera_key,
                # Captured at construction for the same reason as the
                # prebuffer worker_state. See the matching comment there.
                'diagnostic_callback': self.diagnostic_callback,
            }
            thread.start()

    def _drain_chunk_list(
        self,
        camera_key: str,
        chunks_dir: Path,
        list_file: Path,
        seen_count: int,
        on_chunk_complete: Callable[[str, Path], None] | None,
    ) -> int:
        """Drain any unaccounted-for entries from ffmpeg's ``-segment_list``
        file, calling ``on_chunk_complete`` for each new chunk. Returns the
        updated ``seen_count``.

        Used both by the live polling loop inside
        ``_run_continuous_chunk_worker`` AND once after ffmpeg's graceful
        shutdown (SIGTERM) returns, so any chunk finalised during teardown
        is surfaced to the callback before the worker thread exits. Without
        the post-``process.wait`` drain the final chunk of a stopped
        continuous recording is orphaned on disk the moment the user stops
        recording: ``on_chunk_complete`` never fires for it, the database
        never learns about it, and the file sits in the chunks directory
        with no recordings row to point at it.
        """
        if not list_file.exists():
            return seen_count
        try:
            lines = list_file.read_text(encoding='utf-8').splitlines()
        except OSError:
            return seen_count
        for index in range(seen_count, len(lines)):
            segment_name = lines[index].strip()
            if not segment_name:
                seen_count = index + 1
                continue
            segment_path = chunks_dir / segment_name
            try:
                if segment_path.exists() and segment_path.stat().st_size > 0:
                    if on_chunk_complete:
                        try:
                            on_chunk_complete(camera_key, segment_path)
                        except Exception as exc:
                            logger.warning(
                                'Continuous chunk callback failed for %s/%s: %s',
                                camera_key, segment_name, exc,
                            )
                    seen_count = index + 1
            except Exception as exc:
                logger.warning(
                    'Continuous chunk callback failed for %s/%s: %s',
                    camera_key, segment_name, exc,
                )
                seen_count = index + 1
        return seen_count

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
                    seen_count = self._drain_chunk_list(camera_key, chunks_dir, list_file, seen_count, on_chunk_complete)
                    time.sleep(1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                # Bug 3 fix: drain any chunk ffmpeg finalised during graceful
                # shutdown (SIGTERM). The inner polling loop bailed out the
                # instant ``stop_event`` fired, so the chunk that was open at
                # that moment - and the entry ffmpeg appended to
                # ``.segment_list`` while tearing down - would otherwise never
                # reach ``on_chunk_complete`` and would sit orphaned on disk.
                seen_count = self._drain_chunk_list(camera_key, chunks_dir, list_file, seen_count, on_chunk_complete)
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
        recording_stream_path: str = '',
    ) -> bool:
        # Bug 6 follow-up: acquire ``_state._apply_settings_lock`` so a
        # concurrent monitor poll (live_alert_monitor_loop,
        # sound_monitor, event-driven callbacks) blocks while
        # ``apply_storage_and_recording_settings`` is mid-swap. Without
        # this, a concurrent prime could see the OLD service mid-teardown
        # and land an ``_ensure_prebuffer_worker`` call that spawns a
        # fresh ffmpeg writing into the just-vacated
        # ``.prebuffer/<key>/`` / ``.audio/<key>/`` /
        # ``.frames/<key>/`` directories while the OLD ffmpeg is still
        # alive in its SIGTERM teardown -- two workers writing into the
        # same per-camera directory at the same time. Lock is reentrant
        # (``RLock``); see the matching ``start_continuous_chunk_recording``
        # note.
        with _state._apply_settings_lock:
            # The per-camera ingest is the SINGLE RTSP connection that feeds event
            # pre-roll, object detection (latest.jpg) and sound detection (audio
            # segments), so it runs whenever the camera has a stream - not only when
            # pre_event_seconds > 0.
            config = recording_config or self.recording_config
            if not self._worker_ffmpeg_available('camera_ingest'):
                return False
            camera_key = self._camera_key(camera_id)
            self._ensure_prebuffer_worker(camera_key, stream_url, self.prebuffer_window_seconds(config), camera_id=camera_id)
            # Start a parallel high-res prebuffer when a dual-stream recording
            # URL is configured. This worker captures video-only segments from
            # the main stream so event clips render at full resolution
            # throughout (pre-roll + post), eliminating the resolution jump
            # at the trigger point.
            if recording_stream_path:
                self._ensure_rec_prebuffer_worker(camera_key, recording_stream_path, self.prebuffer_window_seconds(config), camera_id=camera_id)
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
        detection_stream_url: str | None = None,
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

        # A configured pre-roll of 0 must NOT skip the (always-running) rolling
        # prebuffer: doing so rendered the clip live from the trigger forward,
        # missing the event and capturing the aftermath. Floor the pre-roll so
        # the buffered footage spanning the trigger is rendered instead. The
        # no-segments / ffmpeg-missing / degenerate-render branches below still
        # hand off to ``_live_capture`` when the buffer has nothing usable.
        if pre_seconds <= 0:
            pre_seconds = self.RTSP_EVENT_MIN_PRE_SECONDS

        # Use the same window the priming path computed, so re-ensuring the worker
        # here never restarts it mid-capture over a mismatched buffer size.
        if buffer_seconds is None:
            buffer_seconds = self.prebuffer_window_seconds()
        buffer_seconds = max(int(buffer_seconds), pre_seconds + post_seconds + 5, pre_seconds + 10, 15)
        camera_key = self._camera_key(camera_id)
        # Keep the shared detection/sound ingest on the primary stream. When
        # ``stream_url`` is the optional high-resolution recording stream, it
        # must never replace that worker or cause URL ping-pong with the live
        # monitor. The recording stream has its own video-only prebuffer below.
        ingest_stream_url = detection_stream_url or stream_url
        self._ensure_prebuffer_worker(camera_key, ingest_stream_url, buffer_seconds, camera_id=camera_id)
        has_dedicated_recording_stream = bool(
            detection_stream_url and detection_stream_url != stream_url
        )

        # Check for ffmpeg and pre-event segments BEFORE sleeping so that if we
        # must fall back to a live capture it starts now (at trigger time) rather
        # than post_seconds later - giving the clip a meaningful timestamp.
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self._emit_diagnostic(
                camera_id,
                'prebuffer_fallback',
                'ffmpeg is not installed, so the pre-event buffer could not be rendered and the clip was captured live.',
                severity='warning',
                details={'reason': 'ffmpeg_missing'},
            )
            return self._live_capture(stream_url, file_path, max_duration_seconds)

        # Prefer high-res recording prebuffer segments when available so
        # the entire clip (pre-roll + post) renders at full resolution.
        rec_segments, _ = self._collect_rec_prebuffer_segments(
            camera_key,
            triggered_at.timestamp() - pre_seconds,
            triggered_at.timestamp(),
        )
        pre_only_segments = rec_segments
        # If a dedicated recording stream is configured, do not silently use
        # lower-resolution detection footage when its high-res buffer is still
        # warming up. The direct-capture fallback below uses ``stream_url``
        # (the recording stream), preserving the requested recording quality.
        if not pre_only_segments and not has_dedicated_recording_stream:
            pre_only_segments, _ = self._collect_prebuffer_segments(
                camera_key,
                triggered_at.timestamp() - pre_seconds,
                triggered_at.timestamp(),
            )
        if not pre_only_segments:
            logger.info('No prebuffer segments available for %s; falling back to direct RTSP clip capture.', camera_key)
            self._emit_diagnostic(
                camera_id,
                'prebuffer_fallback',
                'No pre-event buffer was available, so the clip was captured live from the trigger forward - '
                'the moments before the trigger are missing.',
                severity='warning',
                details={'reason': 'no_segments'},
            )
            return self._live_capture(stream_url, file_path, max_duration_seconds)

        end_capture_at = triggered_at.timestamp() + post_seconds
        delay = end_capture_at - time.time()
        if delay > 0:
            time.sleep(delay)

        start_ts = triggered_at.timestamp() - pre_seconds
        end_ts = end_capture_at
        # Prefer high-res recording prebuffer segments for the full render window.
        segments, content_start_ts = self._collect_rec_prebuffer_segments(camera_key, start_ts, end_ts)
        if not segments and not has_dedicated_recording_stream:
            segments, content_start_ts = self._collect_prebuffer_segments(camera_key, start_ts, end_ts)
        if not segments:
            logger.info('No prebuffer segments available for %s after waiting; falling back to direct RTSP clip capture.', camera_key)
            self._emit_diagnostic(
                camera_id,
                'prebuffer_fallback',
                'Pre-event buffer segments disappeared before rendering; falling back to live capture.',
                severity='warning',
                details={'reason': 'segments_expired'},
            )
            return self._live_capture(stream_url, file_path, max_duration_seconds)

        if content_start_ts is None:
            content_start_ts = start_ts
        # Surface a partial pre-roll. A total buffer miss already emits a
        # ``prebuffer_fallback`` above, but a buffer that holds SOME footage just
        # not reaching all the way back to the requested start renders fine and
        # was previously silent - producing a mysteriously short clip (e.g. 5s of
        # pre-roll instead of 10s). Flag it so operators can tell a short clip
        # apart from a real problem. Only fires when the shortfall exceeds the
        # jitter tolerance; keyframe lead-in (content_start_ts <= start_ts) never
        # trips it.
        preroll_shortfall = content_start_ts - start_ts
        if preroll_shortfall > self.PREBUFFER_SHORT_PREROLL_TOLERANCE_SECONDS:
            actual_pre_seconds = max(0.0, triggered_at.timestamp() - content_start_ts)
            self._emit_diagnostic(
                camera_id,
                'prebuffer_short_preroll',
                f'Only ~{actual_pre_seconds:.0f}s of the requested {pre_seconds}s pre-event buffer was '
                f'available, so this clip is about {preroll_shortfall:.0f}s shorter at the start. The '
                'rolling buffer had not filled yet - this is normal right after saving recording settings, '
                'a camera reconnect, or the camera first coming online, and recovers once the buffer refills.',
                severity='warning',
                details={
                    'reason': 'buffer_not_full',
                    'requested_pre_seconds': pre_seconds,
                    'actual_pre_seconds': round(actual_pre_seconds, 1),
                    'shortfall_seconds': round(preroll_shortfall, 1),
                    'segment_count': len(segments),
                },
            )
        # Render exactly the footage between where the first selected segment
        # starts and the capture deadline. The keyframe-aligned lead before
        # start_ts is kept (and reported via content_start_ts) rather than
        # silently eating the same amount off the end of the clip.
        content_seconds = max(1.0, min(end_ts - content_start_ts, max_duration_seconds + 10.0))

        list_path = file_path.with_name(f'{file_path.stem}.concat.txt')
        tmp_path = file_path.with_name(f'{file_path.stem}.prebuffer.tmp{file_path.suffix}')
        segment_durations = self._prebuffer_segment_durations(camera_key, segments)
        list_content = ''.join(self._concat_file_line(segment, segment_durations.get(segment)) for segment in segments)
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
            '-an',
            # The rolling prebuffer segments are already browser-oriented H.264
            # video. Remux it instead of decoding/re-encoding: it is much
            # cheaper on small boards and preserves recoverable video packets
            # from imperfect RTSP segments.
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
                'Pre-event buffer could not be rendered, so the clip was captured live from the trigger forward - '
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

        effective_seconds = rendered_seconds if rendered_seconds is not None else content_seconds
        self._mux_prebuffer_audio(camera_key, tmp_path, content_start_ts, effective_seconds)
        tmp_path.replace(file_path)
        # Report the clip's real duration, not the requested window - keyframe
        # alignment and short source footage make them differ, and a mismatch
        # shows up as playback that ends well before the stated length.
        return content_start_ts, effective_seconds

    @staticmethod
    def _camera_key(camera_id: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_-]+', '-', str(camera_id or '').strip().lower()).strip('-') or 'camera'

    # Marker file written when the per-camera ingest learns the RTSP stream
    # has no audio track. The worker checks it on startup to skip the audio
    # output, ``audio_segments_after`` raises a clean RuntimeError when
    # consumers ask for audio on a marked camera, and
    # ``_ensure_prebuffer_worker`` clears it before probing a new stream URL.
    NO_AUDIO_MARKER_FILENAME = '.no_audio'
    # Substring to detect in ffmpeg stderr when an output target has no
    # mapped streams. ffmpeg's canonical message is
    # "Output file #N does not contain any stream\n" (see
    # libavformat/output.c av_log(FATAL, "Output file #%d …")).
    NO_AUDIO_FFMPEG_SUBSTRING = 'does not contain any stream'
    # Human-friendly prefix used in RuntimeError messages surfaced to
    # consumers (sound detector, external tests). Kept distinct from the
    # ffmpeg substring above so a test that simulates ffmpeg's exact text
    # can match the worker branch independently of the consumer branch.
    NO_AUDIO_EXC_PREFIX = 'no audio track in stream'

    def _audio_disabled_marker(self, camera_key: str) -> Path:
        return self.audio_dir / camera_key / self.NO_AUDIO_MARKER_FILENAME

    @staticmethod
    def _concat_file_line(file_path: Path, duration_seconds: float | None = None) -> str:
        """Return an ffmpeg concat-demuxer file line for this path.

        The concat demuxer parses backslashes as escapes and resolves relative
        paths from the list file location. Windows paths like
        ``data\\recordings\\.prebuffer\\...`` can therefore point at the wrong
        file or fail to parse, causing otherwise healthy prebuffer segments to
        fall back to late direct RTSP capture.
        """
        escaped = file_path.resolve().as_posix().replace("'", r"'\''")
        line = f"file '{escaped}'\n"
        if duration_seconds is not None and duration_seconds > 0:
            line += f"duration {duration_seconds:.6f}\n"
        return line

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
        old_url: str | None = None
        with self._prebuffer_lock:
            existing = self._prebuffer_workers.get(camera_key)
            if existing and existing.get('stream_url') == stream_url:
                thread = existing.get('thread')
                if isinstance(thread, threading.Thread) and thread.is_alive():
                    existing['buffer_seconds'] = int(buffer_seconds)
                    return
            if existing:
                existing_thread = existing.get('thread')
                if isinstance(existing_thread, threading.Thread) and existing_thread.is_alive():
                    # Replacing a LIVE worker discards its rolling buffer. This is
                    # expected once after a settings change, but if it keeps
                    # happening the buffer never fills and events render as near-
                    # still clips - so surface why, to catch config churn / collisions.
                    if existing.get('stream_url') != stream_url:
                        old_url = str(existing.get('stream_url') or '')
                        restart_reason = 'stream_url_changed'
                        # Debounce: when two callers alternate different URLs they
                        # restart each other on every cycle, and the rolling buffer
                        # never fills. Suppress the restart (and keep the current
                        # worker running) when one was already performed within the
                        # debounce window for this camera. A genuine admin-saved
                        # URL change still takes effect once the window expires.
                        now = time.monotonic()
                        last = self._prebuffer_last_restart.get(camera_key, 0)
                        if now - last < self.PREBUFFER_RESTART_DEBOUNCE_SECONDS:
                            # Update buffer_seconds on the existing worker so the
                            # window size stays current even while we suppress.
                            existing['buffer_seconds'] = int(buffer_seconds)
                            return
            # Bug 2 fix: join the OLD worker's thread (via ``_stop_worker``)
            # BEFORE clearing the marker or starting a replacement worker.
            # Without joining, two ffmpegs race over the same rolling-buffer
            # directories (``frames``/``latest.jpg``, ``.audio``/``*.wav``,
            # ``.prebuffer``/``segment-*.mp4``), and the old worker's
            # ``finally``-block pruner can silently delete freshly-written
            # segments from the new worker - destroying event pre-roll footage
            # immediately after every restart. Joining also closes the
            # URL-change marker-write-back race (P1 from earlier review): the
            # old worker has fully exited by the time we clear the .no_audio
            # marker, so it can't re-touch the file after the unlink and lock
            # the new URL into permanent video-only mode. We hold the lock
            # across the join so concurrent ``_ensure_prebuffer_worker`` calls
            # cannot race in between ``stop_event.set`` and the new
            # ``thread.start``. The join timeout uses
            # ``PREBUFFER_WORKER_JOIN_TIMEOUT_SECONDS`` to cover the worker's
            # worst-case ffmpeg shutdown (1s loop sleep + up to 2s
            # SIGTERM/terminate wait + SIGKILL); if a hung worker exceeds it,
            # the replacement is still started (denying camera ingest would
            # be worse than a brief two-ffmpeg overlap). The operator
            # diagnostic itself is emitted by ``_stop_worker`` which derives
            # ``worker_kind`` from the worker dict ('prebuffer' for the
            # rolling-buffer workers spawned here, 'continuous' for the
            # chunk workers) and fires
            # ``worker_stop_join_timeout_prebuffer`` (this path) or
            # ``worker_stop_join_timeout_continuous`` (the chunk-worker
            # equivalent) through the per-worker ``diagnostic_callback``
            # captured below. Behaviour covered by
            # ``tests/test_stop_join_timeout_diagnostic.py``.
            if existing:
                self._stop_worker(existing, join_timeout=self.PREBUFFER_WORKER_JOIN_TIMEOUT_SECONDS)
            if restart_reason == 'stream_url_changed':
                self._audio_disabled_marker(camera_key).unlink(missing_ok=True)

            stop_event = threading.Event()
            worker_state = {
                'stop_event': stop_event,
                'stream_url': stream_url,
                'buffer_seconds': int(buffer_seconds),
                'camera_id': camera_id or camera_key,
                # Captured at construction so `_stop_worker` can emit a
                # useful ``worker_stop_join_timeout_prebuffer`` diagnostic
                # later (Bug 5). The callback is set once at startup in
                # main.py and is not hot-reloaded, so capturing it here is
                # safe; if a future hot-reload path changes it on a live
                # service, the next ``_ensure_prebuffer_worker`` call will
                # record the new reference in the dict.
                'diagnostic_callback': self.diagnostic_callback,
            }
            thread = threading.Thread(
                target=self._run_prebuffer_worker,
                args=(camera_key, stream_url, worker_state),
                name=f'prebuffer-{camera_key}',
                daemon=True,
            )
            worker_state['thread'] = thread
            self._prebuffer_workers[camera_key] = worker_state
            thread.start()
        if restart_reason:
            url_detail = self._url_diff_summary(old_url or '', stream_url) if old_url else ''
            logger.info(
                'Prebuffer worker for %s restarted (%s)%s; rolling buffer was reset.',
                camera_key,
                restart_reason,
                f': {url_detail}' if url_detail else '',
            )
            with self._prebuffer_lock:
                self._prebuffer_last_restart[camera_key] = time.monotonic()
            self._emit_diagnostic(
                camera_id or camera_key,
                'prebuffer_restart',
                f'Pre-event buffer worker restarted ({restart_reason}); the rolling buffer was reset. '
                'Frequent restarts leave events without pre-roll footage.',
                severity='warning',
                details={'reason': restart_reason, 'camera_key': camera_key, 'url_diff': url_detail} if url_detail else {'reason': restart_reason, 'camera_key': camera_key},
            )

    def _run_prebuffer_worker(self, camera_key: str, stream_url: str, worker_state: dict[str, Any]) -> None:
        stop_event = worker_state.get('stop_event')
        if not isinstance(stop_event, threading.Event):
            stop_event = threading.Event()
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            logger.warning('ffmpeg is required for rolling prebuffer but is not installed.')
            return
        camera_dir = self.prebuffer_dir / camera_key
        camera_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = camera_dir / 'segment-%Y%m%dT%H%M%S.mp4'
        # Sidecar outputs for the other consumers of this single connection.
        frames_dir = self.frames_dir / camera_key
        frames_dir.mkdir(parents=True, exist_ok=True)
        latest_frame_path = frames_dir / 'latest.jpg'
        audio_camera_dir = self.audio_dir / camera_key
        audio_camera_dir.mkdir(parents=True, exist_ok=True)
        audio_pattern = audio_camera_dir / 'aud-%Y%m%dT%H%M%S.wav'
        # Sweep stderr logs orphaned by a previous hard kill (SIGKILL/OOM): the
        # normal exit path unlinks them in `finally`, but a non-graceful death
        # leaves them behind and the segment pruner never touches them.
        self._prune_stale_ingest_logs()

        # If a previous worker for this stream learned it has no audio track,
        # skip the audio output from the first iteration rather than rebuilding
        # and tearing it down just to immediately write the no-audio marker.
        # ``just_disabled_audio`` flags the next iteration as a recovery hop so
        # the worker can respawn video-only ffmpeg WITHOUT the normal 1-5s
        # backoff - otherwise a 4-fps camera would still get one audio-disabled
        # attempt every few seconds forever even after the stream has stabilized.
        audio_enabled = not self._audio_disabled_marker(camera_key).exists()
        just_disabled_audio = False

        while not stop_event.is_set():
            from app.config_facades import effective_live_config as _elc  # noqa: PLC0415
            _live_config = _elc()
            _ingest_fps = int(_live_config.get('ingest_frame_fps', self.INGEST_FRAME_FPS))
            _snapshot_quality = int(_live_config.get('snapshot_quality', self.SNAPSHOT_QUALITY))
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
                # Output 1: rolling fragmented-MP4 video segments for event clips.
                '-map',
                '0:v:0',
                '-c:v',
                'copy',
                '-an',
                '-f',
                'segment',
                '-segment_time',
                str(self.PREBUFFER_SEGMENT_SECONDS),
                '-segment_format',
                'mp4',
                '-segment_format_options',
                'movflags=+frag_keyframe+empty_moov+default_base_moof',
                '-strftime',
                '1',
                str(output_pattern),
                # Output 2: latest decoded frame for object detection + snapshots.
                # Written to a temp name then atomically renamed so a reader never
                # sees a half-written JPEG. -update overwrites the same target.
                '-map',
                '0:v:0',
                '-vf',
                f'fps={_ingest_fps}',
                # JPEG quality on mjpeg's 2-31 scale (lower=better) so the live
                # snapshot matches the old OpenCV-encoded quality; ffmpeg's mjpeg
                # default is noticeably more compressed. Operator-overridable via
                # the Detection & Live "Snapshot Quality" setting.
                '-q:v',
                str(_snapshot_quality),
                '-update',
                '1',
                '-atomic_writing',
                '1',
                '-f',
                'image2',
                '-y',
                str(latest_frame_path),
            ]
            if audio_enabled:
                command += [
                    # Output 3: 1s mono 16 kHz PCM-WAV segments for sound detection.
                    # Only included when the worker has confirmed the stream
                    # carries audio; on a video-only camera ffmpeg refuses with
                    # "Output file does not contain any stream" and the entire
                    # ingest crashes (the optional ``?`` on ``-map 0:a:0?``
                    # makes only the *map* optional, never the output itself).
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
            ffmpeg_started_at = time.time()
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr_file)
            stderr_file.close()
            try:
                last_segment_ts = time.time()
                while process.poll() is None and not stop_event.is_set():
                    keep_seconds = int(worker_state.get('buffer_seconds') or 15)
                    self._prune_prebuffer_segments(camera_dir, keep_seconds)
                    self._prune_audio_segments(audio_camera_dir, keep_seconds)
                    # Dead-stream detection: if the camera stops sending data
                    # ffmpeg can hang indefinitely. If no new segment has been
                    # written within several segment intervals, kill and restart.
                    try:
                        newest = max(
                            (p.stat().st_mtime for p in camera_dir.glob(self.PREBUFFER_SEGMENT_GLOB)),
                            default=last_segment_ts,
                        )
                        if newest > last_segment_ts:
                            last_segment_ts = newest
                    except OSError:
                        pass
                    stall_seconds = max(self.PREBUFFER_SEGMENT_SECONDS * 5, 20)
                    if time.time() - last_segment_ts > stall_seconds:
                        logger.info('Prebuffer ingest for %s stalled (no segment in %.0fs); restarting.', camera_key, stall_seconds)
                        # SIGKILL rather than SIGTERM: the stream is already dead so
                        # graceful cleanup is pointless, and ffmpeg can segfault in its
                        # RTSP teardown path when the connection is in a broken state.
                        process.kill()
                        break
                    time.sleep(1)
            finally:
                return_code = process.poll()
                if return_code is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return_code = process.poll()
                try:
                    stderr_content = stderr_path.read_text(encoding='utf-8', errors='replace')
                    if stderr_content.strip():
                        logger.debug('Prebuffer ffmpeg %s: %s', camera_key, stderr_content.strip()[:1000])
                except OSError:
                    stderr_content = ''
                # ffmpeg aborts with "Output file does not contain any stream"
                # when an output target has no mapped streams - on a video-only
                # camera that means the audio-WAV output. Detect it from the
                # captured stderr, drop the audio output for the rest of this
                # worker's lifetime, and persist the decision in a marker file
                # so a restarted worker (same URL) skips the doomed probe.
                if (
                    audio_enabled
                    and not stop_event.is_set()
                    and return_code not in (None, 0)
                    and self.NO_AUDIO_FFMPEG_SUBSTRING in (stderr_content or '')
                ):
                    audio_enabled = False
                    just_disabled_audio = True
                    self._audio_disabled_marker(camera_key).touch()
                    self._emit_diagnostic(
                        worker_state.get('camera_id') or camera_key,
                        'ingest_audio_disabled',
                        'Camera ingest has switched to video-only mode because '
                        'the RTSP stream has no audio track. Sound detection '
                        'will be unavailable for this camera until the stream '
                        'URL changes.',
                        severity='info',
                        details={'reason': 'no_audio_stream'},
                    )
                try:
                    stderr_path.unlink(missing_ok=True)
                except OSError:
                    # The ffmpeg process or another worker may still hold the log
                    # file briefly on Windows. Missing or locked logs are not
                    # fatal; they will be re-created or cleaned up later.
                    pass
                keep_seconds = int(worker_state.get('buffer_seconds') or 15)
                self._prune_prebuffer_segments(camera_dir, keep_seconds)
                self._prune_audio_segments(audio_camera_dir, keep_seconds)
                # Emit a diagnostic when ffmpeg exits unexpectedly (not via stop_event)
                # so operators can see frequent restarts that would leave the prebuffer
                # empty at event time.
                if not stop_event.is_set() and return_code not in (None, 0):
                    uptime = time.time() - ffmpeg_started_at
                    camera_id = worker_state.get('camera_id') or camera_key
                    stderr_tail = self.redact_stream_credentials((stderr_content or '')[-500:])
                    self._emit_diagnostic(
                        camera_id,
                        'ingest_restart',
                        f'Camera ingest process exited (code {return_code}) after {uptime:.0f}s - '
                        'reconnecting. If this happens frequently the pre-event buffer may be empty '
                        'when recordings are triggered.',
                        severity='warning',
                        details={
                            'return_code': return_code,
                            'uptime_seconds': round(uptime, 1),
                            'stderr_tail': stderr_tail,
                        },
                    )
            if not stop_event.is_set():
                # Skip the backoff on the recovery iteration so a worker that
                # just lost its audio output reconnects video-only immediately
                # rather than after 1-5s of doing nothing.
                if just_disabled_audio:
                    just_disabled_audio = False
                else:
                    run_seconds = time.time() - ffmpeg_started_at
                    time.sleep(5 if run_seconds < 60 else 1)

    # ── High-res recording prebuffer ──────────────────────────────────
    # When a camera exposes dual streams (sub-stream for detection,
    # main stream for recording), a parallel prebuffer captures
    # video-only segments from the main stream so event clips render
    # at full resolution throughout (pre-roll + post).

    def _ensure_rec_prebuffer_worker(self, camera_key: str, stream_url: str, buffer_seconds: int, camera_id: str | None = None) -> None:
        with self._rec_prebuffer_lock:
            existing = self._rec_prebuffer_workers.get(camera_key)
            if existing and existing.get('stream_url') == stream_url:
                thread = existing.get('thread')
                if isinstance(thread, threading.Thread) and thread.is_alive():
                    existing['buffer_seconds'] = int(buffer_seconds)
                    return
            if existing:
                self._stop_worker(existing, join_timeout=self.PREBUFFER_WORKER_JOIN_TIMEOUT_SECONDS)
            stop_event = threading.Event()
            worker_state = {
                'stop_event': stop_event,
                'stream_url': stream_url,
                'buffer_seconds': int(buffer_seconds),
                'camera_id': camera_id or camera_key,
                'diagnostic_callback': self.diagnostic_callback,
            }
            thread = threading.Thread(
                target=self._run_rec_prebuffer_worker,
                args=(camera_key, stream_url, worker_state),
                name=f'rec-prebuffer-{camera_key}',
                daemon=True,
            )
            worker_state['thread'] = thread
            self._rec_prebuffer_workers[camera_key] = worker_state
            thread.start()

    def _run_rec_prebuffer_worker(self, camera_key: str, stream_url: str, worker_state: dict[str, Any]) -> None:
        """Video-only prebuffer worker for the high-res recording stream.

        Captures rolling fragmented-MP4 segments from the recording stream
        into a separate directory so ``write_rtsp_clip_with_prebuffer`` can
        render event clips at full resolution without a resolution jump.
        """
        stop_event = worker_state.get('stop_event')
        if not isinstance(stop_event, threading.Event):
            stop_event = threading.Event()
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            return
        camera_dir = self.prebuffer_dir / f'{camera_key}-rec'
        camera_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = camera_dir / 'segment-%Y%m%dT%H%M%S.mp4'
        while not stop_event.is_set():
            command = [
                ffmpeg,
                '-nostdin',
                '-hide_banner',
                '-loglevel', 'error',
                '-rtsp_transport', 'tcp',
                '-err_detect', 'ignore_err',
                '-i', stream_url,
                '-map', '0:v:0',
                '-c:v', 'copy',
                '-an',
                '-f', 'segment',
                '-segment_time', str(self.PREBUFFER_SEGMENT_SECONDS),
                '-segment_format', 'mp4',
                '-segment_format_options', 'movflags=+frag_keyframe+empty_moov+default_base_moof',
                '-strftime', '1',
                str(output_pattern),
            ]
            stderr_file = tempfile.NamedTemporaryFile(mode='w+', suffix='.log', delete=False, dir=str(self.prebuffer_dir))
            stderr_path = Path(stderr_file.name)
            ffmpeg_started_at = time.time()
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr_file)
            stderr_file.close()
            try:
                last_segment_ts = time.time()
                while process.poll() is None and not stop_event.is_set():
                    keep_seconds = int(worker_state.get('buffer_seconds') or 15)
                    self._prune_prebuffer_segments(camera_dir, keep_seconds)
                    try:
                        newest = max(
                            (p.stat().st_mtime for p in camera_dir.glob(self.PREBUFFER_SEGMENT_GLOB)),
                            default=last_segment_ts,
                        )
                        if newest > last_segment_ts:
                            last_segment_ts = newest
                    except OSError:
                        pass
                    stall_seconds = max(self.PREBUFFER_SEGMENT_SECONDS * 5, 20)
                    if time.time() - last_segment_ts > stall_seconds:
                        process.kill()
                        break
                    time.sleep(1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                try:
                    stderr_path.unlink(missing_ok=True)
                except OSError:
                    # The ffmpeg process or another worker may still hold the log
                    # file briefly on Windows. Missing or locked logs are not
                    # fatal; they will be re-created or cleaned up later.
                    pass
                keep_seconds = int(worker_state.get('buffer_seconds') or 15)
                self._prune_prebuffer_segments(camera_dir, keep_seconds)
            if not stop_event.is_set():
                run_seconds = time.time() - ffmpeg_started_at
                time.sleep(5 if run_seconds < 60 else 1)

    def rec_prebuffer_segments_dir(self, camera_id: str) -> Path:
        """Return the directory holding high-res recording prebuffer segments."""
        return self.prebuffer_dir / f'{self._camera_key(camera_id)}-rec'

    def _collect_rec_prebuffer_segments(self, camera_key: str, start_ts: float, end_ts: float) -> tuple[list[Path], float | None]:
        """Collect high-res prebuffer segments overlapping [start_ts, end_ts]."""
        camera_dir = self.prebuffer_dir / f'{camera_key}-rec'
        if not camera_dir.exists():
            return [], None
        return self._collect_prebuffer_segments_from_dir(camera_dir, start_ts, end_ts)

    def _collect_prebuffer_segments_from_dir(self, camera_dir: Path, start_ts: float, end_ts: float) -> tuple[list[Path], float | None]:
        """Shared segment collector for any prebuffer directory.

        A segment's mtime marks when its content ENDS. Anchoring
        ``content_start_ts`` at the first selected segment's mtime therefore
        reported a start up to a full segment LATE (e.g. ~4s), which then
        dragged the rendered clip's ``-t`` window, the muxed audio delay and
        the baked detection track all late together - the dual-stream
        sound/video misalignment. Select by content overlap and report the
        first selected segment's content START, exactly like
        ``_collect_prebuffer_segments``.
        """
        timed = self._segment_timeline(camera_dir, self.PREBUFFER_SEGMENT_GLOB, self.PREBUFFER_SEGMENT_SECONDS)
        selected = [item for item in timed if item[2] > start_ts and item[1] < end_ts]
        if not selected:
            return [], None
        return [item[0] for item in selected], selected[0][1]

    def _prune_prebuffer_segments(self, camera_dir: Path, keep_seconds: int) -> None:
        cutoff = time.time() - max(keep_seconds, 5)
        for segment in list(camera_dir.glob(self.PREBUFFER_SEGMENT_GLOB)) + list(camera_dir.glob('segment-*.ts')):
            try:
                if segment.stat().st_mtime < cutoff:
                    segment.unlink(missing_ok=True)
            except OSError:
                continue

    def _prune_stale_ingest_logs(self, max_age_seconds: float = 300.0) -> None:
        cutoff = time.time() - max_age_seconds
        for log_path in self.prebuffer_dir.glob('*.log'):
            try:
                if log_path.stat().st_mtime < cutoff:
                    log_path.unlink(missing_ok=True)
            except OSError:
                continue

    def _prune_audio_segments(self, audio_camera_dir: Path, keep_seconds: int | None = None) -> None:
        # Retain audio for the SAME window as the video prebuffer (pre + max_clip)
        # so long event clips have real audio for their whole length, not just
        # the last AUDIO_SEGMENT_RETENTION_SECONDS. Audio shorter than the video
        # made the player's buffered bar stop early (buffered = where all tracks
        # exist). The constant is now just a floor.
        retain = max(self.AUDIO_SEGMENT_RETENTION_SECONDS, int(keep_seconds or 0), 5)
        cutoff = time.time() - retain
        for segment in audio_camera_dir.glob('aud-*.wav'):
            try:
                if segment.stat().st_mtime < cutoff:
                    segment.unlink(missing_ok=True)
            except OSError:
                continue

    # ── Shared-ingest accessors ──────────────────────────────────────
    def ingest_has_produced_frame(self, camera_id: str) -> bool:
        """True if the ingest has written at least one frame for this camera.
        Used to distinguish a warming-up ingest (file absent) from a stale or
        failed one (file present but old), so callers can avoid penalising cold
        starts the same way as genuine camera failures."""
        path = self.frames_dir / self._camera_key(camera_id) / 'latest.jpg'
        return path.exists()

    def latest_frame_jpeg(self, camera_id: str, *, max_age_seconds: float = 30.0) -> tuple[bytes, float] | None:
        """Most recent decoded frame the ingest wrote for this camera, as
        (jpeg_bytes, captured_ts). ``captured_ts`` is the file mtime - when
        ffmpeg wrote the frame - so detection samples and playback overlays stay
        aligned. Returns None when no fresh frame is available (ingest warming
        up, camera offline, or ffmpeg unavailable)."""
        path = self.frames_dir / self._camera_key(camera_id) / 'latest.jpg'
        # Open once and use fstat so the mtime and bytes come from the same
        # inode - eliminates the TOCTOU race between a separate stat() and
        # read_bytes() when ffmpeg atomically renames a new frame into place.
        # O_BINARY forces binary mode on Windows; without it os.open defaults to
        # text mode and reads stop at the first 0x1A (Ctrl-Z) byte, silently
        # truncating any JPEG stream that contains one early in its bytes.
        try:
            fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            mtime = st.st_mtime
            if max_age_seconds and (time.time() - mtime) > max_age_seconds:
                return None
            # Read in a loop: os.read() may return fewer bytes than requested
            # on some platforms even for regular files (e.g. EINTR on slow I/O).
            remaining = st.st_size
            if remaining <= 0:
                data = b''
            else:
                parts: list[bytes] = []
                while remaining > 0:
                    chunk = os.read(fd, min(remaining, 65536))
                    if not chunk:
                        break
                    parts.append(chunk)
                    remaining -= len(chunk)
                data = b''.join(parts)
        except OSError:
            return None
        finally:
            os.close(fd)
        if not data:
            return None
        return data, mtime

    def audio_segments_after(self, camera_id: str, after_ts: float) -> list[tuple[Path, float]]:
        """Audio WAV segments written strictly after ``after_ts``, oldest first,
        as (path, mtime). Lets the sound detector consume each 1s chunk once
        without reopening its own RTSP connection.

        Raises ``RuntimeError`` (``no audio track in stream``) when the
        per-camera ingest has already learned this RTSP stream has no audio
        track - the sound detector treats the prefix as a clean 'unavailable'
        signal rather than spinning on an empty queue.
        """
        audio_camera_dir = self.audio_dir / self._camera_key(camera_id)
        if not audio_camera_dir.exists():
            return []
        if self._audio_disabled_marker(self._camera_key(camera_id)).exists():
            raise RuntimeError(
                f'{self.NO_AUDIO_EXC_PREFIX}: {camera_id} (RTSP stream has no '
                f'audio; per-camera ingest is running video-only)'
            )
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

    @staticmethod
    def _segment_timeline(camera_dir: Path, glob_pattern: str, nominal_seconds: float) -> list[tuple[Path, float, float]]:
        """Return ``(path, content_start_ts, content_end_ts)`` for every segment
        in ``camera_dir``, oldest first.

        A segment's mtime marks when ffmpeg finished writing it - its content
        END. Its content START is the previous segment's content end while the
        stream is continuous (segments split on keyframes, so they can exceed
        their nominal length); after a gap (worker restart) it falls back to the
        nominal segment length.

        Each file is stat()'d exactly once inside a try/except: the rolling
        pruner deletes segments concurrently, so a check-then-stat would race and
        raise FileNotFoundError out of the sort. Missing files are skipped."""
        if not camera_dir.exists():
            return []
        stamped: list[tuple[Path, float]] = []
        for segment in camera_dir.glob(glob_pattern):
            try:
                stamped.append((segment, segment.stat().st_mtime))
            except OSError:
                continue
        stamped.sort(key=lambda item: item[1])
        timed: list[tuple[Path, float, float]] = []
        prev_end: float | None = None
        for segment, end in stamped:
            # A small timing variance is normal at keyframe boundaries, but a
            # gap materially larger than the segment's nominal duration means
            # the worker missed one or more files. Keep that gap in the timeline
            # instead of making later audio catch up to video.
            gap = end - prev_end if prev_end is not None else None
            start = prev_end if gap is not None and 0 < gap <= nominal_seconds * 1.5 else end - nominal_seconds
            timed.append((segment, start, end))
            prev_end = end
        return timed

    def _collect_prebuffer_segments(self, camera_key: str, start_ts: float, end_ts: float) -> tuple[list[Path], float | None]:
        """Return the segments whose footage overlaps [start_ts, end_ts] plus
        the wall-clock timestamp where the first segment's content begins.

        Selecting by content overlap keeps footage from before the requested
        window out of the clip, and the returned start lets the caller align
        stored timing and the detection track with what the rendered video
        actually shows. Delegates to the shared collector so the primary and
        high-res recording paths can never drift apart again (the mtime-anchor
        drift that broke dual-stream A/V sync)."""
        return self._collect_prebuffer_segments_from_dir(self.prebuffer_dir / camera_key, start_ts, end_ts)

    def _prebuffer_segment_durations(self, camera_key: str, segments: list[Path]) -> dict[Path, float]:
        """Return real durations for selected primary or recording segments.

        The high-resolution worker stores files under ``<camera>-rec`` while
        the shared detection ingest stores them under ``<camera>``. Scanning
        only the primary directory loses ``duration`` directives for recording
        segments, which makes concat timing depend on muxer defaults and can
        shorten or stretch high-resolution event clips.
        """
        wanted = {segment.resolve() for segment in segments}
        directories = {
            segment.parent.resolve()
            for segment in segments
            if segment.parent.exists()
        }
        if not directories:
            directories = {(self.prebuffer_dir / camera_key).resolve()}
        durations: dict[Path, float] = {}
        for directory in directories:
            timed = self._segment_timeline(directory, self.PREBUFFER_SEGMENT_GLOB, self.PREBUFFER_SEGMENT_SECONDS)
            for segment, start, end in timed:
                if segment.resolve() in wanted:
                    duration = max(0.001, end - start)
                    # Keep both spellings available: callers usually pass the
                    # absolute paths returned by the collector, but preserving
                    # the original spelling makes this helper safe for tests or
                    # future callers that provide relative Path objects.
                    durations[segment] = duration
                    durations[segment.resolve()] = duration
        return durations

    def _collect_audio_segments(self, camera_key: str, start_ts: float, end_ts: float) -> list[Path]:
        timed = self._segment_timeline(self.audio_dir / camera_key, 'aud-*.wav', 1.0)
        return [segment for segment, start, end in timed if end > start_ts and start < end_ts]

    def _mux_prebuffer_audio(self, camera_key: str, video_path: Path, start_ts: float, duration_seconds: float) -> bool:
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg or duration_seconds <= 0:
            return False
        # The video render starts at the first selected video's content timestamp,
        # while the first selected WAV can begin slightly before or after it (the
        # two segmenters have different durations). Concatenating the WAVs from
        # time zero therefore introduces up to a segment of audible offset. Keep
        # the wall-clock position of the first audio segment and explicitly trim
        # or delay the audio so its first sample lines up with video time zero.
        audio_timed = self._segment_timeline(self.audio_dir / camera_key, 'aud-*.wav', 1.0)
        selected_audio = [
            item for item in audio_timed
            if item[2] > start_ts and item[1] < start_ts + duration_seconds
        ]
        if not selected_audio:
            return False
        muxed_path = video_path.with_name(f'{video_path.stem}.audio{video_path.suffix}')
        if muxed_path.exists():
            muxed_path.unlink(missing_ok=True)

        # Feed every WAV as its own input and delay it to its wall-clock position.
        # A concat demuxer would collapse any missing WAV segment, making audio
        # catch up to video after an ingest restart. Separate delayed inputs keep
        # both the initial offset and all gaps in the audio timeline.
        filter_parts: list[str] = []
        audio_labels: list[str] = []
        for index, (_segment, segment_start, _segment_end) in enumerate(selected_audio):
            delay_seconds = max(0.0, segment_start - start_ts)
            operations = []
            if index == 0 and segment_start < start_ts:
                # The first WAV overlaps the beginning of the video. Remove only
                # its leading overlap; dropping the whole segment would move audio
                # late. Later segments retain their absolute wall-clock delays.
                operations.append(f'atrim=start={start_ts - segment_start:.6f}')
            operations.append('asetpts=PTS-STARTPTS')
            if delay_seconds > 0.001:
                # adelay accepts integer milliseconds across supported FFmpeg builds.
                operations.append(f'adelay={max(1, round(delay_seconds * 1000))}:all=1')
            label = f'a{index}'
            filter_parts.append(f'[{index + 1}:a]' + ','.join(operations) + f'[{label}]')
            audio_labels.append(f'[{label}]')
        filter_parts.append(
            ''.join(audio_labels)
            + f'amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0:normalize=0'
            + ',aresample=async=1,apad[aout]'
        )
        filter_complex = ';'.join(filter_parts)

        command = [ffmpeg, '-y', '-i', str(video_path)]
        for segment, _segment_start, _segment_end in selected_audio:
            command.extend(['-i', str(segment)])
        command.extend([
            '-filter_complex',
            filter_complex,
            '-map',
            '0:v:0',
            '-map',
            '[aout]',
            '-c:v',
            'copy',
            '-c:a',
            'aac',
            '-b:a',
            '128k',
            # Keep the audio and video clocks aligned even when the camera's
            # audio/video packet clocks drift slightly. The filter preserves
            # gaps between sidecar segments, resamples small clock differences,
            # and pads trailing silence; -shortest/-t bound the output to video.
            '-shortest',
            '-t',
            f'{float(duration_seconds):.3f}',
            '-movflags',
            '+faststart',
            str(muxed_path),
        ])
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=max(30, int(duration_seconds) + 20), check=False)
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            result = None

        if result is None or result.returncode != 0 or not muxed_path.exists() or not self.clip_has_video_stream(muxed_path):
            muxed_path.unlink(missing_ok=True)
            if result is not None:
                logger.warning('Failed to mux prebuffer audio for %s; keeping silent video clip: %s', camera_key, self.redact_stream_credentials((result.stderr or '')[-500:]))
            return False
        muxed_path.replace(video_path)
        return True

    @staticmethod
    def grab_frame_from_url(stream_url: str, timeout_seconds: float = 8.0) -> bytes | None:
        """Grab a single JPEG frame from an RTSP stream URL using ffmpeg.

        Used by the Live page to show a preview of the recording stream
        so operators can verify the high-res stream is working. Captures
        one frame via ffmpeg and returns the JPEG bytes, or None on failure.
        """
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            return None
        from app.config_facades import effective_live_config as _elc  # noqa: PLC0415
        snapshot_quality = int(_elc().get('snapshot_quality', RecordingService.SNAPSHOT_QUALITY))
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            command = [
                ffmpeg,
                '-y',
                '-rtsp_transport', 'tcp',
                '-i', stream_url,
                '-vframes', '1',
                '-q:v', str(snapshot_quality),
                '-f', 'image2',
                str(tmp_path),
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            if result.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
                return tmp_path.read_bytes()
            return None
        except (subprocess.TimeoutExpired, OSError):
            return None
        finally:
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def redact_stream_credentials(message: str) -> str:
        return re.sub(r'(rtsps?://[^:\s/@]+):[^@]+@', r'\1:***@', message)

    @staticmethod
    def _url_diff_summary(old_url: str, new_url: str) -> str:
        """Return a short human-readable summary of what changed between two
        RTSP URLs, with credentials stripped. Returns 'credentials-only change'
        when they differ only in the credential portion.

        Uses a broader credential-strip regex than ``redact_stream_credentials``
        because the diff only needs to show host/port/path differences;
        ``redact_stream_credentials`` preserves the username portion for
        operator recognition in error messages."""
        a = re.sub(r'(rtsps?://)[^@]+@', r'\1<creds>@', old_url)
        b = re.sub(r'(rtsps?://)[^@]+@', r'\1<creds>@', new_url)
        if a == b:
            return '(credentials-only change)'
        return f'{a} -> {b}'

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
