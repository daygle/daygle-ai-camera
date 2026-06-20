"""Recording-extension cluster extracted from ``app/main.py`` (Phase-30).

This module owns the four helpers that previously lived inline on
``app/main.py``:

* ``extend_active_rtsp_recording(*, camera_id, event_time,
  recording_config=None, detections=None) -> int | None`` — extends
  an in-flight RTSP recording's capture deadline by the configured
  ``extension_step_seconds`` post-event horizon and updates the
  associated recording row (timing, labels, trigger).
* ``recording_track_sidecar_path(file_path: Path) -> Path`` —
  computes the ``.track.json`` sidecar path next to a recording file.
* ``write_recording_detection_track(file_path: Path, track:
  list[dict[str, Any]]) -> None`` — atomic-ish write of the
  sidecar JSON next to ``file_path``.
* ``load_recording_detection_track(file_path: Path) -> list[dict[str,
  Any]] | None`` — reads + parses the sidecar JSON if present,
  returning ``None`` on any missing-file / corrupt / non-detection
  shape.

The two pieces of exclusive state STAY on ``app.main`` (NOT moved
here, reachable via Pool C):

* ``app.main.active_rtsp_recordings`` — ``dict`` keyed by camera_id,
  holding ``{recording_id, capture_deadline_ts,
  max_capture_deadline_ts, start_capture_ts}`` per in-flight session.
* ``app.main.active_rtsp_recordings_lock`` — ``threading.Lock``
  guarding ``active_rtsp_recordings``.

Mirrors the Phase-26 (``live_detection_history`` /
``live_detection_history_lock``), Phase-27
(``_notification_threads_lock`` / ``_notification_threads``), and
Phase-29 (``live_detection_status_lock`` / ``live_detection_status``)
precedent for keeping state on ``app.main``.

**Pool-A rebind (in ``app/main.py``):** all four helpers are re-bound
as ``main.<orig_name>``. No underscore stripping needed — none of the
four are originally underscored.

**Pool-C reach sites used by this module (each resolved lazily via
``import app.main as main``):**

* ``main.active_rtsp_recordings`` + ``main.active_rtsp_recordings_lock``
  — state primitives (exclusive to ``extend_active_rtsp_recording``).
* ``main.database`` — the singleton DB handle, instantiated on
  ``app/main.py``. NOTE this is NOT a module-level export of
  ``app/database.py``; that module only exposes the ``EventDatabase``
  class.
* ``main.recording_service`` — the ``RecordingService`` singleton
  instantiated on ``app/main.py``. Used for ``should_record`` policy
  lookup.
* ``main.effective_recording_config`` — Phase-17 rebind from
  ``app.config_facades`` for the active recording config dict.
* ``main.detection_label_strings`` — Phase-29 rebind from
  ``app.detection_status`` for label dedup + sort.
* ``main.detection_label_confidences`` — Phase-29 rebind from
  ``app.detection_status`` for per-label max-confidence selection.

**Track trio Pool-C reach:** NONE. Pure helper group. Uses only
stdlib (``json``, ``pathlib.Path``, ``typing.Any``) and resolves
``recording_track_sidecar_path`` as a bare name inside the same
module.

**External callers that must keep working via Phase-30 rebind:**

* ``app/api/recordings_router.py`` — ``main.load_recording_detection_track``
  + ``main.recording_track_sidecar_path``.
* ``tests/test_api.py`` — extensive usage of all 4 helpers for
  recording-extension tests.
* ``web/app.js`` — comment reference (no functional dependency).

**Internal main.py body callers (use bare names after rebind):**

* ``extend_active_rtsp_recording(...)`` — called from L1149 (likely
  ``process_live_stream_alerts`` event-recording decision) and L1331
  (another recording-orchestration helper).
* ``recording_track_sidecar_path(...)`` — called from L1500 + the
  track trio internally.
* ``write_recording_detection_track(...)`` — called from L1589 (likely
  the main stream-loop detection track persistence).
* ``load_recording_detection_track(...)`` — called from various
  playback + recording-access sites.

**Generic-label-set duplication (recorded for follow-up):** the
``generic_labels = {'', 'motion', 'alert', 'human', 'object', 'none',
'off', 'continuous'}`` set inside ``extend_active_rtsp_recording`` is
near-duplicate of the ``generic`` set inside
``app.detection_status.detection_label_strings``. Per Phase-29's
reviewer-ship verdict, this is a known cosmetic duplication; defer to a
future shared-constants cleanup.

**Logger acquisition:** owns its own child logger via
``logging.getLogger('daygle.ai')`` (matches Phase-25/26/27/28/29
precedent; same logging tree).

**Default-arg safety:** no function-default expression evaluates
``main.<name>`` at module-load time. All defaults are scalars
(``None`` / ``[]``). Safe to import while main.py is partially loaded.

**Skipped helpers (NOT moved):** ``schedule_live_camera_backoff``
remains in ``main.py`` for future Phase-31 (related to Phase-27
event_debounce), as do the rest of the detection-status neighbors now
that Phase-29 has shipped.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import app.state as _state
from app.auth import utc_now
from app.config_facades import effective_recording_config
from app.detection_status import detection_label_confidences, detection_label_strings
from app.media_utils import recording_playback_sidecar_path

logger = logging.getLogger('daygle.ai')


def extend_active_rtsp_recording(
    *,
    camera_id: str,
    event_time: str,
    recording_config: dict[str, Any] | None = None,
    detections: list[dict[str, Any]] | None = None,
) -> int | None:
    try:
        event_dt = datetime.fromisoformat(str(event_time))
    except ValueError:
        event_dt = datetime.now(timezone.utc)
    if event_dt.tzinfo is None:
        event_dt = event_dt.replace(tzinfo=timezone.utc)
    config = recording_config or effective_recording_config()
    extension_step_seconds = max(
        0,
        int(config.get('extension_step_seconds', config.get('post_event_seconds', 10))),
    )
    extend_until = event_dt.timestamp() + extension_step_seconds
    with _state.active_rtsp_recordings_lock:
        session = _state.active_rtsp_recordings.get(camera_id)
        if not session:
            return None
        current_deadline = float(session.get('capture_deadline_ts') or 0)
        max_deadline = float(session.get('max_capture_deadline_ts') or current_deadline)
        new_deadline = min(max_deadline, max(current_deadline, extend_until))
        if new_deadline <= current_deadline:
            return int(session.get('recording_id'))
        session['capture_deadline_ts'] = new_deadline
        start_ts = float(session.get('start_capture_ts') or new_deadline)
        ended_at = datetime.fromtimestamp(new_deadline, tz=timezone.utc).isoformat()
        duration_seconds = max(1.0, new_deadline - start_ts)
        recording_id = int(session.get('recording_id'))
    _state.database.update_recording_timing(
        recording_id, ended_at=ended_at, duration_seconds=duration_seconds,
    )
    # Re-check that this recording is still the active one for this camera before
    # writing labels/trigger — a new capture may have started between lock release
    # and here, in which case these updates belong to a now-closed recording.
    with _state.active_rtsp_recordings_lock:
        current_session = _state.active_rtsp_recordings.get(camera_id)
        still_active = current_session is not None and int(current_session.get('recording_id', -1)) == recording_id
    if detections and still_active:
        should_record, trigger_type, trigger_label = _state.recording_service.should_record(
            detections, config,
        )
        new_labels = detection_label_strings(detections)
        if new_labels:
            _state.database.add_recording_labels(
                recording_id,
                new_labels,
                source='extension',
                confidences=detection_label_confidences(detections),
            )
        if should_record and trigger_label:
            current_recording = _state.database.get_recording(recording_id) or {}
            current_label = str(current_recording.get('trigger_label') or '').strip().lower()
            current_type = str(current_recording.get('trigger_type') or '').strip().lower()
            generic_labels = {
                '', 'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous',
            }
            candidate_label = str(trigger_label).strip().lower()
            if (
                candidate_label not in generic_labels
                and (current_label in generic_labels or current_type in {'motion', 'human'})
            ):
                _state.database.update_recording_trigger(
                    recording_id, trigger_type=trigger_type, trigger_label=candidate_label,
                )
    return recording_id


def recording_track_sidecar_path(file_path: Path) -> Path:
    return file_path.with_name(f'{file_path.stem}.track.json')


def write_recording_detection_track(
    file_path: Path,
    track: list[dict[str, Any]],
) -> None:
    recording_track_sidecar_path(file_path).write_text(
        json.dumps(track),
        encoding='utf-8',
    )


def load_recording_detection_track(
    file_path: Path,
) -> list[dict[str, Any]] | None:
    sidecar = recording_track_sidecar_path(file_path)
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    if not any((isinstance(sample, dict) and sample.get('detections') for sample in data)):
        return None
    return data


def write_live_history_detection_track(
    recording_id: int | None,
    file_path: Path,
    camera_id: str | None,
    start_ts: float,
    end_ts: float,
) -> bool:
    """Persist the monitor's detections over the capture window as the clip's track."""
    from app.detection_state import build_track_from_live_history
    if not str(file_path):
        return False
    track = build_track_from_live_history(camera_id, start_ts, end_ts)
    if track is None:
        logger.debug(
            'No live detection history covers recording %s (%s); no track written.',
            recording_id, file_path.name,
        )
        return False
    try:
        write_recording_detection_track(file_path, track)
    except OSError as exc:
        logger.warning('Could not write detection track for recording %s: %s', recording_id, exc)
        return False
    localized = sum(1 for sample in track if sample.get('detections'))
    logger.info(
        'Saved detection track for recording %s from live history (%d samples, %d with detections).',
        recording_id, len(track), localized,
    )
    return True


def _recording_capture_window(recording: dict[str, Any]) -> tuple[float, float] | None:
    """Return the recording's (start_ts, end_ts) from its stored timing."""
    try:
        started_at = datetime.fromisoformat(str(recording.get('started_at') or ''))
    except ValueError:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    start_ts = started_at.timestamp()
    try:
        ended_at = datetime.fromisoformat(str(recording.get('ended_at') or ''))
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=timezone.utc)
        end_ts = ended_at.timestamp()
    except ValueError:
        end_ts = start_ts + max(1.0, float(recording.get('duration_seconds') or 0))
    if end_ts <= start_ts:
        return None
    return (start_ts, end_ts)


def delete_recording_files(recordings: list[dict[str, Any]]) -> None:
    for recording in recordings:
        raw_file_path = str(recording.get('file_path') or '')
        file_path = Path(raw_file_path)
        if file_path.exists() and file_path.is_file():
            file_path.unlink(missing_ok=True)
        if raw_file_path:
            playback_paths = [
                recording_playback_sidecar_path(file_path),
                recording_track_sidecar_path(file_path),
                file_path.with_name(f'{file_path.stem}.playback.failed'),
                file_path.with_name(f'{file_path.stem}.h264.mp4'),
                file_path.with_name(f'{file_path.stem}.browser.mp4'),
                file_path.with_name(f'{file_path.stem}.playback.mp4'),
                file_path.with_name(f'{file_path.name}.meta.json'),
            ]
            for playback_path in playback_paths:
                if playback_path.exists() and playback_path.is_file():
                    playback_path.unlink(missing_ok=True)
        thumbnail_path = recording.get('thumbnail_path')
        if thumbnail_path:
            thumbnail = Path(str(thumbnail_path))
            if thumbnail.exists() and thumbnail.is_file():
                thumbnail.unlink(missing_ok=True)


def clear_runtime_media_directory(path_value: str | None) -> int:
    if not path_value:
        return 0
    path = Path(str(path_value))
    if not path.exists() or not path.is_dir():
        return 0
    deleted = 0
    for child in path.iterdir():
        try:
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
                deleted += 1
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                deleted += 1
        except OSError:
            continue
    return deleted


def recording_skip_reason(
    detections: list[dict[str, Any]],
    recording_config: dict[str, Any] | None = None,
) -> str:
    should_record, trigger_type, trigger_label = _state.recording_service.should_record(
        detections, recording_config,
    )
    if should_record:
        return (
            f"Recording policy matched {trigger_type}"
            f"{(f' {trigger_label}' if trigger_label else '')}, but no recording was linked."
        )
    labels = (
        ', '.join(str(d.get('label')) for d in detections if d.get('label')) or 'none'
    )
    return (
        f'Recording is waiting for an enabled alert rule to trigger for this camera.'
        f' Detected labels: {labels}.'
    )


def _parse_chunk_start_time(file_path: Path) -> datetime | None:
    stem = file_path.stem
    parts = stem.rsplit('_', 1)
    if len(parts) != 2:
        return None
    try:
        return datetime.strptime(parts[1], '%Y%m%dT%H%M%S').astimezone(timezone.utc)
    except ValueError:
        return None


def _make_continuous_chunk_callback(camera_id: str) -> Any:
    def on_chunk_complete(camera_key: str, file_path: Path) -> None:
        from app.backup import purge_recordings_by_policy
        try:
            started_at_dt = _parse_chunk_start_time(file_path)
            stat = file_path.stat()
            ended_at_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if started_at_dt is None:
                started_at_dt = ended_at_dt - timedelta(
                    seconds=effective_recording_config().get('chunk_duration_seconds', 3600)
                )
            duration_seconds = max(1.0, (ended_at_dt - started_at_dt).total_seconds())
            from app.detection_state import build_track_from_live_history
            chunk_track = build_track_from_live_history(
                camera_id, started_at_dt.timestamp(), ended_at_dt.timestamp(),
            )
            chunk_labels: list[str] = []
            chunk_confidences: dict[str, float] = {}
            if chunk_track:
                _seen: set[str] = set()
                for _sample in chunk_track:
                    for _det in _sample.get('detections') or []:
                        _lbl = str(_det.get('label') or '').strip().lower()
                        if not _lbl:
                            continue
                        if _lbl not in _seen:
                            _seen.add(_lbl)
                            chunk_labels.append(_lbl)
                        try:
                            _conf = float(_det.get('confidence'))
                        except (TypeError, ValueError):
                            _conf = None
                        if _conf is not None and (
                            _lbl not in chunk_confidences or _conf > chunk_confidences[_lbl]
                        ):
                            chunk_confidences[_lbl] = _conf
            recording_id = _state.database.add_recording(
                event_id=None, camera_id=camera_id,
                started_at=started_at_dt.isoformat(), ended_at=ended_at_dt.isoformat(),
                duration_seconds=duration_seconds, file_path=str(file_path),
                thumbnail_path=None, source='rtsp', created_at=utc_now(),
                trigger_type='continuous', trigger_label=None,
                labels=chunk_labels or None, label_confidences=chunk_confidences or None,
            )
            write_live_history_detection_track(
                recording_id, file_path, camera_id,
                started_at_dt.timestamp(), ended_at_dt.timestamp(),
            )
            purge_recordings_by_policy()
        except Exception as exc:
            logger.warning(
                'Failed to register continuous chunk %s for camera %s: %s',
                file_path.name, camera_id, exc,
            )
    return on_chunk_complete


def attach_event_recording(
    event_id: int,
    event_time: str,
    source: str,
    detections: list[dict[str, Any]],
    camera_id: str | None = None,
    recording_config: dict[str, Any] | None = None,
) -> int | None:
    from app.backup import purge_recordings_by_policy
    from app.utils import build_stream_url
    from app.config_facades import get_camera_config
    stream_url = ''
    if source == 'rtsp' and camera_id:
        stream_url = build_stream_url(get_camera_config(camera_id))
        extended_recording_id = extend_active_rtsp_recording(
            camera_id=camera_id, event_time=event_time,
            recording_config=recording_config, detections=detections,
        )
        if extended_recording_id is not None:
            return extended_recording_id
    metadata = _state.recording_service.event_recording_metadata(
        event_id, event_time, source, detections,
        write_clip=not stream_url, recording_config=recording_config,
    )
    if metadata is None:
        return None
    if camera_id:
        metadata['camera_id'] = camera_id
    detection_labels = detection_label_strings(detections)
    recording_id = _state.database.add_recording(
        created_at=utc_now(),
        labels=detection_labels or None,
        label_confidences=detection_label_confidences(detections) or None,
        **metadata,
    )
    if stream_url:
        start_rtsp_recording_capture(
            stream_url, metadata, event_id, detections,
            recording_id=recording_id, camera_id=camera_id,
            event_time=event_time, recording_config=recording_config,
        )
    else:
        window = _recording_capture_window(metadata)
        if window:
            write_live_history_detection_track(
                recording_id, Path(str(metadata.get('file_path') or '')),
                camera_id, window[0], window[1],
            )
    purge_recordings_by_policy()
    return recording_id


def start_rtsp_recording_capture(
    stream_url: str,
    metadata: dict[str, Any],
    event_id: int,
    detections: list[dict[str, Any]],
    *,
    recording_id: int,
    camera_id: str | None = None,
    event_time: str | None = None,
    recording_config: dict[str, Any] | None = None,
) -> None:
    from app.diagnostics import log_camera_diagnostic
    file_path = Path(str(metadata.get('file_path') or ''))
    duration_seconds = float(metadata.get('duration_seconds') or 1)
    trigger_type = str(metadata.get('trigger_type') or 'motion')
    trigger_label = metadata.get('trigger_label')
    pre_seconds = max(0, int((recording_config or {}).get('pre_event_seconds', 0)))
    post_seconds = max(0, int((recording_config or {}).get('post_event_seconds', 0)))
    try:
        triggered_at = datetime.fromisoformat(str(event_time or utc_now()))
    except ValueError:
        triggered_at = datetime.now(timezone.utc)
    if triggered_at.tzinfo is None:
        triggered_at = triggered_at.replace(tzinfo=timezone.utc)
    start_capture_ts = triggered_at.timestamp() - pre_seconds
    initial_deadline_ts = triggered_at.timestamp() + post_seconds
    max_clip_seconds = max(
        1, int((recording_config or effective_recording_config()).get('max_clip_seconds', 60))
    )
    max_deadline_ts = start_capture_ts + max(duration_seconds, float(max_clip_seconds))
    if camera_id:
        with _state.active_rtsp_recordings_lock:
            _state.active_rtsp_recordings[camera_id] = {
                'recording_id': recording_id,
                'start_capture_ts': start_capture_ts,
                'capture_deadline_ts': min(max_deadline_ts, initial_deadline_ts),
                'max_capture_deadline_ts': max_deadline_ts,
            }

    def write_generated_fallback() -> None:
        _state.recording_service.write_event_clip(
            file_path, event_id, detections, duration_seconds,
            trigger_type, str(trigger_label) if trigger_label else None,
        )

    def capture() -> None:
        try:
            final_deadline_ts = min(max_deadline_ts, initial_deadline_ts)
            if camera_id:
                while True:
                    with _state.active_rtsp_recordings_lock:
                        session = _state.active_rtsp_recordings.get(camera_id)
                        if not session or int(session.get('recording_id', -1)) != int(recording_id):
                            break
                        final_deadline_ts = float(
                            session.get('capture_deadline_ts') or final_deadline_ts
                        )
                    remaining = final_deadline_ts - time.time()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.5, max(0.05, remaining)))
            final_deadline_ts = min(final_deadline_ts, max_deadline_ts)
            actual_end_ts = min(max(time.time(), final_deadline_ts), max_deadline_ts)
            final_duration_seconds = max(1.0, actual_end_ts - start_capture_ts)
            dynamic_post_seconds = max(0, int(round(actual_end_ts - triggered_at.timestamp())))
            if camera_id and pre_seconds > 0:
                content_start_ts, content_seconds = (
                    _state.recording_service.write_rtsp_clip_with_prebuffer(
                        stream_url=stream_url, camera_id=camera_id, file_path=file_path,
                        triggered_at=triggered_at, pre_seconds=pre_seconds,
                        post_seconds=dynamic_post_seconds,
                        max_duration_seconds=final_duration_seconds,
                        buffer_seconds=_state.recording_service.prebuffer_window_seconds(
                            recording_config
                        ),
                    )
                )
            else:
                content_start_ts = time.time()
                _state.recording_service.write_rtsp_clip(stream_url, file_path, final_duration_seconds)
                content_seconds = final_duration_seconds
            _state.database.update_recording_timing(
                recording_id,
                started_at=datetime.fromtimestamp(content_start_ts, tz=timezone.utc).isoformat(),
                ended_at=datetime.fromtimestamp(
                    content_start_ts + content_seconds, tz=timezone.utc
                ).isoformat(),
                duration_seconds=content_seconds,
            )
            write_live_history_detection_track(
                recording_id, file_path, camera_id,
                content_start_ts, content_start_ts + content_seconds,
            )
        except Exception as exc:
            logger.warning(
                'RTSP recording capture failed for event %s, writing generated fallback: %s',
                event_id, exc,
            )
            log_camera_diagnostic(
                camera_id, 'capture_failed',
                f'RTSP recording capture failed; wrote a generated placeholder clip instead: {exc}',
                severity='error', details={'event_id': event_id, 'recording_id': recording_id},
            )
            write_generated_fallback()
            write_live_history_detection_track(
                recording_id, file_path, camera_id,
                start_capture_ts, start_capture_ts + duration_seconds,
            )
        finally:
            if camera_id:
                with _state.active_rtsp_recordings_lock:
                    session = _state.active_rtsp_recordings.get(camera_id)
                    if session and int(session.get('recording_id', -1)) == int(recording_id):
                        _state.active_rtsp_recordings.pop(camera_id, None)
    threading.Thread(target=capture, name=f'rtsp-recording-{event_id}', daemon=True).start()
