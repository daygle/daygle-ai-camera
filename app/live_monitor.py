"""Live-alert-monitor lifecycle and live-stream detection helpers.

Phase-32: lifecycle cluster (run_live_alert_monitor_once, live_alert_monitor_loop, etc.)
Phase-K: live-stream detection entry points (queue_live_stream_alerts,
         _encode_frame_jpeg, process_live_stream_alerts)
"""

from __future__ import annotations
import logging
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import app.state as _state
from app.ai_settings import ai_status_payload
from app.ai_verification import submit_alert_notification_with_verification, submit_event_description
from app.alert_dispatch import (
    _rule_notify_active_now,
    compute_minimum_rule_confidence,
)
from app.camera_health import _check_cameras_health
from app.camera_instance import read_ingest_frame
from app.config_facades import effective_ai_config, effective_email_alert_settings, effective_face_recognition_config, effective_live_config
from app.live_snapshot import build_event_thumbnail
from app.detection_state import (
    confirm_motion_detections,
    confirm_object_detections,
    detect_frame_motion,
    frame_motion_lock,
    shadow_inclusive_motion_mask,
    update_camera_motion,
    record_live_detection_history,
)
from app.detection_status import _camera_has_live_alert_stream, update_live_detection_status
from app.pipeline_timing import (
    STAGE_ALERTS,
    STAGE_BEHAVIOUR,
    STAGE_CONFIRMATION,
    STAGE_EVENT,
    STAGE_FACE_IDENTITY,
    STAGE_FACE_PASS,
    STAGE_FILTERING,
    STAGE_INFERENCE,
    STAGE_LOW_LIGHT,
    STAGE_MOTION,
    STAGE_REGION_BOOST,
    STAGE_SECOND_LOOK,
    STAGE_TILING,
    STAGE_TRACKING,
    STAGE_ZONE_RULES,
    StageTimer,
    prune_pipeline_timing,
    record_frame_read,
    record_pipeline_cycle,
)
from app.detection_telemetry import (
    MODE_ALWAYS_ON,
    MODE_ERROR,
    MODE_MOTION_GATED,
    MODE_SKIPPED_NO_DETECTOR,
    MODE_SKIPPED_NO_MOTION,
    REJECT_CAMERA_MOTION,
    REJECT_CAMERA_SCOPE,
    REJECT_CONFIRMATION,
    REJECT_MOTION_MODE,
    REJECT_ZONE,
    prune_detection_telemetry,
    record_detection_cycle,
)
from app.object_settings import (
    annotate_motion_states,
    effective_object_settings,
    filter_detections_by_motion_mode,
    motion_mode_for_label,
    motion_mode_label,
    object_detection_allowed_during_camera_motion,
    still_alert_thresholds,
    still_dwell_candidates,
    update_still_dwell_alerts,
)
from app.object_tracking import rebase_object_tracks, recent_track_boxes, update_object_tracks
from app.behaviour_monitor import (
    clear_behavioural_state,
    emit_activity_spike_anomalies,
    emit_loiter_anomalies,
    emit_time_of_day_anomalies,
    emit_tripwire_crossings,
    sync_behaviour_pause,
)
from app.recording_settings import effective_camera_live_settings
from app.inference_scheduler import LiveInferenceScheduler
from app.adaptive_cadence import get_adaptive_cadence
from app.face_identity import annotate_face_identities, face_identity_metadata, unknown_face_alerts
from app.face_detection_rules import effective_face_detection_rules, known_face_rules_for_camera
from app.region_detection import (
    detect_with_region_boost,
    detect_with_tiling,
    region_boost_enabled,
    tiling_grid,
)
from app.second_look import (
    confirm_borderline_detections,
    second_look_enabled,
    second_look_floor,
)
from app.low_light import low_light_detection_frame, normalize_low_light_mode
from app.ptz_tracking import update_auto_tracking
from app.camera_models import camera_detector
from app.detector import DetectorUnavailableError
from app.event_debounce import (
    clear_live_camera_backoff,
    schedule_live_camera_backoff,
)
from app.event_presence import absence_seconds, bind_presence_recording, drop_covered_motion, observe_presence
from app.overlap_suppression import drop_people_boxed_as_animals, overlap_reason
from app.recording_extension import (
    _make_continuous_chunk_callback,
    active_recording_id,
    attach_event_recording,
    continue_active_recording,
    extend_active_rtsp_recording,
    recording_skip_reason,
)
from app.backup import purge_camera_diagnostics_by_policy, purge_ai_diagnostics_by_policy
from app.utils import build_stream_url, normalize_bool_setting, normalize_ptz_motion_detection
from app.zone_schema import label_matches
from app.motion_object_priority import (
    DEFAULT_GRACE_SECONDS as MOTION_OBJECT_DEFAULT_GRACE_SECONDS,
    MAX_GRACE_SECONDS as MOTION_OBJECT_MAX_GRACE_SECONDS,
    object_zone_keys as motion_object_zone_keys,
)
from app.motion_levels import forget_camera as forget_motion_levels, motion_zone_levels
from app.zone_detection import (
    detection_matches_zone,
    filter_detections_for_camera,
    filter_motion_detections_by_objects,
    motion_zone_confirm_cycles,
    camera_watches_label,
    normalize_detection_boxes_for_frame,
    rule_rejection_reason,
    stamp_frame_aspect,
    zone_alert_detections,
    zone_detection_alert_rule_names,
    zone_motion_detections,
    zone_motion_record_on_detect,
    zone_name_for_detection,
    zone_object_alert_rules,
    zone_object_rule_matches,
    zone_record_on_detect,
)

logger = logging.getLogger('daygle.ai')


def _no_object_match_reason(
    detections: list[dict[str, Any]],
    raw_labels: list[str],
    monitored_zones: list[dict[str, Any]],
) -> str:
    """Explain why detections produced no zone-rule match, for the live status.

    Distinguishes the two very different cases the old catch-all "outside
    monitored zones" message conflated:
    - a detection is geometrically *inside* a monitored zone but no enabled
      object rule matched it (e.g. a full-frame zone with no enabled ``car``
      rule) -> name the object and point at the zone's Object Rules;
    - the detection is genuinely outside every zone area;
    - nothing was detected at all.
    """
    inside_zone = bool(monitored_zones) and any(
        detection_matches_zone(d, z) for d in detections for z in monitored_zones
    )
    if inside_zone:
        labels = ', '.join(sorted({str(d.get('label')) for d in detections if d.get('label')})) or 'object'
        return f'in a zone, but no enabled rule matched {labels} (check the zone Object Rules)'
    if raw_labels:
        return 'outside your zone areas'
    return 'No detections matched this camera and its zone areas.'


def _below_threshold_object_reason(
    detections: list[dict[str, Any]],
    monitored_zones: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return a sound-style diagnostic for an object below its zone threshold."""
    candidates: list[dict[str, Any]] = []
    for detection in detections:
        label = str(detection.get('label') or '').strip().lower()
        if not label:
            continue
        try:
            confidence = float(detection.get('confidence'))
        except (TypeError, ValueError):
            continue
        for zone in monitored_zones:
            if not detection_matches_zone(detection, zone):
                continue
            matching_rules = [
                rule for rule in zone.get('object_rules') or []
                if rule.get('enabled', True)
                and str(rule.get('label') or '').strip().lower() != 'motion'
                and label_matches(label, rule.get('label'))
            ]
            if not matching_rules:
                continue
            thresholds: list[float] = []
            for rule in matching_rules:
                try:
                    thresholds.append(float(rule.get('min_confidence', 0.5) or 0.5))
                except (TypeError, ValueError):
                    thresholds.append(0.5)
            if thresholds and not any(confidence >= threshold for threshold in thresholds):
                candidates.append({
                    'code': 'below_threshold',
                    'label': label,
                    'confidence': round(max(0.0, confidence), 3),
                    'threshold': round(min(thresholds), 3),
                })
    if not candidates:
        return None
    return max(candidates, key=lambda item: item['confidence'])


# Faint detections for the recording detection log: the detector is asked down
# to this confidence, and whatever lands under the real floor is logged as
# dropped ("Below the detection minimum") instead of silently never existing.
# Only watched labels in watched zones are logged, at most FAINT_LOG_MAX per
# cycle, strongest first, so a bush that looks 20% like a person does not
# flood the log.
FAINT_LOG_FLOOR = 0.20
FAINT_LOG_MAX = 5


def _faint_log_floor(detect_floor: float) -> float:
    """Confidence to ask the detector for: the log floor, unless the real one is lower."""
    return min(float(detect_floor), FAINT_LOG_FLOOR)


def _split_faint_detections(
    detections: list[dict[str, Any]], detect_floor: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(kept, faint)``: faint detections scored under ``detect_floor``.

    Kept detections are exactly what a detector asked at ``detect_floor``
    returns, so everything downstream behaves as before.
    """
    floor = float(detect_floor)
    kept: list[dict[str, Any]] = []
    faint: list[dict[str, Any]] = []
    for det in detections:
        try:
            confidence = float(det.get('confidence') or 0)
        except (AttributeError, TypeError, ValueError):
            kept.append(det)
            continue
        if confidence < floor:
            faint.append({
                **det,
                'reason': f'Below the detection minimum ({confidence:.2f} < {floor:.2f})',
            })
        else:
            kept.append(det)
    return kept, faint


def _faint_log_entries(early_dropped: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only the faint detections worth logging (see FAINT_LOG_FLOOR);
    Second Look rejections pass through untouched."""
    faint = [det for det in early_dropped if str(det.get('reason') or '').startswith('Below the detection minimum')]
    if not faint:
        return early_dropped
    others = [det for det in early_dropped if det not in faint]
    watched = filter_detections_for_camera(faint, settings)
    watched.sort(key=lambda det: float(det.get('confidence') or 0), reverse=True)
    return others + watched[:FAINT_LOG_MAX]


def _new_known_face_alerts(
    camera_id: str, matches: list[dict[str, Any]], *, absence: float, recording_id: int | None,
) -> list[dict[str, Any]]:
    """Keep one alert per person rule that is NEW in view (presence-gated)."""
    counts: dict[str, int] = {}
    for match in matches:
        key = f"person:{match.get('face_rule_id') or match.get('rule_name') or ''}"
        counts[key] = counts.get(key, 0) + 1
    new = observe_presence(camera_id, counts, absence=absence, recording_id=recording_id)
    alerts: list[dict[str, Any]] = []
    for match in matches:
        key = f"person:{match.get('face_rule_id') or match.get('rule_name') or ''}"
        if key in new:
            new.discard(key)
            alerts.append(match)
    return alerts


def _new_unknown_face_alerts(
    camera_id: str, matches: list[dict[str, Any]], *, absence: float, recording_id: int | None,
) -> list[dict[str, Any]]:
    """Keep the unknown-face rules whose stranger count in view went up.

    Each unknown rule alerts when a stranger appears, and again only when more
    strangers are in view together than before.
    """
    counts: dict[str, int] = {}
    for match in matches:
        for rule_id in match.get('face_rule_ids') or []:
            counts[f'unknown-face:{rule_id}'] = counts.get(f'unknown-face:{rule_id}', 0) + 1
    new = observe_presence(camera_id, counts, absence=absence, recording_id=recording_id)
    alerts: list[dict[str, Any]] = []
    for match in matches:
        fired = [rule_id for rule_id in match.get('face_rule_ids') or [] if f'unknown-face:{rule_id}' in new]
        if fired:
            new.difference_update(f'unknown-face:{rule_id}' for rule_id in fired)
            alerts.append({**match, 'face_rule_ids': fired})
    return alerts


def _parse_motion_override(raw_value: Any, fallback: Any, cast=float) -> Any:
    """Cast a per-camera motion override, keeping ``fallback`` if the cast fails.

    One malformed override (e.g. a bad ``motion_pixel_threshold`` merged from a
    hand-edited config.yaml) must never break the detection cycle, so a cast
    failure silently keeps the validated global value. Used for every numeric
    override on the ~4 Hz hot path so each stays a single inline branch instead
    of a hand-rolled try/except per field.
    """
    try:
        return cast(raw_value)
    except (TypeError, ValueError):
        return fallback


def _camera_has_direct_frame_source(camera_id: str) -> bool:
    """Return True when the configured camera can provide a frame directly.

    The background monitor normally reads the shared RTSP ingest. ONVIF/direct
    camera configurations may not have a URL that ``build_stream_url`` can
    construct, but their camera instance can still provide ``read_jpeg``. Those
    cameras must not be skipped before the monitor gets a chance to use that
    source.
    """
    instance = _state.camera_instances.get(camera_id)
    return instance is not None and callable(getattr(instance, 'read_jpeg', None))


# ---------------------------------------------------------------------------
# Central inference scheduling
# ---------------------------------------------------------------------------
# Background detection used to spawn one daemon thread per camera, each of
# which then blocked on the shared detector's inference semaphore. With more
# cameras than the semaphore allows, the extra threads sat in staging holding
# their slots, and a thread that finally got the semaphore could be working on
# a decision it had started before the backlog existed. The scheduler below is
# the single admission point: one job per camera (newest wins), fair
# round-robin, priority for recording cameras, one global concurrency limit,
# and queue wait measured separately from run time.

def _scheduler_max_workers() -> int:
    """Global inference concurrency, mirroring the detector's own semaphore.

    ``max_concurrent_inferences`` already caps how many inferences may run at
    once; applying the same number here means cameras queue at the door instead
    of piling N threads onto one lock. Never raises: a settings read failure
    falls back to the conservative single-worker default rather than taking the
    detection loop down with it.
    """
    try:
        configured = int(effective_ai_config().get('max_concurrent_inferences') or 1)
    except Exception:
        configured = 1
    return max(1, min(16, configured))


def _camera_is_priority(camera_id: str) -> bool:
    """A camera that is recording right now is served before idle cameras.

    An event clip that is already being written is worth finishing before a
    quiet camera's next check; a missed detection on that recording costs more
    than a few hundred ms of delay on the other.
    """
    with _state.active_rtsp_recordings_lock:
        return camera_id in _state.active_rtsp_recordings


def _scheduler_claim(camera_id: str) -> None:
    with _state.live_detection_worker_lock:
        _state.active_live_detection_cameras.add(camera_id)


def _scheduler_release(camera_id: str) -> None:
    with _state.live_detection_worker_lock:
        _state.active_live_detection_cameras.discard(camera_id)


def _scheduler_report_timing(camera_id: str, timing: dict[str, Any]) -> None:
    """Surface queue wait next to (not mixed into) execution time.

    A slow model and a starved queue are different problems; the live status
    payload keeps them as separate numbers so the Live page and the API can
    tell which one is hurting.
    """
    if not timing:
        return
    try:
        update_live_detection_status(
            camera_id,
            inference_wait_ms=int(round(float(timing.get('wait_seconds') or 0.0) * 1000)),
            inference_ms=int(round(float(timing.get('run_seconds') or 0.0) * 1000)),
            inference_queue_depth=int(timing.get('queue_depth') or 0),
        )
    except Exception as exc:  # pragma: no cover - defensive: status must never break a cycle
        logger.debug('Scheduler timing report failed for %s: %s', camera_id, exc)


# The concurrency limit is a settings value, so it can change at runtime, but
# reading it costs a settings lookup: re-read it at most every few seconds
# rather than on every camera of every monitor cycle.
_SCHEDULER_WORKERS_REFRESH_SECONDS = 5.0
_scheduler_workers_refreshed_at = 0.0
_scheduler_creation_lock = threading.Lock()


def get_live_inference_scheduler() -> LiveInferenceScheduler:
    # Foreground HTTP requests and the background monitor can arrive together
    # on first use. Publish exactly one shared pool and concurrency budget.
    with _scheduler_creation_lock:
        return _get_live_inference_scheduler_locked()


def _get_live_inference_scheduler_locked() -> LiveInferenceScheduler:
    """Return the process-wide scheduler, creating and starting it on demand.

    Created lazily (rather than at monitor start) so the Live page's
    foreground detection path can reach it even if background detection was
    disabled, and so tests can inject their own.
    """
    global _scheduler_workers_refreshed_at
    scheduler = getattr(_state, 'live_inference_scheduler', None)
    if scheduler is None:
        scheduler = LiveInferenceScheduler(
            _run_scheduled_detection,
            is_priority=_camera_is_priority,
            on_claim=_scheduler_claim,
            on_release=_scheduler_release,
            on_complete=_scheduler_report_timing,
            max_workers=_scheduler_max_workers(),
        )
        _state.live_inference_scheduler = scheduler
        _scheduler_workers_refreshed_at = time.time()
    now = time.time()
    if now - _scheduler_workers_refreshed_at >= _SCHEDULER_WORKERS_REFRESH_SECONDS:
        _scheduler_workers_refreshed_at = now
        scheduler.set_max_workers(_scheduler_max_workers())
    scheduler.start()
    return scheduler


def read_live_detection_frame(camera_id: str) -> tuple[Any, dict[str, Any]] | None:
    """Return the newest available frame for ``camera_id``, or ``None``.

    Called by the scheduler when a job actually runs, NOT when it is queued, so
    a job that waited behind a backlog still processes the newest frame
    available rather than the one that existed when it was submitted.
    """
    read_started = time.perf_counter()
    try:
        return _read_live_detection_frame(camera_id)
    finally:
        # Sampled here, outside the per-cycle timer, because this runs BEFORE
        # the cycle begins. A slow read is an ingest/RTSP problem, not a stage
        # of the detection pipeline, and conflating the two would make a starved
        # camera look like a slow one.
        record_frame_read(camera_id, (time.perf_counter() - read_started) * 1000.0)


def _read_live_detection_frame(camera_id: str) -> tuple[Any, dict[str, Any]] | None:
    sample = read_ingest_frame(camera_id)
    if sample is not None:
        return sample
    # Prefer a direct camera frame when the shared ingest has not produced one
    # yet. The previous early return here made this fallback unreachable for
    # ONVIF/direct cameras, which left their live status stuck at Waiting and
    # prevented both motion telemetry and alerts from running.
    cam_instance = _state.camera_instances.get(camera_id)
    if cam_instance is not None and callable(getattr(cam_instance, 'read_jpeg', None)):
        try:
            import cv2
            import numpy as np
            jpeg_bytes, _frame_meta = cam_instance.read_jpeg()
            img = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                h, w = img.shape[:2]
                return img, {'frame_number': 0, 'timestamp': time.time(), 'width': w, 'height': h}
        except Exception as exc:
            logger.debug('Direct frame fallback failed for camera %s: %s', camera_id, exc)
    if not _state.recording_service.ingest_has_produced_frame(camera_id):
        return None
    schedule_live_camera_backoff(camera_id, 'No fresh frame available from the camera ingest.')
    return None


def _run_scheduled_detection(image: Any, frame: dict[str, Any], camera_cfg: dict[str, Any]) -> None:
    """Background detection cycle: run it, or back the camera off on failure."""
    camera_id = str(camera_cfg.get('id') or 'camera')
    try:
        clear_live_camera_backoff(camera_id)
        process_live_stream_alerts(image, frame, camera_cfg, enforce_interval=False)
    except Exception as exc:
        logger.warning('Background live alert check failed for camera %s: %s', camera_id, exc)
        schedule_live_camera_backoff(camera_id, str(exc))


def _run_foreground_live_detection(image: Any, frame: dict[str, Any], camera_cfg: dict[str, Any]) -> None:
    """Live-page fallback cycle: a failure surfaces on the page, not as a backoff."""
    camera_id = str(camera_cfg.get('id') or 'camera')
    try:
        process_live_stream_alerts(image, frame, camera_cfg, enforce_interval=False)
    except Exception as exc:
        logger.warning('Live detection failed for camera %s: %s', camera_id, exc)
        update_live_detection_status(camera_id, state='error', reason=str(exc), detections=[])


def run_live_alert_monitor_once(live_settings: dict[str, Any] | None=None) -> int:
    if live_settings is None:
        live_settings = effective_live_config()
    processed = 0
    for selected_config in list(_state.cameras_config):
        camera_live_settings = effective_camera_live_settings(selected_config, live_settings)
        camera_id = str(selected_config.get('id') or 'camera')
        if selected_config.get('enabled') is False:
            continue
        has_ingest_stream = _camera_has_live_alert_stream(selected_config)
        has_direct_source = _camera_has_direct_frame_source(camera_id)
        if not has_ingest_stream and not has_direct_source:
            continue
        now = time.time()
        stream_url = build_stream_url(selected_config)
        cam_rec_config = _state.camera_event_recording_config(selected_config)
        if has_ingest_stream and stream_url:
            _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config)
            if cam_rec_config.get('continuous'):
                _state.recording_service.start_continuous_chunk_recording(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config, on_chunk_complete=_make_continuous_chunk_callback(camera_id))
            else:
                # Continuous recording switched off (or never on): drop the
                # camera's continuous entry so its ingest reconnects without
                # chunking. A no-op when nothing was recording.
                _state.recording_service.stop_continuous_chunk_recording(camera_id)
        # The camera's Day/Night profile value when set, else the global one
        # (effective_camera_live_settings merges them), so a camera can be
        # switched on while the global default is off as well as the reverse.
        if not normalize_bool_setting(camera_live_settings.get('background_detection_enabled'), True):
            continue
        with _state._live_backoff_lock:
            retry_after = _state.live_detection_retry_after.get(camera_id, 0)
        if retry_after and now < retry_after:
            continue
        detection_interval_seconds = _adaptive_detection_interval(
            camera_id,
            camera_live_settings,
        )
        with _state.live_detection_worker_lock:
            if camera_id in _state.active_live_detection_cameras:
                continue
            if now - _state.live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
                continue
            _state.live_detection_last_checked[camera_id] = now
            _state.active_live_detection_cameras.add(camera_id)

        # Snapshot the camera config NOW: the job runs later (possibly after
        # the loop has advanced to another camera), so it must carry its own
        # copy rather than a reference to whatever ``selected_config`` points
        # at by then. This is the same late-binding fix the old per-camera
        # thread closure used default arguments for.
        camera_cfg = dict(selected_config)
        scheduler = get_live_inference_scheduler()
        submitted = scheduler.submit(
            camera_id,
            camera_cfg,
            # Read the frame when the job RUNS, not when it is queued: latest
            # frame wins, so a job that waited behind a backlog still scores
            # the newest picture instead of a stale one.
            lambda cid=camera_id: read_live_detection_frame(cid),
            interval=detection_interval_seconds,
        )
        if not submitted:
            # The scheduler is stopped (shutdown). Release the claim we took
            # above rather than leaving the camera marked busy forever.
            _scheduler_release(camera_id)
            continue
        processed += 1
    return processed

def _prune_frame_motion_state() -> None:
    """Remove background model and scan timestamp entries for cameras no longer in the active config."""
    from app.detection_state import frame_motion_locks

    active_ids = {str(cfg.get('id') or '') for cfg in _state.cameras_config if cfg.get('id')}
    with frame_motion_locks():
        # Union of every per-camera motion model so MOG2-only or diff-only
        # cameras are both pruned (a camera lives in one engine's dict, not both).
        tracked_ids = (
            set(_state._frame_motion_prev)
            | set(_state._frame_motion_last_frame)
            | set(_state._frame_motion_last_gray)
            | set(_state._frame_motion_mog2)
            | set(_state._frame_motion_mog2_meta)
            | set(_state._frame_motion_scene_streak)
            | set(_state._frame_motion_shadow_mask)
            | set(_state._frame_motion_error_cameras)
        )
        stale = [cid for cid in tracked_ids if cid not in active_ids]
        for cid in stale:
            _state._frame_motion_prev.pop(cid, None)
            _state._frame_motion_last_frame.pop(cid, None)
            _state._frame_motion_last_gray.pop(cid, None)
            _state._frame_motion_mog2.pop(cid, None)
            _state._frame_motion_mog2_meta.pop(cid, None)
            _state._frame_motion_scene_streak.pop(cid, None)
            _state._frame_motion_shadow_mask.pop(cid, None)
            _state._frame_motion_error_cameras.discard(cid)
    for cid in stale:
        _state._periodic_scan_last_ts.pop(cid, None)
        with _state._motion_confirm_lock:
            _state._motion_confirm_streaks.pop(cid, None)
        forget_motion_levels(cid)
        _state.motion_object_arbiter.clear_camera(cid)
        with _state._object_tracks_lock:
            _state._object_tracks.pop(cid, None)
    if stale:
        with _state.live_detection_confirm_lock:
            for cid in stale:
                _state.live_detection_confirm_history.pop(cid, None)
        # A removed camera must not leave a permanent telemetry entry (or stale
        # behavioural presence) behind in the same pruning pass.
        prune_detection_telemetry(active_ids)
        prune_pipeline_timing(active_ids)
        for cid in stale:
            clear_behavioural_state(cid)
        logger.debug('Pruned stale motion state for cameras: %s', stale)

def live_alert_monitor_loop() -> None:
    _last_prune = 0.0
    while not _state.live_alert_monitor_stop.is_set():
        try:
            live_settings = effective_live_config()
            run_live_alert_monitor_once(live_settings)
            _check_cameras_health()
            now = time.time()
            if now - _last_prune > 300:
                _prune_frame_motion_state()
                purge_camera_diagnostics_by_policy()
                purge_ai_diagnostics_by_policy()
                _last_prune = now
            configured_intervals = [
                float(effective_camera_live_settings(camera, live_settings).get('detection_interval_seconds', 0.5))
                for camera in _state.cameras_config
            ]
            interval = max(0.1, min(configured_intervals or [float(live_settings.get('detection_interval_seconds', 0.5))]))
        except Exception as exc:
            # The monitor thread is the single background source for detection,
            # camera-health tracking, and offline/recovery alerts. A transient
            # failure (e.g. SQLite briefly locked by a settings write while
            # ``effective_live_config`` or ``_check_cameras_health`` reads a
            # setting) must not kill the loop: that would silently disable all
            # background detection until the next service restart. Log and
            # retry on a short delay instead.
            logger.warning('Live alert monitor cycle failed; retrying: %s', exc)
            interval = 1.0
        _state.live_alert_monitor_stop.wait(interval)

def start_live_alert_monitor() -> None:
    if _state.live_alert_monitor_thread and _state.live_alert_monitor_thread.is_alive():
        return
    _state.live_alert_monitor_stop.clear()
    _state.live_alert_monitor_thread = threading.Thread(target=live_alert_monitor_loop, name='live-alert-monitor', daemon=True)
    _state.live_alert_monitor_thread.start()

def stop_live_alert_monitor() -> None:
    _state.live_alert_monitor_stop.set()
    if _state.live_alert_monitor_thread and _state.live_alert_monitor_thread.is_alive():
        _state.live_alert_monitor_thread.join(timeout=5)
    _state.live_alert_monitor_thread = None
    # Stop the shared scheduler too: its workers would otherwise keep serving
    # queued cameras after detection was supposed to have stopped. Dropping the
    # instance makes the next ``get_live_inference_scheduler()`` build a fresh
    # one, so a restart-in-place (settings apply) is clean.
    scheduler = getattr(_state, 'live_inference_scheduler', None)
    if scheduler is not None:
        scheduler.stop()
        _state.live_inference_scheduler = None


# ---------------------------------------------------------------------------
# Phase-K: live-stream detection entry points
# ---------------------------------------------------------------------------

def _encode_frame_jpeg(image: Any) -> bytes:
    """Encode a numpy BGR frame to JPEG bytes for snapshot storage."""
    import cv2
    ok, buffer = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok or buffer is None:
        raise RuntimeError('Failed to encode frame as JPEG')
    return buffer.tobytes()


def queue_live_stream_alerts(
    image_bytes: bytes,
    frame: dict[str, Any],
    settings: dict[str, Any],
    *,
    allow_when_background_enabled: bool = False,
) -> None:
    """Queue a foreground frame, optionally as a live-page fallback.

    The background monitor remains the normal source. The Live page can opt
    into this same guarded worker path when the monitor has not produced a
    status for an ONVIF camera; ``live_detection_last_checked`` and the worker
    lock ensure the two sources do not run duplicate inference cycles.
    """
    camera_id = str(settings.get('id') or 'camera')
    live_cfg = effective_live_config()
    camera_live_cfg = effective_camera_live_settings(settings, live_cfg)
    # Per camera, like the background loop: a camera whose profile turns
    # background detection off still gets the Live page's foreground cycles.
    background_enabled = normalize_bool_setting(camera_live_cfg.get('background_detection_enabled'), True)
    if background_enabled and not allow_when_background_enabled:
        return
    stream_url = build_stream_url(settings)
    if stream_url:
        _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=_state.camera_event_recording_config(settings))
    detection_interval_seconds = float(camera_live_cfg.get('detection_interval_seconds', 0.5))
    now = time.time()
    with _state.live_detection_worker_lock:
        if camera_id in _state.active_live_detection_cameras:
            return
        if now - _state.live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
            return
        _state.live_detection_last_checked[camera_id] = now
        _state.active_live_detection_cameras.add(camera_id)

    # The same scheduler as the background path: a foreground request queues
    # behind whatever inference is already running rather than grabbing a
    # thread and blocking on the detector's semaphore. The frame is the one the
    # page just fetched, so it is replayed as-is instead of being re-read.
    submitted = get_live_inference_scheduler().submit(
        camera_id,
        dict(settings),
        lambda: (image_bytes, frame),
        runner=_run_foreground_live_detection,
        interval=detection_interval_seconds,
    )
    if not submitted:
        with _state.live_detection_worker_lock:
            _state.active_live_detection_cameras.discard(camera_id)


def _adaptive_detection_interval(
    camera_id: str,
    camera_live_settings: dict[str, Any],
) -> float:
    """The detection interval this camera should be sampled at (Item 16).

    Starts from the operator's configured ``detection_interval_seconds`` and
    lets the adaptive tracker stretch it when the camera's scene has been
    still for a while. Two things keep this honest rather than merely cheap:
    the tracker enforces a hard staleness floor, so a stationary subject is
    still sampled within ``max_stale_seconds``; and the existing
    ``periodic_scan_interval_seconds`` scan keeps overriding the motion gate,
    so the two features compose instead of fighting.

    Adaptive cadence is opt-out per camera via ``adaptive_detection_enabled``:
    an operator who wants a fixed interval for a camera can pin it, and a
    dropped frame on that camera is their call, not ours.
    """
    configured = float(camera_live_settings.get('detection_interval_seconds', 0.5))
    if not normalize_bool_setting(camera_live_settings.get('adaptive_detection_enabled'), True):
        return configured
    scheduler = get_live_inference_scheduler()
    stats = scheduler.stats() if scheduler is not None else {}
    return get_adaptive_cadence().effective_interval(
        camera_id,
        configured,
        pending=int(stats.get('pending', 0) or 0),
        max_workers=int(stats.get('max_workers', 1) or 1),
    )


def _camera_has_face_zone_rules(settings: dict[str, Any]) -> bool:
    """True when any enabled zone on this camera carries a ``face`` object rule.

    Such a rule is alerted by the AlertEngine off the ``face`` label, so a
    camera with one MUST keep running the face pass -- even when no
    face-detection rule and no identity annotation would otherwise want it.
    """
    for zone in (settings.get('detection') or {}).get('zones', []):
        if not zone.get('enabled', True):
            continue
        for rule in zone.get('object_rules') or []:
            if rule.get('enabled', True) and str(rule.get('label') or '').strip().lower() == 'face':
                return True
    return False


def camera_uses_face_detections(camera_id: str, settings: dict[str, Any]) -> bool:
    """True when anything on this camera can consume a ``face`` detection.

    A camera with nothing to do with faces should not pay for a second ONNX
    pass every cycle: both models scan the same frame, so an idle face model
    roughly doubles per-cycle inference cost for detections that are then
    dropped. Three things can want a face here, and any one of them is enough:

    * a face-detection rule (per-person or the ``_unknown`` stranger rule) that
      is enabled and not pinned to a different camera -- a zone-scoped rule
      still counts, because a face anywhere in frame can land in that zone;
    * a zone ``face`` object rule on this camera (AlertEngine);
    * face recognition enabled with a loaded model, which stamps identities on
      events and captures unknown faces for review whether or not any rule
      alerts on them.

    Deliberately conservative: anything unrecognised counts as "in use" rather
    than risking a silently dead face feature.
    """
    if _camera_has_face_zone_rules(settings or {}):
        return True
    try:
        rules = (effective_face_detection_rules().get('rules') or []) if camera_id else []
    except Exception as exc:  # pragma: no cover - defensive: never fail a cycle on a settings read
        logger.debug('Face rule lookup failed for camera %s: %s', camera_id, exc)
        return True
    for rule in rules:
        if not normalize_bool_setting(rule.get('enabled'), False):
            continue
        rule_camera = str(rule.get('camera_id') or '').strip()
        if rule_camera and rule_camera != str(camera_id or '').strip():
            continue
        return True
    try:
        from app.face_recognition_service import get_face_recognition_service
        if normalize_bool_setting(effective_face_recognition_config().get('enabled'), False) and getattr(
            get_face_recognition_service(), 'available', False
        ):
            return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug('Face recognition lookup failed for camera %s: %s', camera_id, exc)
        return True
    return False


def face_detection_pass_due(camera_id: str, live_settings: dict[str, Any] | None) -> bool:
    """Claim this camera's face-pass slot, honouring ``face_detection_interval_seconds``.

    The face model runs on its own, slower clock than object detection: the
    last-run stamp lives in its own dict (not ``live_detection_last_checked``)
    so the object cadence is unaffected. The stamp is written only when the
    pass is genuinely due and about to run, so a camera that skipped a cycle
    (backoff, hidden tab) is immediately eligible again instead of waiting out
    the remainder of an interval it never consumed.
    """
    try:
        interval = max(0.1, min(10.0, float((live_settings or {}).get('face_detection_interval_seconds', 1.0))))
    except (TypeError, ValueError):
        interval = 1.0
    now = time.time()
    with _state.live_detection_worker_lock:
        if now - float(_state.face_detection_last_checked.get(camera_id, 0.0) or 0.0) < interval:
            return False
        _state.face_detection_last_checked[camera_id] = now
    return True


def merge_secondary_face_detections(
    image: Any,
    detections: list,
    confidence: float | None = None,
    *,
    camera_id: str = '',
    settings: dict[str, Any] | None = None,
    live_settings: dict[str, Any] | None = None,
) -> list:
    """Run the optional secondary face detector and merge its results.

    The secondary detector is a dedicated face model that scans the same frame
    as the primary object model. Returns the input list unchanged when the face
    detector is not configured/loaded, when the camera has no consumer for face
    detections, or when this camera's face interval has not elapsed yet (see
    :func:`camera_uses_face_detections` and
    :func:`face_detection_pass_due`); a failing pass is logged and skipped
    rather than taking down the whole detection cycle.

    Callers that pass no ``camera_id`` (unit tests, the snapshot path) keep the
    historical behaviour: run the pass, unconditionally.

    ``confidence`` is deliberately left as ``None`` by the live caller so the
    face detector applies its OWN configured threshold (the Face Confidence
    setting). Passing the primary pipeline's object-rule threshold here used to
    silently override that setting, leaving Face Confidence with no effect on
    the live path. Per-rule minimums are still enforced downstream by the face
    rules themselves.
    """
    face_detector = getattr(_state, 'face_detector', None)
    if face_detector is None or not getattr(face_detector, 'available', False):
        return detections
    if camera_id:
        if not camera_uses_face_detections(camera_id, settings or {}):
            return detections
        if not face_detection_pass_due(camera_id, live_settings):
            return detections
    try:
        if isinstance(image, bytes):
            face_detections = face_detector.detect_image(image, confidence=confidence)
        else:
            face_detections = face_detector.detect_frame(image, confidence=confidence)
    except (DetectorUnavailableError, ValueError) as exc:
        logger.warning('Secondary face detection pass skipped: %s', exc)
        return detections
    if not face_detections:
        return detections
    if camera_id and face_requires_person(effective_ai_config()):
        face_detections = faces_on_people(camera_id, face_detections, detections)
    return list(detections) + list(face_detections)


def face_requires_person(ai_config: dict[str, Any]) -> bool:
    """"Only accept faces on a person" (Faces page), on unless switched off."""
    return normalize_bool_setting(ai_config.get('face_require_person'), True)


# How far a face's centre may sit outside a person's box, as a share of that
# box: a head is at the top edge of a person box, and a box drawn a little
# tight must not drop the face it belongs to.
_FACE_PERSON_MARGIN = 0.1


def faces_on_people(camera_id: str, faces: list, detections: list) -> list:
    """Keep only the faces whose centre lies on a person.

    A face detector run on its own misfires on textures the object model
    shrugs off -- at night on an IR camera it boxes gravel, concrete, fences
    and the timestamp overlay as "faces", sometimes at high confidence. A real
    face belongs to a person, and the object detector finds people far more
    reliably, so a face counts only where a person is: one detected this
    cycle, or a person track seen in the last couple of cycles (the person
    model can miss a frame while the face is still in view).
    """
    people = [
        detection.get('box') for detection in detections
        if str(detection.get('label') or '').strip().lower() == 'person' and isinstance(detection.get('box'), dict)
    ]
    people.extend(recent_track_boxes(camera_id, 'person'))
    if not people:
        return []

    def _on_person(face: dict) -> bool:
        box = face.get('box')
        if not isinstance(box, dict):
            return False
        try:
            cx = float(box['x']) + float(box['width']) / 2
            cy = float(box['y']) + float(box['height']) / 2
        except (KeyError, TypeError, ValueError):
            return False
        for person in people:
            try:
                px, py = float(person['x']), float(person['y'])
                pw, ph = float(person['width']), float(person['height'])
            except (KeyError, TypeError, ValueError):
                continue
            mx, my = pw * _FACE_PERSON_MARGIN, ph * _FACE_PERSON_MARGIN
            if px - mx <= cx <= px + pw + mx and py - my <= cy <= py + ph + my:
                return True
        return False

    return [face for face in faces if _on_person(face)]


def process_live_stream_alerts(image: Any, frame: dict[str, Any], settings: dict[str, Any], *, enforce_interval: bool = True) -> int | None:
    camera_id = str(settings.get('id') or 'camera')
    live_settings = effective_camera_live_settings(settings, effective_live_config())
    detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.5))
    # Master AI toggle (ai.enabled): when disabled, skip inference and every
    # downstream effect (detections, alerts, AI-triggered recordings). This is
    # the single gate for both the background monitor thread and the
    # event-driven queue path, so toggling AI off stops object detection
    # everywhere without tearing down the detector session.
    ai_config = effective_ai_config()
    if not normalize_bool_setting(ai_config.get('enabled'), True):
        update_live_detection_status(camera_id, state='skipped', reason='AI detection is disabled.', detections=[])
        record_detection_cycle(
            camera_id,
            inference_mode=MODE_SKIPPED_NO_DETECTOR,
        )
        # A cycle that never reached the pipeline still happened: it is the
        # frame the operator waited for, and a camera whose cycles are all
        # 0ms is information (the AI gate is off), not a gap in the record.
        # Total is 0 rather than the cost of the gate itself, because the gate
        # runs ABOVE the pipeline and must not be charged to it.
        record_pipeline_cycle(camera_id, {'stages': {}, 'total_ms': 0.0})
        return None
    if enforce_interval:
        now = time.time()
        with _state.live_detection_worker_lock:
            if now - _state.live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
                return None
            _state.live_detection_last_checked[camera_id] = now
    ai_state = ai_status_payload()
    frame_is_numpy = hasattr(image, 'shape') and hasattr(image, 'dtype')
    now = time.time()
    try:
        frame_capture_ts = float(frame.get('timestamp') or 0.0)
    except (TypeError, ValueError):
        frame_capture_ts = 0.0
    if not now - 300 <= frame_capture_ts <= now + 1:
        frame_capture_ts = now
    _pixel_threshold = float(live_settings.get('motion_pixel_threshold', _state._MOTION_PIXEL_THRESHOLD))
    _gate_fraction = float(live_settings.get('motion_gate_fraction', _state._MOTION_GATE_FRACTION))
    _scale_fraction = float(live_settings.get('motion_scale_fraction', _state._MOTION_SCALE_FRACTION))
    _background_alpha = float(live_settings.get('motion_background_alpha', _state._MOTION_BACKGROUND_ALPHA))
    # Background engine + post-processing toggles (global defaults; per-camera
    # override resolved below alongside the numeric thresholds).
    _algorithm = str(live_settings.get('motion_algorithm', getattr(_state, '_MOTION_ALGORITHM', 'mog2')) or 'mog2').strip().lower()
    _denoise = normalize_bool_setting(live_settings.get('motion_denoise'), getattr(_state, '_MOTION_DENOISE', True))
    # Tri-state ('on'/'off'/'auto'; a bool is also accepted) resolved per-frame in
    # the engine, so pass the raw value through rather than coercing to a bool.
    _shadow_suppression = live_settings.get('motion_shadow_suppression', getattr(_state, '_MOTION_SHADOW_SUPPRESSION', 'on'))
    # Clamp to the SAME bounds validate_live_settings enforces (40-640 x
    # 30-480). The UI path is already validated, but effective_live_config also
    # merges a raw ``live`` block from config.yaml that never passes through the
    # validator -- without the upper bound an oversized value would allocate a
    # huge per-frame motion thumbnail on the hot path and waste CPU/memory.
    _frame_w = max(40, min(640, int(live_settings.get('motion_frame_width', _state._MOTION_FRAME_W))))
    _frame_h = max(30, min(480, int(live_settings.get('motion_frame_height', _state._MOTION_FRAME_H))))
    # Keep the motion thumbnail geometry local to this camera.  The previous
    # implementation wrote these values into app.state globals and cleared every
    # camera's models, so cameras with different Day/Night profiles continuously
    # invalidated one another.  detect_frame_motion passes the size into the
    # per-camera model signature and the zone scorer derives geometry from the
    # returned mask shape.
    # Per-camera flat overrides; a value that fails its cast keeps the
    # validated global default via _parse_motion_override.
    _cam_pt = settings.get('motion_pixel_threshold')
    if _cam_pt is not None:
        _pixel_threshold = _parse_motion_override(_cam_pt, _pixel_threshold)
    _cam_gf = settings.get('motion_gate_fraction')
    if _cam_gf is not None:
        _gate_fraction = _parse_motion_override(_cam_gf, _gate_fraction)
    _cam_sf = settings.get('motion_scale_fraction')
    if _cam_sf is not None:
        _scale_fraction = _parse_motion_override(_cam_sf, _scale_fraction)
    _cam_ba = settings.get('motion_background_alpha')
    if _cam_ba is not None:
        _background_alpha = _parse_motion_override(_cam_ba, _background_alpha)
    # Per-camera engine / post-processing overrides.
    _cam_algo = settings.get('motion_algorithm')
    if _cam_algo is not None:
        _algorithm = str(_cam_algo or 'mog2').strip().lower() or _algorithm
    _cam_dn = settings.get('motion_denoise')
    if _cam_dn is not None:
        _denoise = normalize_bool_setting(_cam_dn, _denoise)
    _cam_ss = settings.get('motion_shadow_suppression')
    if _cam_ss is not None:
        _shadow_suppression = _cam_ss  # raw tri-state; resolved in the engine
    periodic_scan_interval = float(live_settings.get('periodic_scan_interval_seconds', 0))
    force_scan = False
    if periodic_scan_interval > 0 and now - _state._periodic_scan_last_ts.get(camera_id, 0) >= periodic_scan_interval:
        force_scan = True
        _state._periodic_scan_last_ts[camera_id] = now
    _cycle_timer = StageTimer()
    _motion_started = time.perf_counter()
    frame_has_motion, frame_motion_confidence, diff_mask, raw_motion_fraction = detect_frame_motion(camera_id, image, pixel_threshold=_pixel_threshold, gate_fraction=_gate_fraction, scale_fraction=_scale_fraction, background_alpha=_background_alpha, algorithm=_algorithm, denoise=_denoise, shadow_suppression=_shadow_suppression, frame_size=(_frame_w, _frame_h))
    _cycle_timer.add(STAGE_MOTION, (time.perf_counter() - _motion_started) * 1000.0)
    # Feed the adaptive-cadence tracker (Item 16). This is the only place that
    # knows whether the scene is still, so it is the only place that can keep
    # the per-camera quiet streak honest. Recorded on EVERY cycle, not just
    # the motion ones - the absence of motion is the signal.
    get_adaptive_cadence().note_cycle(camera_id, had_motion=bool(frame_has_motion))
    # Keep a diagnostic signal separate from the alert-gated confidence. The
    # latter is intentionally zero below the motion gate; the former lets the
    # live bar show real sub-gate pixel changes without making them alertable.
    motion_signal = round(min(1.0, raw_motion_fraction / max(_scale_fraction, 1e-9)), 3)
    # The frame-wide global-motion heuristic is only valid for a camera that can
    # actually move (PTZ / auto-track). On a fixed camera a high-change frame is
    # a real subject or a lighting shift, so it must not gate object alerts. The
    # per-camera ``ptz_motion_detection`` switch decides: ``on`` always runs it,
    # ``off`` never does, and ``auto`` (default) follows the PTZ-enabled flag --
    # the historical behaviour. An app-issued PTZ command still suppresses via
    # ``command_until`` regardless of this switch.
    _ptz_motion_mode = normalize_ptz_motion_detection((settings.get('detection') or {}).get('ptz_motion_detection'))
    if _ptz_motion_mode == 'on':
        _allow_auto_motion = True
    elif _ptz_motion_mode == 'off':
        _allow_auto_motion = False
    else:
        _allow_auto_motion = bool((settings.get('ptz') or {}).get('enabled'))
    camera_motion = update_camera_motion(
        camera_id, raw_motion_fraction, allow_auto_detection=_allow_auto_motion,
    )
    # Explicit behavioural pause token. Suppressing the engines while the camera
    # moves is not enough on its own: a pan moves every tracked box in image
    # space, so the FIRST sample after resume could otherwise read a
    # pre-pan visit as a long loiter or a pan-induced line crossing. The token
    # drops per-camera transitional state on both the onset and the resume edge
    # (learned baselines and cooldowns survive, so a 0.4s nudge cannot erase
    # an hour of learning or re-fire the same anomaly).
    sync_behaviour_pause(camera_id, bool(camera_motion.get('active')))
    # Per-cycle pipeline telemetry (see app/detection_telemetry.py). Populated
    # as the cycle advances so a diagnostics reader can attribute a missed
    # detection to a specific stage instead of correlating log lines.
    _telemetry_candidates: dict[str, int] = {}
    _telemetry_rejected: dict[str, int] = {}

    def _finish_pipeline_cycle(timer: StageTimer) -> dict[str, Any]:
        """Seal a cycle: stamp the total, then store the stage breakdown.

        The total is stamped LAST and from the live clock, so it covers every
        stage plus the Python between them. That residue -- ``unaccounted_ms``
        -- is reported alongside the breakdown, and its p95 is the roadmap's
        "cycle overhead < 10 ms" acceptance target. A number that stays large
        means a stage is still unmeasured, so the breakdown is not yet complete
        enough to act on.
        """
        timer.mark_total(timer.elapsed_ms())
        return record_pipeline_cycle(camera_id, timer)

    def _telemetry_finish(
        inference_mode: str,
        *,
        event_created: bool = False,
        error: bool = False,
    ) -> None:
        try:
            record_detection_cycle(
                camera_id,
                inference_mode=MODE_ERROR if error else inference_mode,
                camera_motion=bool(camera_motion.get('active')),
                camera_motion_reason=camera_motion.get('reason'),
                frame_motion=bool(frame_has_motion),
                candidates=dict(_telemetry_candidates),
                rejected=dict(_telemetry_rejected),
                event_created=event_created,
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a cycle
            logger.debug('Detection telemetry failed for %s: %s', camera_id, exc)
        _finish_pipeline_cycle(_cycle_timer)
    # A motion-gate error is not evidence of motion, but it must not suppress
    # the independent object-detection path: some callers provide detector-
    # compatible input that the optional motion decoder cannot parse. Keep the
    # zero-confidence motion result and let object inference decide normally.
    with frame_motion_lock(camera_id):
        motion_gate_error = camera_id in _state._frame_motion_error_cameras
    # Publish the measurement immediately, before any ONNX work or downstream
    # zone/alert filtering. The live page is a motion diagnostic, so it must
    # still show the measured pixel change when object inference is slow, fails,
    # or the result is later rejected by a rule. motion_confidence carries the
    # scaled zone-gate level (fraction / scale, the same 0-1 scale the zone
    # Sensitivity slider gates on); motion_fraction is the raw changed-pixel
    # fraction for context. This status-only update does not alter detections,
    # alerts, recordings, or detector inputs.
    update_live_detection_status(
        camera_id,
        state='checked',
        reason='Motion sample measured.',
        detections=[],
        frame_timestamp=frame_capture_ts,
        motion_confidence=frame_motion_confidence, motion_fraction=raw_motion_fraction, motion_signal=motion_signal,
        camera_motion=camera_motion,
    )
    if not frame_has_motion:
        frame_motion_confidence = 0.0
        # A periodic scan bypasses the gate but measured no pixel motion, so
        # motion zone rules stay silent (matches the docs). On a normal
        # sub-gate frame the diff mask is still valid: each zone scores its
        # OWN rectangle, so motion confined to a small zone (a doorway, a
        # distant subject) can clear that zone's rule without ever reaching
        # the frame-wide gate fraction. Evaluate the zone rules before the
        # bail below so those per-zone rules actually fire.
        if force_scan:
            diff_mask = None
    # Per-zone motion rules score independently of the frame-wide gate.
    _motion_levels: list[dict[str, Any]] = []
    motion_detections = zone_motion_detections(settings, frame_motion_confidence, diff_mask=diff_mask, scale_fraction=_scale_fraction, frame_size=(_frame_w, _frame_h), levels=_motion_levels)
    # Require the same motion zone to be active in consecutive analyzed frames
    # (each zone's "Must last: N checks", two by default) before allowing it
    # to create an event or recording. The raw motion telemetry and
    # object-detection path remain immediate; only motion-zone actions wait for
    # confirmation, filtering one-frame stream/exposure artifacts.
    _confirm_cycles = motion_zone_confirm_cycles(settings)
    motion_detections = confirm_motion_detections(camera_id, motion_detections, required_by_zone=_confirm_cycles)
    # A moving camera invalidates both global motion-zone detections and the
    # object movement verdict. Keep the raw pixel telemetry above, but do not
    # allow camera motion to create a motion-only event or recording.
    if camera_motion['active']:
        motion_detections = []
    # Shadow suppression also drops a dark subject over a lighter background (a
    # dark car on grey concrete), so the motion mask can lose a car while it is
    # still driving off. While a clip is recording, the shadow-inclusive mask
    # at each zone's own trigger may continue it (continue_active_recording
    # below); it never creates an event or starts a clip.
    shadow_motion_detections: list[dict[str, Any]] = []
    if diff_mask is not None and not camera_motion['active'] and active_recording_id(camera_id) is not None:
        _shadow_mask = shadow_inclusive_motion_mask(camera_id)
        if _shadow_mask is not None:
            shadow_motion_detections = zone_motion_detections(settings, frame_motion_confidence, diff_mask=_shadow_mask, scale_fraction=_scale_fraction, frame_size=(_frame_w, _frame_h))
    # Per-zone meter for the live page: each zone's moving share against its
    # own trigger, its state and its ten-minute peak.
    try:
        with _state._motion_confirm_lock:
            _streaks = dict(_state._motion_confirm_streaks.get(camera_id, {}))
        update_live_detection_status(camera_id, motion_zones=motion_zone_levels(
            camera_id, _motion_levels,
            triggered_zone_ids={str(item.get('zone_id') or '') for item in motion_detections},
            streaks=_streaks, confirm_cycles=_confirm_cycles,
        ))
    except Exception as exc:  # noqa: BLE001 - a status meter must never break a cycle
        logger.debug('Motion zone levels failed for %s: %s', camera_id, exc)
    # ``always_run_object_detection`` decouples object (YOLO) inference from the
    # motion gate: when set, inference runs every cycle regardless of pixel
    # motion, so a still/slow/low-contrast subject is never hidden from the
    # detector. Motion detection itself is unchanged -- it still runs above and
    # feeds motion-only zones/alerts. Default on; disable it to restore the
    # CPU-saving motion gate (inference only when motion fires).
    always_run_object_detection = normalize_bool_setting(live_settings.get('always_run_object_detection'), True)
    # Motion held for object priority must still be resolved this cycle (an
    # object may now be visible, or the hold may expire), so it keeps the
    # cycle running even on a quiet frame.
    if not frame_has_motion and (not motion_gate_error) and (not force_scan) and (not motion_detections) and (not shadow_motion_detections) and (not always_run_object_detection) and (not _state.motion_object_arbiter.has_held(camera_id)):
        update_live_detection_status(camera_id, state='checked', reason='No motion detected; ONNX inference skipped.', detected_labels=[], matched_labels=[], detections=[], frame_timestamp=frame_capture_ts, motion_confidence=frame_motion_confidence, motion_fraction=raw_motion_fraction)
        _telemetry_finish(MODE_SKIPPED_NO_MOTION)
        return None
    # Per-camera YOLO model assignment (app/camera_models.py): a camera with a
    # model override runs its own cached detector; every other camera shares
    # the global primary detector exactly as before. Readiness is measured on
    # the DETECTOR THIS CYCLE USES -- an assigned model that failed to load
    # must not inherit the global detector's healthy status (and vice versa).
    detector = camera_detector(settings, ai_config)
    detector_method_available = hasattr(
        detector,
        'detect_frame' if frame_is_numpy else 'detect_image',
    )
    if detector is _state.detector:
        detector_loaded = bool(ai_state['detector_loaded'])
    else:
        detector_loaded = bool(getattr(detector, 'available', False))
        _assigned_model = str(getattr(detector, 'model_path', '') or '').replace('\\', '/')
        _assigned_error = getattr(detector, 'unavailable_reason', None)
        ai_state = {
            **ai_state,
            'model_path': _assigned_model,
            'model_name': _assigned_model.rsplit('/', 1)[-1] or None,
            'detector_loaded': detector_loaded,
            'model_loaded': detector_loaded,
            'error': _assigned_error or '',
            'last_detector_error': _assigned_error or '',
        }
    detector_ready = bool(detector_loaded and detector_method_available)
    # Held motion must still reach the arbiter so the hold can expire and
    # release: motion-only rules keep working without an object detector.
    if not detector_ready and not motion_detections and not shadow_motion_detections and not _state.motion_object_arbiter.has_held(camera_id):
        detector_reason = (
            ai_state['last_detector_error']
            or 'Live object detector is not loaded; motion-only rules can still run.'
        )
        update_live_detection_status(
            camera_id,
            state='skipped',
            reason=detector_reason,
            ai=ai_state,
            detections=[],
            frame_timestamp=frame_capture_ts,
            motion_confidence=frame_motion_confidence, motion_fraction=raw_motion_fraction,
        )
        _telemetry_finish(MODE_SKIPPED_NO_DETECTOR)
        return None
    # Name the inference path this cycle actually took, so a diagnostics reader
    # can tell an always-on camera from one running under the CPU-saving gate.
    _telemetry_mode = (
        MODE_ALWAYS_ON if always_run_object_detection else MODE_MOTION_GATED
    )

    # Motion-only rules are independent of ONNX. If the object detector is
    # unavailable but a motion zone fired, continue with an empty object list
    # so the motion event/recording path still runs. When ONNX is ready, this
    # branch is identical to the existing object-detection path.
    min_conf = compute_minimum_rule_confidence(camera_settings=settings)
    _inference_started = time.perf_counter()
    # Captured straight after the BASE detector call, not after the opt-in
    # region-boost / tiling re-runs below: every extra pass overwrites
    # ``detector.last_timing``, so reading it after them would report the cost
    # of the last sub-inference as if it were the whole cycle's model time --
    # and ``account_for`` replaces (not adds) the inference stage, so the
    # base cost would silently vanish from the breakdown.
    _base_inference_timing: Any = None
    # Detections thrown away this cycle, with why (faint detections and Second
    # Look rejections here, pipeline drops below). Diagnostics only: they go to
    # the history's ``dropped`` list for the recording's detection log, never
    # downstream.
    _early_dropped: list[dict[str, Any]] = []
    try:
        if detector_ready and frame_is_numpy and hasattr(detector, 'detect_frame'):
            # Low-light / IR enhancement (opt-in): the detector, and the crop
            # passes below, see a contrast-equalised copy of a dark frame. The
            # original ``image`` still feeds snapshots, recordings and faces.
            detector_frame = image
            _low_light_mode = live_settings.get('object_detection_low_light')
            if normalize_low_light_mode(_low_light_mode) != 'off':
                _low_light_started = time.perf_counter()
                detector_frame, _low_light_applied = low_light_detection_frame(image, _low_light_mode)
                _cycle_timer.add(STAGE_LOW_LIGHT, (time.perf_counter() - _low_light_started) * 1000.0)
            # Second look (opt-in): the full-frame pass runs at a lower floor so
            # near-misses survive long enough to be re-checked at higher
            # resolution below; anything still unconfirmed is dropped there.
            _second_look = second_look_enabled(live_settings)
            _inference_started = time.perf_counter()
            _detect_floor = second_look_floor(min_conf) if _second_look else min_conf
            # Ask down to the detection-log floor, then split anything under the
            # real floor straight off: only the log ever sees it.
            detections = detector.detect_frame(
                detector_frame, confidence=_faint_log_floor(_detect_floor),
            )
            detections, _faint = _split_faint_detections(detections, _detect_floor)
            _early_dropped.extend(_faint)
            _base_inference_timing = getattr(detector, 'last_timing', None)
            _cycle_timer.add(STAGE_INFERENCE, (time.perf_counter() - _inference_started) * 1000.0)
            if _second_look:
                _second_look_started = time.perf_counter()
                detections = confirm_borderline_detections(
                    detector, detector_frame, detections,
                    threshold=min_conf,
                    # Only spend the re-check on a box some rule could use: a
                    # watched label inside a watched zone.
                    is_relevant=lambda candidates: filter_detections_for_camera(
                        normalize_detection_boxes_for_frame(candidates, frame), settings,
                    ),
                    rejected=_early_dropped,
                )
                _cycle_timer.add(STAGE_SECOND_LOOK, (time.perf_counter() - _second_look_started) * 1000.0)
            # Motion-region high-res boost (opt-in): re-run the detector zoomed
            # into the moving regions so small/distant subjects that vanish in
            # the full-frame downscale are recovered, then merge + de-dup. Safe
            # to call unconditionally -- it returns the base list when disabled,
            # when there is no diff mask, or when no region qualifies.
            if diff_mask is not None and region_boost_enabled(live_settings):
                _boost_started = time.perf_counter()
                detections = detect_with_region_boost(
                    detector, detector_frame, diff_mask, detections, confidence=min_conf,
                )
                _cycle_timer.add(STAGE_REGION_BOOST, (time.perf_counter() - _boost_started) * 1000.0)
            # Tiled / sliced inference (opt-in): re-run the detector on a grid of
            # overlapping tiles covering the WHOLE frame every cycle, recovering
            # small subjects anywhere -- including stationary ones the
            # motion-region boost never sees. Composes with region boost; the
            # shared IoU de-dup collapses any overlap.
            _tile_grid = tiling_grid(live_settings)
            if _tile_grid is not None:
                _tiling_started = time.perf_counter()
                detections = detect_with_tiling(
                    detector, detector_frame, detections,
                    cols=_tile_grid[0], rows=_tile_grid[1], confidence=min_conf,
                )
                _cycle_timer.add(STAGE_TILING, (time.perf_counter() - _tiling_started) * 1000.0)
        elif detector_ready:
            detections = detector.detect_image(image, confidence=_faint_log_floor(min_conf))
            detections, _faint = _split_faint_detections(detections, min_conf)
            _early_dropped.extend(_faint)
            _base_inference_timing = getattr(detector, 'last_timing', None)
            _cycle_timer.add(STAGE_INFERENCE, (time.perf_counter() - _inference_started) * 1000.0)
        else:
            detections = []
    except Exception as exc:
        # A provider/runtime failure must only fail this camera cycle. In
        # particular, CUDA/ORT can raise RuntimeError or TypeError after the
        # detector was reported healthy; letting either escape kills the worker
        # path and leaves stale live status until the next external request.
        logger.warning('Live detection skipped for camera %s: %s', camera_id, exc)
        update_live_detection_status(camera_id, state='error', reason=str(exc), ai=ai_state, detections=[], frame_timestamp=frame_capture_ts, motion_confidence=frame_motion_confidence, motion_fraction=raw_motion_fraction)
        _telemetry_finish(_telemetry_mode, error=True)
        return None
    # Secondary face-detector pass (opt-in): runs a dedicated face model
    # alongside the primary object detector so COCO objects and faces are
    # detected in the same cycle. Merged before zone filtering so the ``face``
    # label flows through rules/alerts exactly like any other object label.
    # No explicit confidence: the face detector uses its configured Face
    # Confidence setting (see docstring).
    # The pass runs on its own clock (face_detection_interval_seconds) and not
    # at all when nothing on this camera consumes a face detection, so object
    # detection keeps its full cadence and its sub-second alert latency.
    # The detector's own preprocess / ONNX session / NMS split refines the single
    # ``STAGE_INFERENCE`` bracket above. It is read with getattr because the
    # detector is duck-typed: a stub, an alternative provider, or a build
    # without the breakdown simply contributes no extra stages.
    _cycle_timer.account_for(_base_inference_timing)
    _face_started = time.perf_counter()
    _face_pass_stamp = _state.face_detection_last_checked.get(camera_id)
    detections = merge_secondary_face_detections(
        image, detections,
        camera_id=camera_id, settings=settings, live_settings=live_settings,
    )
    # The face model runs on its own clock; on a cycle it skipped, no face is
    # not evidence the face left, so the tracker must not age face tracks.
    _face_pass_ran = _state.face_detection_last_checked.get(camera_id) != _face_pass_stamp
    _cycle_timer.add(STAGE_FACE_PASS, (time.perf_counter() - _face_started) * 1000.0)
    detections = normalize_detection_boxes_for_frame(detections, frame)
    _early_dropped = _faint_log_entries(
        normalize_detection_boxes_for_frame(_early_dropped, frame), settings,
    )
    stamp_frame_aspect(detections, frame)
    _telemetry_candidates['detected'] = len(detections)
    # Stamp stable track ids on EVERY detection BEFORE the moving/still filter so
    # the tracker's ``track_displacement`` annotation (net box motion over recent
    # cycles) is available to it. Without it the pixel mask alone governs, and an
    # intermittently-moving subject -- a cat that stops and starts, a person who
    # pauses -- is classified ``still`` on its quiet frames and dropped by the
    # default Moving Only mode, so it flickers in and out of detection. The
    # displacement override keeps a traversing-but-paused track ``moving`` and a
    # genuinely stationary track ``still`` (the parked-car flap). Tracking must
    # run on this full list, not a camera-filtered copy, because the filter and
    # ``still_dwell_candidates`` below both read the annotation off ``detections``
    # directly; the ids then ride through ``filter_detections_for_camera`` into
    # confirmation, recording, dwell, and face amortisation. It annotates in
    # place -- it never adds or drops detections -- so it cannot change what any
    # downstream gate counts.
    _tracking_started = time.perf_counter()
    if camera_motion['active']:
        # The camera is panning, tilting or zooming: every box shifts or
        # rescales with the view, so movement is measured afresh once it stops.
        rebase_object_tracks(camera_id)
    detections = update_object_tracks(
        camera_id, detections,
        hold_labels=frozenset() if _face_pass_ran else frozenset({'face'}),
    )
    # Tag each detection so the checks below can record which ones they drop
    # and why (``_``-prefixed: internal, stripped from every serialised copy).
    _dropped: dict[int, dict[str, Any]] = {}
    for _diag_index, _diag_det in enumerate(detections):
        _diag_det['_diag_id'] = _diag_index

    def _note_dropped(before: list[dict[str, Any]], after: list[dict[str, Any]], reason: Any) -> None:
        kept = {det.get('_diag_id') for det in after}
        for det in before:
            diag_id = det.get('_diag_id')
            if diag_id is None or diag_id in kept or diag_id in _dropped:
                continue
            _dropped[diag_id] = {**det, 'reason': reason(det) if callable(reason) else reason}
    _cycle_timer.add(STAGE_TRACKING, (time.perf_counter() - _tracking_started) * 1000.0)
    _behaviour_started = time.perf_counter()
    # Behavioural engines consume tracked geometry, so pause them while the
    # camera-motion guard is active.  Otherwise PTZ/ego-motion displacement can
    # be interpreted as a subject crossing, dwelling, or appearing at an unusual
    # hour.  Object inference and diagnostics remain available during suppression.
    if not camera_motion['active']:
        # Tier-1 behavioural intelligence: directional line-crossing (tripwire).
        # Runs on the freshly-tracked, pre-filter detections (each carries its
        # prev/current centre) so a subject crossing the line is caught regardless
        # of the object/motion rules. Fully isolated and best-effort -- a bug in
        # behavioural code must never break the detection loop.
        try:
            emit_tripwire_crossings(camera_id, settings, detections)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Tripwire crossing check failed on %s: %s', camera_id, exc)
        # Tier-2 behavioural intelligence: statistical loitering / long-dwell. Runs
        # on the same freshly-tracked detections (each carries its track id + box),
        # learns each zone's normal dwell, and flags an unusually long visit. Also
        # fully isolated and best-effort.
        try:
            emit_loiter_anomalies(camera_id, settings, detections)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Loiter check failed on %s: %s', camera_id, exc)
        # Tier-2 behavioural intelligence: unusual time-of-day. Same freshly-tracked
        # detections; learns each zone's normal active hours and flags activity in a
        # normally-quiet hour. Also fully isolated and best-effort.
        try:
            emit_time_of_day_anomalies(camera_id, settings, detections)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Time-of-day check failed on %s: %s', camera_id, exc)
        # Tier-2 behavioural intelligence: activity spike (unusual burst of activity
        # for the hour). Same freshly-tracked detections; also isolated/best-effort.
        try:
            emit_activity_spike_anomalies(camera_id, settings, detections)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Activity-spike check failed on %s: %s', camera_id, exc)
    # Object settings (default mode + per-label overrides + still-alert
    # thresholds) drive both the still/moving filter and the still-dwell
    # tracker below, so resolve them once per cycle rather than reading the
    # ``objects`` DB setting twice on this ~4 Hz hot path.
    _cycle_timer.add(STAGE_BEHAVIOUR, (time.perf_counter() - _behaviour_started) * 1000.0)
    object_settings = effective_object_settings()
    # Classify moving/still ONCE for this cycle and stamp it on each detection.
    # Both ``still_dwell_candidates`` and ``filter_detections_by_motion_mode``
    # below need the verdict and both run on this same pre-filter list, so
    # annotating up front lets each reuse the stamp instead of repeating the
    # per-box mask classification (the numpy work) a second time.
    detections = annotate_motion_states(
        detections, diff_mask, camera_motion=camera_motion['active'],
    )
    # Kept for continue_active_recording below: the still/moving filter drops
    # still objects, but a still object an active clip already follows may
    # keep that clip going for a moment.
    _annotated_detections = detections
    # Still-dwell candidates must be taken from the UNFILTERED detections: the
    # still/moving filter below drops still detections under the default Moving
    # Only mode, which would otherwise starve every "still for N minutes" alert
    # (an independent axis) of the still classifications it needs. Select them
    # before the filter reassigns ``detections``.
    still_candidates = still_dwell_candidates(
        detections, diff_mask, object_settings,
        camera_motion=camera_motion['active'],
    )
    # Per-label still/moving filter (Objects page): drop detections whose
    # label's detection mode (any/moving/still) does not allow this object's
    # motion state, so a "car moving only" rule never records a parked car.
    # Classified from the Layer-1 diff mask: a box overlapping changed pixels
    # is moving, otherwise still (no mask -> still). Motion-zone rules (Layer
    # 3) are a separate pixel-diff axis and are unaffected. Surviving
    # detections carry a ``motion_state`` annotation for overlays/status.
    _filtering_started = time.perf_counter()
    _detected_count = len(detections)
    _before_motion_mode = detections
    detections = filter_detections_by_motion_mode(
        detections, diff_mask, object_settings,
        camera_motion=camera_motion['active'],
    )
    _note_dropped(_before_motion_mode, detections, lambda det: (
        f"Judged {det.get('motion_state') or 'unknown'}"
        + (f" ({str(det['motion_basis']).replace('_', ' ')})" if det.get('motion_basis') else '')
        + f"; {det.get('label')} is set to {motion_mode_label(motion_mode_for_label(det.get('label'), object_settings))}"
    ))
    _telemetry_candidates['after_motion_mode'] = len(detections)
    _telemetry_rejected[REJECT_MOTION_MODE] = max(
        0, _detected_count - len(detections),
    )
    _cycle_timer.add(STAGE_FILTERING, (time.perf_counter() - _filtering_started) * 1000.0)
    raw_labels = [str(detection.get('label')) for detection in detections if detection.get('label')]
    _camera_scope_input = len(detections)
    object_detections = filter_detections_for_camera(detections, settings)
    # Unwatched labels are filtered out of the log below, so a drop here that
    # reaches the log is a watched object outside the zones watching it.
    _note_dropped(detections, object_detections, lambda det: f"Outside the zones watching {det.get('label')}")
    # A person box drawn on an animal the detector also boxed is that animal:
    # drop it before it can be confirmed or alert. Every box this cycle is the
    # evidence, so an animal no zone watches still explains it.
    _before_overlap = object_detections
    object_detections = drop_people_boxed_as_animals(object_detections, detections)
    _note_dropped(_before_overlap, object_detections, lambda det: overlap_reason(det, detections))
    _telemetry_candidates['after_camera_filter'] = len(object_detections)
    _telemetry_rejected[REJECT_CAMERA_SCOPE] = max(
        0, _camera_scope_input - len(object_detections),
    )
    # Object detector boxes are authoritative over generic motion boxes. This
    # suppresses only motion regions explained by a concrete object that the
    # camera actually accepts; unrelated motion remains available for
    # motion-only rules and recordings.
    motion_detections = filter_motion_detections_by_objects(motion_detections, object_detections)
    shadow_motion_detections = filter_motion_detections_by_objects(shadow_motion_detections, object_detections)

    # Temporal confirmation gate: require an object label to persist across
    # several detection cycles before it can raise an alert or a recording.
    # Defaults to 1 for minimum first-alert latency; set to 2 with a 3-frame
    # window when filtering one-frame false positives is more important.
    # Applied to the zone/label-filtered detections so the window only
    # counts objects this camera actually cares about, and only to the object
    # axis -- motion is already gated separately.
    _confirm_frames = live_settings.get('detection_confirm_frames', 1)
    _confirm_window = live_settings.get('detection_confirm_window', _confirm_frames)
    # Optional spatial-persistence lever (0 = off): when set, a label is
    # confirmed only if its box has persisted in roughly the same place across
    # the confirmation window, filtering noise whose false detections jump
    # around the frame each cycle (rain/snow streaks, IR sensor noise, foliage).
    _confirm_iou = live_settings.get('detection_confirm_iou', 0.0)
    _confirm_input = len(object_detections)
    _confirm_started = time.perf_counter()
    _before_confirmation = object_detections
    object_detections = confirm_object_detections(
        camera_id, object_detections,
        required_frames=_confirm_frames, window_frames=_confirm_window,
        location_iou=_confirm_iou,
        skip_confidence=live_settings.get('detection_confirm_skip_confidence', 0.0),
    )
    try:
        _confirm_spatial = float(_confirm_iou or 0) > 0
    except (TypeError, ValueError):
        _confirm_spatial = False
    _note_dropped(
        _before_confirmation, object_detections,
        f'Not confirmed yet (needs {_confirm_frames} of {_confirm_window} cycles'
        + (', in the same place or as the same tracked object)' if _confirm_spatial else ')'),
    )
    _cycle_timer.add(STAGE_CONFIRMATION, (time.perf_counter() - _confirm_started) * 1000.0)
    _telemetry_candidates['after_confirmation'] = len(object_detections)
    _telemetry_rejected[REJECT_CONFIRMATION] = max(
        0, _confirm_input - len(object_detections),
    )
    _ptz_tracking_status = None
    # PTZ auto-tracking (opt-in per camera): start following only an object
    # that passed the zone + confirmation gates above, but keep following it
    # through every tracked detection, since zones stop lining up with the
    # scene once the camera pans. Best-effort: camera commands run on their
    # own worker threads and a failure here never breaks the cycle.
    try:
        _ptz_tracking_status = update_auto_tracking(
            camera_id, settings,
            candidates=object_detections,
            visible=_annotated_detections,
            image=image if frame_is_numpy else None,
        )
        update_live_detection_status(camera_id, ptz_tracking=_ptz_tracking_status)
    except Exception as exc:  # noqa: BLE001
        logger.warning('PTZ auto-tracking failed on %s: %s', camera_id, exc)
    # (Track ids were stamped earlier, before the confirmation gate, so the
    # motion-mode filter could read ``track_displacement``; the ids still
    # thread through to the history + recording rows from there.)
    # Face recognition (Stage 2c): annotate each ``face`` detection in place with
    # the recognised person (or mark it unknown), amortised across the stable
    # track id. A no-op unless recognition is enabled with a loaded model and the
    # frame is a numpy array, so non-face cameras pay nothing. The annotations
    # ride through the ``{**det}`` copies below into the stored event + overlay.
    #
    # ``annotate_face_identities`` needs the DECODED pixel frame -- it crops each
    # face for embedding. ``image`` is that frame on the numpy path; on the bytes
    # path (event/snapshot queue) decode it once here. ``frame`` is only the
    # per-frame METADATA dict (timestamp/size): passing it -- the long-standing
    # bug -- handed annotate an object with no ``.shape``, so it early-returned
    # every cycle. That silently disabled ALL live identity work: no recognised
    # names, no unknown-face captures for review, and no unknown/known face
    # alerts, since every one of those reads the annotations produced here.
    if frame_is_numpy:
        recognition_frame = image
    elif isinstance(image, (bytes, bytearray)):
        try:
            import cv2
            import numpy as np
            recognition_frame = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception as exc:  # pragma: no cover - defensive: never fail the cycle on a decode
            logger.debug('Face recognition frame decode failed for camera %s: %s', camera_id, exc)
            recognition_frame = None
    else:
        recognition_frame = None
    _identity_started = time.perf_counter()
    object_detections = annotate_face_identities(camera_id, object_detections, recognition_frame)
    _cycle_timer.add(STAGE_FACE_IDENTITY, (time.perf_counter() - _identity_started) * 1000.0)
    # Zone-stamp faces so People rules can be scoped: a face inside an enabled
    # zone carries that zone's id, and scoped rules (_unknown:<zone>, per-person
    # rules with camera_id/zone_id) fire only for faces stamped accordingly.
    # Additive-only and skipped entirely when no faces were found this cycle.
    if any(str(d.get('label') or '').strip().lower() == 'face' for d in object_detections):
        _face_zones = [
            z for z in (settings.get('detection') or {}).get('zones', [])
            if z.get('enabled', True)
        ]
        for _det in object_detections:
            if str(_det.get('label') or '').strip().lower() != 'face' or 'zone_id' in _det:
                continue
            _fz = next((z for z in _face_zones if detection_matches_zone(_det, z)), None)
            if _fz is not None:
                _det['zone_id'] = str(_fz.get('id') or _fz.get('name') or '')
                _det['zone_name'] = str(_fz.get('name') or '').strip() or None
    # Alert-on-unknown (Stage 2c): a detected face matching no enrolled person is
    # itself alertable ("stranger"), one alert per track per configured rule.
    # Empty unless recognition is enabled and an unknown-person face-detection
    # rule is on (global _unknown, or a zone-scoped _unknown:<zone> variant
    # created by the Zones page People card).
    #
    # Faces go through the presence gate (app.event_presence) like objects, so
    # no cooldown: one alert per person while they stay in view, and one per
    # unknown-face rule while the number of strangers in view does not grow.
    _presence_cycle_start = time.time()
    _presence_absence = absence_seconds(_state.camera_event_recording_config(settings))
    _presence_recording_id = active_recording_id(camera_id)
    _unknown_face_alerts = (
        [] if camera_motion['active'] else _new_unknown_face_alerts(
            camera_id, unknown_face_alerts(camera_id, object_detections),
            absence=_presence_absence, recording_id=_presence_recording_id,
        )
    )
    # Face detection rules (Stage 2c): per-person alert rules with email/push
    # notifications, checked after identity annotation so each face carries
    # person_name/person_id annotations. PTZ movement suppresses these direct
    # face-rule paths too; they do not pass through AlertEngine's unknown-state
    # guard.
    _known_face_rule_alerts = (
        [] if camera_motion['active'] else _new_known_face_alerts(
            camera_id, known_face_rules_for_camera(camera_id, object_detections),
            absence=_presence_absence, recording_id=_presence_recording_id,
        )
    )
    # Still-dwell alerts (Objects page: "still for N minutes"): a label that
    # has been detected continuously still for its configured threshold fires
    # one dwell alert per streak, watching the same Layer-1 background-
    # absorption classification the still/moving filter applies. The streak
    # resets the moment the subject moves or leaves the frame.
    #
    # Fed from ``still_candidates`` (still detections for still-alert labels,
    # taken before the moving/still filter) rather than ``object_detections``:
    # under the default Moving Only mode the filter drops still detections, so
    # sourcing the tracker from the filtered set would never let a default-mode
    # label accrue a streak. The candidates are zone-scoped the same way the
    # normal object pipeline is so a dwell alert still respects which zones the
    # camera monitors and whether object detection is enabled.
    _still_thresholds = still_alert_thresholds(object_settings)
    if _still_thresholds:
        # Zone-scope the still candidates (skip when there are none so we do no
        # zone work on the common empty cycle). An empty input still runs the
        # tracker so a streak resets when its subject moves or leaves the frame
        # -- but only on a clear cycle: during PTZ/ego-motion the empty list
        # carries no information about the subject, so the tracker pauses
        # instead of wiping every streak (a 0.4s nudge would otherwise reset
        # all long-dwell alerts to zero).
        _dwell_input = filter_detections_for_camera(still_candidates, settings) if still_candidates else []
        dwell_detections = update_still_dwell_alerts(
            camera_id, _dwell_input, _still_thresholds,
            camera_motion=camera_motion['active'],
        )
    else:
        dwell_detections = []
    if dwell_detections:
        # Stamp the containing zone so a dwell detection can also match the
        # operator's existing zone rules for that label -- a "person -> email"
        # rule then notifies on a long-still person exactly like it would on
        # any other detection, without any extra per-label notification setup.
        _dwell_enabled_zones = [
            zone for zone in (settings.get('detection') or {}).get('zones', [])
            if zone.get('enabled', True)
        ]
        _zone_stamp: list[dict[str, Any]] = []
        for _dwell in dwell_detections:
            _matching_zone = next(
                (zone for zone in _dwell_enabled_zones if detection_matches_zone(_dwell, zone)),
                None,
            )
            _zone_stamp.append({
                **_dwell,
                'zone_name': str(_matching_zone.get('name') or _matching_zone.get('id') or '').strip() or None if _matching_zone else None,
                'zone_id': str(_matching_zone.get('id') or _matching_zone.get('name') or '') if _matching_zone else '',
            })
        dwell_detections = _zone_stamp
    zone_rules = zone_object_alert_rules(settings)
    has_object_zone_rules = any((zone.get('enabled', True) and zone.get('monitor_objects', True) and any((rule.get('enabled', True) and str(rule.get('label') or '').strip() for rule in zone.get('object_rules') or [])) for zone in (settings.get('detection') or {}).get('zones', [])))
    # Zone-scoped faces need the geometry-stamping pass too, even when no
    # zone monitors OBJECTS: without it face detections skip zone matching and
    # the AlertEngine would fire zone-face rules for faces anywhere in frame.
    has_face_zone_rules = _camera_has_face_zone_rules(settings)
    _zone_match_needed = has_object_zone_rules or has_face_zone_rules
    # Detections remain visible while the camera moves. PTZ invalidates the
    # moving/still classification, so permit only labels configured for Any;
    # Moving Only and Still Only remain visible but cannot alert or record until
    # the camera settles. The explicit marker lets AlertEngine accept this
    # narrow PTZ-safe path without weakening its unknown-state guard globally.
    alertable_object_detections = [
        (
            {**detection, 'allow_camera_motion_alert': True}
            if detection.get('motion_state') == 'unknown'
            and object_detection_allowed_during_camera_motion(detection, object_settings)
            else detection
        )
        for detection in object_detections
        if detection.get('motion_state') != 'unknown'
        or object_detection_allowed_during_camera_motion(detection, object_settings)
    ]
    if camera_motion['active']:
        # A pan stamps every surviving detection ``unknown``. Those whose label
        # mode does not allow alerting during camera motion stay visible but
        # cannot alert or record until the camera settles -- count them so a
        # missed alert during a pan is attributable rather than mysterious.
        _telemetry_rejected[REJECT_CAMERA_MOTION] = max(
            0, len(object_detections) - len(alertable_object_detections),
        )
    _zone_started = time.perf_counter()
    object_alert_detections = zone_alert_detections(settings, alertable_object_detections) if _zone_match_needed else list(alertable_object_detections)
    _cycle_timer.add(STAGE_ZONE_RULES, (time.perf_counter() - _zone_started) * 1000.0)
    if _zone_match_needed:
        _telemetry_rejected[REJECT_ZONE] = max(
            0, len(alertable_object_detections) - len(object_alert_detections),
        )
    record_only_detections = [d for d in alertable_object_detections if zone_record_on_detect(d, settings) and (not zone_object_rule_matches(settings, d, action='alert'))] if _zone_match_needed else []
    _note_dropped(
        object_detections, alertable_object_detections,
        'Camera moving: only objects set to Moving & Still count',
    )
    if _zone_match_needed:
        _note_dropped(
            alertable_object_detections, list(object_alert_detections) + record_only_detections,
            lambda det: rule_rejection_reason(settings, det),
        )
    # Object priority over motion across cycles (see app.motion_object_priority):
    # motion in a zone an alertable object occupied within the grace window is
    # the object's, and motion-only is held that long for an object to appear.
    # Released motion is stamped and pictured at the moment it began.
    _motion_hold_reason = None
    _priority_objects = list(object_alert_detections) + record_only_detections
    _motion_zones = [
        zone for zone in (settings.get('detection') or {}).get('zones', [])
        if zone.get('enabled', True) and zone.get('monitor_motion', True)
    ]
    try:
        _motion_grace = min(MOTION_OBJECT_MAX_GRACE_SECONDS, max(0.0, float(
            live_settings.get('motion_object_grace_seconds', MOTION_OBJECT_DEFAULT_GRACE_SECONDS)
        )))
    except (TypeError, ValueError):
        _motion_grace = MOTION_OBJECT_DEFAULT_GRACE_SECONDS
    _arbitration = _state.motion_object_arbiter.resolve(
        camera_id,
        now=frame_capture_ts,
        grace_seconds=_motion_grace,
        motion_detections=motion_detections,
        object_zone_keys=motion_object_zone_keys(_motion_zones, _priority_objects, detection_matches_zone),
        image=image,
        image_is_numpy=frame_is_numpy,
        has_objects=bool(_priority_objects),
    )
    # Keep every firing motion zone in the playback track. Retaining only the
    # strongest zone made multi-zone motion clips show a box for one region while
    # silently omitting movement elsewhere in the same frame. Motion attributed
    # to an object is left out, so playback shows the car rather than a
    # "Motion" box for its headlight spill; held motion stays in (it is real
    # movement, and a released motion event needs its boxes).
    _attributed_zones = set(_arbitration.attributed_zones)
    motion_history_detections = [
        {**motion, 'label': 'motion', 'motion_event': True}
        for motion in motion_detections
        if str(motion.get('zone_id') or motion.get('zone_name') or '').strip() not in _attributed_zones
    ]
    record_live_detection_history(
        camera_id,
        list(object_alert_detections) + record_only_detections + motion_history_detections + dwell_detections,
        sample_ts=frame_capture_ts,
        live_config=live_settings,
        dropped=[
            det for det in _early_dropped + list(_dropped.values())
            if camera_watches_label(settings, det.get('label'))
        ],
        ptz=_ptz_tracking_status,
    )
    # An active clip continues on what follows its object: a still object it
    # already follows, or motion (see continue_active_recording). Only on cycles
    # with no recordable object - those extend through the normal path below -
    # and never during camera motion, when pixel change is not scene activity.
    if not object_alert_detections and not record_only_detections and not camera_motion['active']:
        try:
            continue_active_recording(
                camera_id=camera_id,
                frame_ts=frame_capture_ts,
                motion_detections=motion_detections or shadow_motion_detections,
                still_detections=[d for d in _annotated_detections if d.get('motion_state') == 'still'],
                recording_config=_state.camera_event_recording_config(settings),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('Recording continuation failed on %s: %s', camera_id, exc)
    motion_detections = _arbitration.motion_detections
    if _arbitration.event_ts is not None:
        frame_capture_ts = _arbitration.event_ts
        image = _arbitration.image
        frame_is_numpy = _arbitration.image_is_numpy
    if _arbitration.held_zones and not motion_detections:
        _motion_hold_reason = (
            f'Motion held up to {_motion_grace:.0f}s so an object in the same zone takes priority.'
        )
    alert_detections = list(object_alert_detections) + record_only_detections
    for _mot in motion_detections:
        alert_detections.append({**_mot, 'label': 'motion', 'motion_event': True})
    for _dwell in dwell_detections:
        alert_detections.append(_dwell)
    _telemetry_candidates['alertable'] = (
        len(object_alert_detections) + len(record_only_detections)
    )
    _monitored_zones = [
        z for z in (settings.get('detection') or {}).get('zones', [])
        if z.get('enabled', True) and z.get('monitor_objects', True)
    ]
    object_reason = _below_threshold_object_reason(detections, _monitored_zones)
    if not alert_detections and not _unknown_face_alerts and not _known_face_rule_alerts:
        reason = _motion_hold_reason or _no_object_match_reason(detections, raw_labels, _monitored_zones)
        update_live_detection_status(camera_id, state='checked', reason=reason, object_reason=object_reason, detected_labels=raw_labels, matched_labels=[], detections=list(detections), frame_timestamp=frame_capture_ts, motion_confidence=frame_motion_confidence, motion_fraction=raw_motion_fraction)
        _telemetry_finish(_telemetry_mode)
        return None
    _alerts_started = time.perf_counter()
    triggered = _state.alerts.process(alert_detections, rules=zone_rules)
    # Zone-rule alerts lead ``triggered``; only those of NEW objects survive
    # the presence gate below (dwell and face alerts carry their own gating).
    _zone_alert_count = len(triggered)
    _cycle_timer.add(STAGE_ALERTS, (time.perf_counter() - _alerts_started) * 1000.0)
    # Dwell alerts are first-class in-app alerts added directly to ``triggered``
    # (they fire once per still streak by construction, so a zone-style rule
    # with cooldowns would either flood alerts or risk being skipped). The
    # alert-history row uses a descriptive name; email/push stay untouched
    # unless the dwell detection also matched one of the operator's real zone
    # rules above (which still happens normally via the stamped zone_id).
    for _dwell in dwell_detections:
        _dwell_label = str(_dwell.get('label') or '').strip()
        if not _dwell_label:
            continue
        triggered.append({
            'rule_name': f"Still for {_dwell.get('still_alert_minutes')} min: {_dwell_label}",
            'label': _dwell_label,
            'confidence': float(_dwell.get('confidence') or 0),
            'message': f"Alert triggered: {_dwell_label} still for {_dwell.get('still_alert_minutes')} minutes",
            'motion_state': 'still',  # a dwell streak only fires while still
        })
    # Unknown-face alerts (Stage 2c), added directly to ``triggered`` like
    # dwell alerts: already filtered to new ones by the presence gate above.
    for _unknown in _unknown_face_alerts:
        triggered.append({
            'rule_name': 'Unknown face',
            'label': 'face',
            # Which scoped ``_unknown`` rule(s) fired -- dispatch unions the
            # email/push config of exactly these.
            'face_rule_ids': _unknown.get('face_rule_ids') or [],
            'zone_id': str(_unknown.get('zone_id') or ''),
            'confidence': float(_unknown.get('confidence') or 0),
            'message': 'Alert triggered: unrecognized face detected',
        })
    # Known-face rules: per-person alerts that fire when a face rule is
    # enabled and the person appears (presence-gated above).
    for _known in _known_face_rule_alerts:
        triggered.append(_known)
    triggered_rule_names = {str(alert.get('rule_name') or '') for alert in triggered}
    triggered_labels = {str(alert.get('label') or '').lower() for alert in triggered}
    _confident_object_detections: list[dict[str, Any]] = []
    if has_object_zone_rules:
        for _det in alertable_object_detections:
            _zone_name = zone_name_for_detection(settings, _det)
            if _zone_name or zone_record_on_detect(_det, settings):
                _confident_object_detections.append({**_det, 'zone_name': _zone_name or None})
    else:
        # No object-monitoring zones with enabled rules: stamp the first
        # enabled zone whose geometry contains the detection so the playback
        # cards / recordings list still show a zone name for cameras that
        # monitor motion only (or have zones but no object rules yet).
        _enabled_zones = [
            zone for zone in (settings.get('detection') or {}).get('zones', [])
            if zone.get('enabled', True)
        ]
        _confident_object_detections = [
            {
                **_det,
                'zone_name': next((
                    str(zone.get('name') or zone.get('id') or '').strip() or None
                    for zone in _enabled_zones
                    if detection_matches_zone(_det, zone)
                ), None),
            }
            for _det in alertable_object_detections
        ]
    recording_detections = [{**detection, 'alert_matched': bool(zone_detection_alert_rule_names(settings, detection) & triggered_rule_names) if has_object_zone_rules else str(detection.get('label') or '').lower() in triggered_labels, 'alert_triggered': zone_record_on_detect(detection, settings)} for detection in _confident_object_detections]
    # Each motion detection is stamped with the record decision for ITS OWN
    # zone, so motion in a record-off zone cannot piggyback on a record-on
    # rule in a different zone. Appending every firing zone (not just the
    # strongest) keeps the event's detection list faithful when multiple
    # zones move at once.
    for _mot in motion_detections:
        _motion_zone_key = str(_mot.get('zone_id') or _mot.get('zone_name') or '')
        _motion_record = zone_motion_record_on_detect(settings, _motion_zone_key)
        # alert_triggered tracks ONLY the motion rule's own Record flag: an
        # enabled Email/Push alert on the motion rule must not silently force a
        # recording when Record is off. The alert itself still fires via
        # ``triggered_labels`` (visible as ``alert_matched``) and delivery.
        recording_detections.append({**_mot, 'label': 'motion', 'motion_event': True, 'alert_matched': 'motion' in triggered_labels, 'alert_triggered': _motion_record})
    for _dwell in dwell_detections:
        # A dwell alert always records when the camera can: the whole point is
        # to capture the subject (a package, a pet) that has been left in view.
        recording_detections.append({**_dwell, 'alert_matched': True, 'alert_triggered': True})
    matched_labels = [str(detection.get('label')) for detection in alert_detections if detection.get('label')]
    camera_recording_config = _state.camera_event_recording_config(settings)
    frame_capture_time = datetime.fromtimestamp(frame_capture_ts, tz=timezone.utc).isoformat()
    # Presence gate (app.event_presence) - no cooldowns. An object raises one
    # event when it appears and none while it stays in view; another object of
    # the same type raises one when more of that type are in view together than
    # before; a different type raises its own at once. Counting (not track ids)
    # keeps an object that briefly hides - and comes back under a new track id -
    # from alerting again. A type is forgotten once unseen for the extension
    # step and its recording has ended, so it re-alerts only in new footage.
    # This gates EVENT creation for alert-only cameras too, which would
    # otherwise create an event + snapshot every detection cycle.
    _presence_counts: dict[str, int] = {}
    for _det in _confident_object_detections:
        _lbl = str(_det.get('label') or '').strip().lower()
        if _lbl:
            _presence_counts[_lbl] = _presence_counts.get(_lbl, 0) + 1
    if motion_detections:
        _presence_counts['motion'] = 1
    _presence_new = observe_presence(
        camera_id, _presence_counts, absence=_presence_absence,
        recording_id=active_recording_id(camera_id),
    )
    _presence_new = drop_covered_motion(camera_id, _presence_new)
    # Zone-rule alerts only for the objects that are new this cycle: a person
    # walking into the cat's footage alerts for the person, not the cat again.
    # A dwell's own zone-rule alert stays: it carries the dwell's email/push.
    _alertable_labels = _presence_new | {
        str(_dwell.get('label') or '').strip().lower() for _dwell in dwell_detections
    }
    # The alert engine reports one alert per matching detection, so two people
    # in one frame are two alerts on the same rule: one notification (and one
    # alert row) per rule and label is enough, led by the strongest detection.
    _zone_alerts: dict[tuple[str, str], dict[str, Any]] = {}
    for _zone_alert in triggered[:_zone_alert_count]:
        _zone_label = str(_zone_alert.get('label') or '').strip().lower()
        if _zone_label not in _alertable_labels:
            continue
        _zone_key = (str(_zone_alert.get('rule_name') or ''), _zone_label)
        _kept_alert = _zone_alerts.get(_zone_key)
        if _kept_alert is None or float(_zone_alert.get('confidence') or 0) > float(_kept_alert.get('confidence') or 0):
            _zone_alerts[_zone_key] = _zone_alert
    triggered = list(_zone_alerts.values()) + triggered[_zone_alert_count:]
    # Dwell and face alerts are already new by construction (a dwell fires once
    # per still streak; faces went through the presence gate above), and face
    # recognition usually lands a cycle or two AFTER the event for the person
    # who walked in, so each gets its own event.
    if not (_presence_new or dwell_detections or _unknown_face_alerts or _known_face_rule_alerts):
        # Snapshot before extending: the extension retires a stale session itself.
        with _state.active_rtsp_recordings_lock:
            capture_blocked = (
                camera_id in _state.active_rtsp_recordings
                or bool(_state.last_rtsp_capture_capped.get(camera_id))
            )
        extended_recording_id = extend_active_rtsp_recording(camera_id=camera_id, event_time=frame_capture_time, recording_config=camera_recording_config, detections=recording_detections)
        # Nothing new suppresses duplicate EVENTS, not continuing footage. A
        # capped/frozen capture cannot extend; let a recording-enabled stream
        # create one follow-on event/clip through the normal path below (with
        # no alert: nothing in it is new). With no capture at all, that applies
        # only when the last clip was cut at its Max Clip ceiling.
        needs_follow_on = (
            extended_recording_id is None
            and capture_blocked
            and _camera_has_live_alert_stream(settings)
            and _state.recording_service.should_record(recording_detections, camera_recording_config)[0]
        )
        if not needs_follow_on:
            _suppressed_reason = (
                'Ongoing detection extended the active recording; nothing new in view.'
                if extended_recording_id is not None
                else 'Ongoing detection; nothing new in view.'
            )
            update_live_detection_status(camera_id, state='checked', reason=_suppressed_reason, object_reason=object_reason, detected_labels=raw_labels, matched_labels=matched_labels, detections=recording_detections, recording_id=extended_recording_id, motion_confidence=frame_motion_confidence, motion_fraction=raw_motion_fraction)
            _telemetry_finish(_telemetry_mode)
            return None
    event_time = frame_capture_time
    _event_started = time.perf_counter()
    if frame_is_numpy:
        image_bytes = _encode_frame_jpeg(image)
    else:
        image_bytes = image
    snapshot_path = _state.storage.save_image_snapshot(image_bytes, f'{camera_id}.jpg')
    # Bake the gallery thumbnail once at capture time (annotated + downscaled),
    # so the Snapshots gallery serves a stored file instead of decoding,
    # annotating and re-encoding the full frame for every row. ``None`` when it
    # cannot be rendered here (e.g. no cv2) - the snapshot endpoint then falls
    # back to rendering on the fly.
    thumbnail_bytes = build_event_thumbnail(image_bytes, recording_detections)
    thumbnail_path = (
        _state.storage.save_image_snapshot(thumbnail_bytes, f'{camera_id}.thumb.jpg')
        if thumbnail_bytes else None
    )
    _rule_by_name = {str(r.get('name') or ''): r for r in zone_rules or []}

    # An event's alert (the "Alert" badge - "an alert notification was fired
    # for this event" - read from its alert_history rows, plus the
    # alert_triggered flag) means a notification is due NOW, as the sound and
    # behaviour monitors already record it, not merely that a rule matched. A
    # zone rule notifies when Email or Push is on and its notify window covers
    # now: the window test alert_dispatch applies before delivering. Outside the window the detection is still an event (and
    # still records); it just is not an alert. Dwell and face alerts carry
    # their own delivery rules and keep their rows.
    def _alert_notifies_now(alert: dict[str, Any]) -> bool:
        rule = _rule_by_name.get(str(alert.get('rule_name') or ''))
        if rule is None:
            return True
        if not rule.get('enabled', True):
            return False
        # Email or Push on: the same "deliverable" test zone_object_rule_matches
        # applies when it decides a rule is an alert rule at all.
        has_channel = bool(rule.get('push_enabled') or rule.get('email_enabled'))
        return has_channel and _rule_notify_active_now(rule.get('schedule') or rule)

    alert_rows = [
        {'created_at': datetime.now(timezone.utc).isoformat(), 'rule_name': alert['rule_name'], 'label': alert['label'], 'confidence': alert['confidence'], 'message': alert['message']}
        for alert in triggered
        if _alert_notifies_now(alert)
    ]
    event_alerted = bool(alert_rows)
    try:
        event_id = _state.database.add_event_with_alerts(created_at=event_time, source='rtsp', snapshot_path=snapshot_path, thumbnail_path=thumbnail_path, detections=recording_detections, alerts=alert_rows, alert_triggered=event_alerted, metadata={'camera_id': settings.get('id'), 'camera_name': settings.get('name'), 'ai_backend': ai_state['configured_backend'], 'detector_backend': ai_state['active_backend'], 'source': 'live-stream', **face_identity_metadata(recording_detections)})
    except Exception:
        # The event row never landed, so nothing references the images just
        # written: remove them rather than leak one pair per failed cycle.
        for _orphan in (snapshot_path, thumbnail_path):
            if _orphan:
                Path(_orphan).unlink(missing_ok=True)
        raise
    recording_id = attach_event_recording(event_id, event_time, 'rtsp', recording_detections, camera_id=camera_id, recording_config=camera_recording_config)
    if recording_id is not None:
        bind_presence_recording(camera_id, recording_id, seen_since=_presence_cycle_start)
    _cycle_timer.add(STAGE_EVENT, (time.perf_counter() - _event_started) * 1000.0)
    # Alert rows were written in the same transaction as the event above. The
    # recording link is applied afterwards because clip creation is asynchronous
    # with respect to event persistence.
    notifying = [alert for alert in triggered if _alert_notifies_now(alert)]
    if notifying:
        # Routed through AI verification when enabled for this camera; that
        # path fails open to the direct delivery below. Only alerts that will
        # notify go through it: verifying one outside its notify window spends
        # an AI call on a notification that delivery would then skip.
        submit_alert_notification_with_verification(
            notifying, event_id, zone_rules,
            camera_id=camera_id, camera_name=str(settings.get('name') or ''), detections=recording_detections,
        )
    else:
        # No alert: in 'describe all events' mode the AI pool still captions
        # the snapshot in the background so the event is searchable.
        submit_event_description(event_id, camera_id=camera_id, camera_name=str(settings.get('name') or ''),
                                 detections=recording_detections)
    triggered_rule_names = {str(alert.get('rule_name') or '') for alert in triggered}
    email_rules = [
        rule for rule in zone_rules
        if rule.get('enabled', True)
        and rule.get('email_enabled')
        and str(rule.get('name') or '') in triggered_rule_names
        and _rule_notify_active_now(rule.get('schedule') or rule)
    ]
    email_recipients = sorted({recipient for rule in email_rules for recipient in rule.get('email_recipients', [])})
    update_live_detection_status(camera_id, state='alerted' if triggered else 'checked', reason='Alert matched.' if triggered else 'Event recorded without an alert: no alert rule notifies for what is new in view.', object_reason=object_reason, detected_labels=raw_labels, matched_labels=matched_labels, detections=recording_detections, triggered_alerts=triggered, event_id=event_id, recording_id=recording_id, recording_state='linked' if recording_id is not None else 'skipped', recording_reason='Recording linked.' if recording_id is not None else recording_skip_reason(recording_detections, _state.camera_event_recording_config(settings)), email_enabled_rules=len(email_rules), email_recipients=email_recipients, email_attempted=bool(triggered and email_recipients and effective_email_alert_settings().get('enabled')), motion_confidence=frame_motion_confidence, motion_fraction=raw_motion_fraction)
    _telemetry_finish(_telemetry_mode, event_created=event_id is not None)
    return event_id
