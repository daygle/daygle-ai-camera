"""Live-alert-monitor lifecycle and live-stream detection helpers.

Phase-32: lifecycle cluster (run_live_alert_monitor_once, live_alert_monitor_loop, etc.)
Phase-K: live-stream detection entry points (queue_live_stream_alerts,
         _encode_frame_jpeg, process_live_stream_alerts)
"""

from __future__ import annotations
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import app.state as _state
from app.ai_settings import ai_status_payload
from app.alert_dispatch import (
    _rule_notify_active_now,
    compute_minimum_rule_confidence,
    deliver_alert_notifications as _deliver_alert_notifications,
)
from app.camera_health import _check_cameras_health
from app.camera_instance import read_ingest_frame
from app.config_facades import effective_ai_config, effective_email_alert_settings, effective_live_config
from app.detection_state import (
    confirm_object_detections,
    detect_frame_motion,
    detection_label_set,
    record_live_detection_history,
)
from app.detection_status import _camera_has_live_alert_stream, update_live_detection_status
from app.detector import DetectorUnavailableError
from app.event_debounce import (
    clear_live_camera_backoff,
    live_event_fresh_labels,
    remember_live_event,
    schedule_live_camera_backoff,
)
from app.recording_extension import (
    _make_continuous_chunk_callback,
    attach_event_recording,
    extend_active_rtsp_recording,
    recording_skip_reason,
)
from app.backup import purge_camera_diagnostics_by_policy
from app.utils import build_stream_url, build_recording_stream_url, normalize_bool_setting
from app.zone_detection import (
    detection_matches_zone,
    filter_detections_for_camera,
    normalize_detection_boxes_for_frame,
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


def run_live_alert_monitor_once(live_settings: dict[str, Any] | None=None) -> int:
    if live_settings is None:
        live_settings = effective_live_config()
    background_detection_enabled = normalize_bool_setting(live_settings.get('background_detection_enabled'), True)
    processed = 0
    for selected_config in list(_state.cameras_config):
        camera_id = str(selected_config.get('id') or 'camera')
        if selected_config.get('enabled') is False:
            continue
        if not _camera_has_live_alert_stream(selected_config):
            continue
        now = time.time()
        stream_url = build_stream_url(selected_config)
        recording_stream_url = build_recording_stream_url(selected_config)
        cam_rec_config = _state.camera_event_recording_config(selected_config)
        if stream_url:
            _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config, recording_stream_path=recording_stream_url)
            if cam_rec_config.get('continuous'):
                # Continuous recordings must use the optional high-resolution
                # stream too; otherwise dual-stream cameras silently save their
                # low-resolution detection stream. The chunk worker uses
                # ``-c:v copy``, so the source resolution and FPS are preserved.
                _state.recording_service.start_continuous_chunk_recording(stream_url=recording_stream_url or stream_url, camera_id=camera_id, recording_config=cam_rec_config, on_chunk_complete=_make_continuous_chunk_callback(camera_id))
        if not background_detection_enabled:
            continue
        with _state._live_backoff_lock:
            retry_after = _state.live_detection_retry_after.get(camera_id, 0)
        if retry_after and now < retry_after:
            continue
        detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.5))
        with _state.live_detection_worker_lock:
            if camera_id in _state.active_live_detection_cameras:
                continue
            if now - _state.live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
                continue
            _state.live_detection_last_checked[camera_id] = now
            _state.active_live_detection_cameras.add(camera_id)

        # Bind BOTH the camera id and its config as default args (evaluated
        # now, per loop iteration). ``selected_config`` was previously read as
        # a free variable, which late-binds: the daemon thread runs after the
        # loop has advanced, so in a multi-camera setup every detection thread
        # saw a LATER camera's config -- evaluating this camera's frame against
        # the wrong camera's zones/rules. Snapshotting it as a default arg (like
        # ``cid``) captures the correct per-iteration value.
        def _detect_bg(cid: str=camera_id, cfg: dict[str, Any]=selected_config) -> None:
            camera_cfg = dict(cfg)
            try:
                sample = read_ingest_frame(cid)
                if sample is None:
                    if not _state.recording_service.ingest_has_produced_frame(cid):
                        return
                    cam_instance = _state.camera_instances.get(cid)
                    if cam_instance is not None and hasattr(cam_instance, 'read_jpeg'):
                        try:
                            import cv2
                            import numpy as np
                            jpeg_bytes, _frame_meta = cam_instance.read_jpeg()
                            img = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
                            if img is not None:
                                h, w = img.shape[:2]
                                sample = (img, {'frame_number': 0, 'timestamp': time.time(), 'width': w, 'height': h})
                        except Exception:
                            pass
                    if sample is None:
                        schedule_live_camera_backoff(cid, 'No fresh frame available from the camera ingest.')
                        return
                image, frame = sample
                clear_live_camera_backoff(cid)
                process_live_stream_alerts(image, frame, camera_cfg, enforce_interval=False)
            except Exception as exc:
                logger.warning('Background live alert check failed for camera %s: %s', cid, exc)
                schedule_live_camera_backoff(cid, str(exc))
            finally:
                with _state.live_detection_worker_lock:
                    _state.active_live_detection_cameras.discard(cid)
        threading.Thread(target=_detect_bg, name=f'live-detection-{camera_id}', daemon=True).start()
        processed += 1
    return processed

def _prune_frame_motion_state() -> None:
    """Remove background model and scan timestamp entries for cameras no longer in the active config."""
    active_ids = {str(cfg.get('id') or '') for cfg in _state.cameras_config if cfg.get('id')}
    with _state._frame_motion_lock:
        stale = [cid for cid in _state._frame_motion_prev if cid not in active_ids]
        for cid in stale:
            del _state._frame_motion_prev[cid]
    for cid in stale:
        _state._periodic_scan_last_ts.pop(cid, None)
        _state._frame_motion_error_cameras.discard(cid)
    if stale:
        with _state.live_detection_confirm_lock:
            for cid in stale:
                _state.live_detection_confirm_history.pop(cid, None)
        logger.debug('Pruned stale motion state for cameras: %s', stale)

def live_alert_monitor_loop() -> None:
    _last_prune = 0.0
    while not _state.live_alert_monitor_stop.is_set():
        live_settings = effective_live_config()
        run_live_alert_monitor_once(live_settings)
        _check_cameras_health()
        now = time.time()
        if now - _last_prune > 300:
            _prune_frame_motion_state()
            purge_camera_diagnostics_by_policy()
            _last_prune = now
        interval = max(0.1, float(live_settings.get('detection_interval_seconds', 0.5)))
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


def queue_live_stream_alerts(image_bytes: bytes, frame: dict[str, Any], settings: dict[str, Any]) -> None:
    camera_id = str(settings.get('id') or 'camera')
    stream_url = build_stream_url(settings)
    if stream_url:
        _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=_state.camera_event_recording_config(settings), recording_stream_path=build_recording_stream_url(settings))
    live_cfg = effective_live_config()
    if normalize_bool_setting(live_cfg.get('background_detection_enabled'), True):
        return
    detection_interval_seconds = float(live_cfg.get('detection_interval_seconds', 0.5))
    now = time.time()
    with _state.live_detection_worker_lock:
        if camera_id in _state.active_live_detection_cameras:
            return
        if now - _state.live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
            return
        _state.live_detection_last_checked[camera_id] = now
        _state.active_live_detection_cameras.add(camera_id)

    def detect() -> None:
        try:
            process_live_stream_alerts(image_bytes, frame, settings, enforce_interval=False)
        except Exception as exc:
            logger.warning('Live detection failed for camera %s: %s', camera_id, exc)
            update_live_detection_status(camera_id, state='error', reason=str(exc), detections=[])
        finally:
            with _state.live_detection_worker_lock:
                _state.active_live_detection_cameras.discard(camera_id)
    threading.Thread(target=detect, name=f'live-detection-{camera_id}', daemon=True).start()


def process_live_stream_alerts(image: Any, frame: dict[str, Any], settings: dict[str, Any], *, enforce_interval: bool = True) -> int | None:
    camera_id = str(settings.get('id') or 'camera')
    live_settings = effective_live_config()
    detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.5))
    # Master AI toggle (ai.enabled): when disabled, skip inference and every
    # downstream effect (detections, alerts, AI-triggered recordings). This is
    # the single gate for both the background monitor thread and the
    # event-driven queue path, so toggling AI off stops object detection
    # everywhere without tearing down the detector session.
    ai_config = effective_ai_config()
    if not normalize_bool_setting(ai_config.get('enabled'), True):
        update_live_detection_status(camera_id, state='skipped', reason='AI detection is disabled.', detections=[])
        return None
    if not hasattr(_state.detector, 'detect_image'):
        update_live_detection_status(camera_id, state='skipped', reason='Live stream alerts require ONNX AI mode.', detections=[])
        return None
    if enforce_interval:
        now = time.time()
        with _state.live_detection_worker_lock:
            if now - _state.live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
                return None
            _state.live_detection_last_checked[camera_id] = now
    ai_state = ai_status_payload()
    if not ai_state['detector_loaded']:
        update_live_detection_status(camera_id, state='skipped', reason=ai_state['last_detector_error'] or 'ONNX detector is not loaded.', ai=ai_state, detections=[])
        return None
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
    _frame_w = max(40, int(live_settings.get('motion_frame_width', _state._MOTION_FRAME_W)))
    _frame_h = max(30, int(live_settings.get('motion_frame_height', _state._MOTION_FRAME_H)))
    if _frame_w != _state._MOTION_FRAME_W or _frame_h != _state._MOTION_FRAME_H:
        with _state._frame_motion_lock:
            _state._MOTION_FRAME_W = _frame_w
            _state._MOTION_FRAME_H = _frame_h
            _state._frame_motion_prev.clear()
    _cam_motion_nest = settings.get('motion') if isinstance(settings.get('motion'), dict) else {}
    _cam_pt = settings.get('motion_pixel_threshold') if settings.get('motion_pixel_threshold') is not None else _cam_motion_nest.get('pixel_threshold')
    if _cam_pt is not None:
        try:
            _pixel_threshold = float(_cam_pt)
        except (TypeError, ValueError):
            pass
    _cam_gf = settings.get('motion_gate_fraction') if settings.get('motion_gate_fraction') is not None else _cam_motion_nest.get('gate_fraction')
    if _cam_gf is not None:
        try:
            _gate_fraction = float(_cam_gf)
        except (TypeError, ValueError):
            pass
    _cam_sf = settings.get('motion_scale_fraction') if settings.get('motion_scale_fraction') is not None else _cam_motion_nest.get('scale_fraction')
    if _cam_sf is not None:
        try:
            _scale_fraction = float(_cam_sf)
        except (TypeError, ValueError):
            pass
    _cam_ba = settings.get('motion_background_alpha') if settings.get('motion_background_alpha') is not None else _cam_motion_nest.get('background_alpha')
    if _cam_ba is not None:
        try:
            _background_alpha = float(_cam_ba)
        except (TypeError, ValueError):
            pass
    periodic_scan_interval = float(live_settings.get('periodic_scan_interval_seconds', 0))
    force_scan = False
    if periodic_scan_interval > 0 and now - _state._periodic_scan_last_ts.get(camera_id, 0) >= periodic_scan_interval:
        force_scan = True
        _state._periodic_scan_last_ts[camera_id] = now
    frame_has_motion, frame_motion_confidence, diff_mask = detect_frame_motion(camera_id, image, pixel_threshold=_pixel_threshold, gate_fraction=_gate_fraction, scale_fraction=_scale_fraction, background_alpha=_background_alpha)
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
    motion_detections = zone_motion_detections(settings, frame_motion_confidence, diff_mask=diff_mask, gate_fraction=_gate_fraction, scale_fraction=_scale_fraction)
    if not frame_has_motion and (not force_scan) and (not motion_detections):
        update_live_detection_status(camera_id, state='checked', reason='No motion detected; ONNX inference skipped.', detected_labels=[], matched_labels=[], detections=[])
        return None
    min_conf = compute_minimum_rule_confidence(camera_settings=settings)
    try:
        if frame_is_numpy and hasattr(_state.detector, 'detect_frame'):
            detections = _state.detector.detect_frame(image, confidence=min_conf)
        else:
            detections = _state.detector.detect_image(image, confidence=min_conf)
    except (DetectorUnavailableError, ValueError) as exc:
        logger.warning('Live detection skipped for camera %s: %s', camera_id, exc)
        update_live_detection_status(camera_id, state='error', reason=str(exc), ai=ai_state, detections=[])
        return None
    detections = normalize_detection_boxes_for_frame(detections, frame)
    raw_labels = [str(detection.get('label')) for detection in detections if detection.get('label')]
    object_detections = filter_detections_for_camera(detections, settings)
    # Temporal confirmation gate: require an object label to persist across
    # several detection cycles before it can raise an alert or a recording.
    # Default required=1 is a pass-through no-op, so cameras that don't opt in
    # behave exactly as before. Applied to the zone/label-filtered detections so
    # the window only counts objects this camera actually cares about, and only
    # to the object axis -- motion is already gated separately.
    _confirm_frames = live_settings.get('detection_confirm_frames', 1)
    _confirm_window = live_settings.get('detection_confirm_window', _confirm_frames)
    object_detections = confirm_object_detections(
        camera_id, object_detections,
        required_frames=_confirm_frames, window_frames=_confirm_window,
    )
    zone_rules = zone_object_alert_rules(settings)
    has_object_zone_rules = any((zone.get('enabled', True) and zone.get('monitor_objects', True) and any((rule.get('enabled', True) and str(rule.get('label') or '').strip() for rule in zone.get('object_rules') or [])) for zone in (settings.get('detection') or {}).get('zones', [])))
    object_alert_detections = zone_alert_detections(settings, object_detections) if has_object_zone_rules else list(object_detections)
    record_only_detections = [d for d in object_detections if zone_record_on_detect(d, settings) and (not zone_object_rule_matches(settings, d, action='alert'))] if has_object_zone_rules else []
    strongest_motion = max(motion_detections, key=lambda d: float(d.get('confidence', 0))) if motion_detections else None
    record_live_detection_history(camera_id, list(object_alert_detections) + record_only_detections + ([{**strongest_motion, 'label': 'motion', 'motion_event': True}] if strongest_motion is not None else []), sample_ts=frame_capture_ts, live_config=live_settings)
    alert_detections = list(object_alert_detections) + record_only_detections
    for _mot in motion_detections:
        alert_detections.append({**_mot, 'label': 'motion', 'motion_event': True})
    if not alert_detections:
        _monitored_zones = [
            z for z in (settings.get('detection') or {}).get('zones', [])
            if z.get('enabled', True) and z.get('monitor_objects', True)
        ]
        reason = _no_object_match_reason(detections, raw_labels, _monitored_zones)
        update_live_detection_status(camera_id, state='checked', reason=reason, detected_labels=raw_labels, matched_labels=[], detections=list(detections))
        return None
    triggered = _state.alerts.process(alert_detections, rules=zone_rules)
    triggered_rule_names = {str(alert.get('rule_name') or '') for alert in triggered}
    triggered_labels = {str(alert.get('label') or '').lower() for alert in triggered}
    _confident_object_detections: list[dict[str, Any]] = []
    if has_object_zone_rules:
        for _det in object_detections:
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
            for _det in object_detections
        ]
    recording_detections = [{**detection, 'alert_matched': bool(zone_detection_alert_rule_names(settings, detection) & triggered_rule_names) if has_object_zone_rules else str(detection.get('label') or '').lower() in triggered_labels, 'alert_triggered': zone_record_on_detect(detection, settings)} for detection in _confident_object_detections]
    # Each motion detection is stamped with the record decision for ITS OWN
    # zone, so motion in a record-off zone cannot piggyback on a record-on
    # rule in a different zone. Appending every firing zone (not just the
    # strongest) keeps the event's detection list faithful when multiple
    # zones move at once.
    for _mot in motion_detections:
        _motion_zone_key = str(_mot.get('zone_id') or _mot.get('zone_name') or '')
        _motion_record = zone_motion_record_on_detect(settings, _motion_zone_key) if _motion_zone_key else zone_motion_record_on_detect(settings)
        # alert_triggered tracks ONLY the motion rule's own Record flag: an
        # enabled Email/Push alert on the motion rule must not silently force a
        # recording when Record is off. The alert itself still fires via
        # ``triggered_labels`` (visible as ``alert_matched``) and delivery.
        recording_detections.append({**_mot, 'label': 'motion', 'motion_event': True, 'alert_matched': 'motion' in triggered_labels, 'alert_triggered': _motion_record})
    matched_labels = [str(detection.get('label')) for detection in alert_detections if detection.get('label')]
    camera_recording_config = _state.camera_event_recording_config(settings)
    debounced_labels = detection_label_set([detection for detection in recording_detections if detection.get('alert_triggered')])
    if not debounced_labels:
        debounced_labels = detection_label_set(recording_detections)
    global_debounce = max(0.0, float(live_settings.get('event_debounce_seconds', 10.0)))
    label_cooldowns: dict[str, float] = {}
    for _zone in (settings.get('detection') or {}).get('zones', []):
        for _rule in _zone.get('object_rules') or []:
            if not _rule.get('enabled', True):
                continue
            _lbl = str(_rule.get('label') or '').strip().lower()
            if not _lbl:
                continue
            try:
                _cd = max(0.0, float(_rule.get('cooldown_seconds', 60)))
            except (TypeError, ValueError):
                _cd = 60.0
            if _lbl not in label_cooldowns or _cd > label_cooldowns[_lbl]:
                label_cooldowns[_lbl] = _cd
    # Each label is debounced against its OWN cooldown window (per-label
    # debounce), so a label whose window has elapsed fires a new event even
    # while a slower label on the same camera is still cooling. Labels without
    # a rule cooldown use the global event_debounce_seconds.
    resolved_cooldowns = {_lbl: label_cooldowns.get(_lbl, global_debounce) for _lbl in debounced_labels}
    frame_capture_time = datetime.fromtimestamp(frame_capture_ts, tz=timezone.utc).isoformat()
    # Debounce gates EVENT creation, not just recording: a camera whose alert
    # rules match but whose record rules don't (or that has recording off)
    # would otherwise create a fresh event + snapshot on every detection cycle
    # (~4 Hz), flooding the timeline with duplicates of the same activity. The
    # debounce window is derived from the same label cooldowns regardless of
    # whether a recording attaches, so an alert-only camera is throttled to one
    # event per window like a recording camera is.
    if resolved_cooldowns and not live_event_fresh_labels(camera_id, resolved_cooldowns):
        debounce_seconds = max(resolved_cooldowns.values())
        extended_recording_id = extend_active_rtsp_recording(camera_id=camera_id, event_time=frame_capture_time, recording_config=camera_recording_config, detections=recording_detections)
        remember_live_event(camera_id, debounced_labels, merge=True)
        update_live_detection_status(camera_id, state='checked', reason=f'Ongoing detection extended active recording and suppressed duplicate event for {debounce_seconds:.1f}s debounce window.' if extended_recording_id is not None else f'Ongoing detection suppressed for {debounce_seconds:.1f}s debounce window.', detected_labels=raw_labels, matched_labels=matched_labels, detections=recording_detections, recording_id=extended_recording_id)
        return None
    event_time = frame_capture_time
    if frame_is_numpy:
        image_bytes = _encode_frame_jpeg(image)
    else:
        image_bytes = image
    snapshot_path = _state.storage.save_image_snapshot(image_bytes, f'{camera_id}.jpg')
    event_id = _state.database.add_event(created_at=event_time, source='rtsp', snapshot_path=snapshot_path, detections=recording_detections, alert_triggered=bool(triggered), metadata={'camera_id': settings.get('id'), 'camera_name': settings.get('name'), 'ai_backend': ai_state['configured_backend'], 'detector_backend': ai_state['active_backend'], 'source': 'live-stream'})
    recording_id = attach_event_recording(event_id, event_time, 'rtsp', recording_detections, camera_id=camera_id, recording_config=camera_recording_config)
    # Remember the event even when no recording attached: the debounce state
    # must advance for alert-only events too, otherwise the next cycle (which
    # sees the same labels) is not suppressed and the timeline floods with
    # duplicates. ``remember_live_event`` no-ops on an empty label set.
    remember_live_event(camera_id, debounced_labels)
    _rule_by_name = {str(r.get('name') or ''): r for r in zone_rules or []}
    for alert in triggered:
        _rule = _rule_by_name.get(str(alert.get('rule_name') or ''), {})
        if not _rule_notify_active_now(_rule):
            continue
        _state.database.add_alert(created_at=datetime.now(timezone.utc).isoformat(), rule_name=alert['rule_name'], event_id=event_id, label=alert['label'], confidence=alert['confidence'], message=alert['message'], recording_id=recording_id)
    if triggered:
        notify_thread = threading.Thread(target=_deliver_alert_notifications, args=(triggered, event_id, zone_rules), name=f'alert-notify-{event_id}', daemon=True)
        notify_thread.start()
        with _state._notification_threads_lock:
            _state._notification_threads[:] = [thread for thread in _state._notification_threads if thread.is_alive()]
            _state._notification_threads.append(notify_thread)
    email_rules = [rule for rule in zone_rules if rule.get('enabled', True) and rule.get('email_enabled') and _rule_notify_active_now(rule) and (str(rule.get('name') or '') in {str(alert.get('rule_name') or '') for alert in triggered})]
    email_recipients = sorted({recipient for rule in email_rules for recipient in rule.get('email_recipients', [])})
    update_live_detection_status(camera_id, state='alerted' if triggered else 'checked', reason='Alert matched.' if triggered else 'Detections found. No new alert event was created because no alert rule matched, or a matching rule is still in cooldown.', detected_labels=raw_labels, matched_labels=matched_labels, detections=recording_detections, triggered_alerts=triggered, event_id=event_id, recording_id=recording_id, recording_state='linked' if recording_id is not None else 'skipped', recording_reason='Recording linked.' if recording_id is not None else recording_skip_reason(recording_detections, _state.camera_event_recording_config(settings)), email_enabled_rules=len(email_rules), email_recipients=email_recipients, email_attempted=bool(triggered and email_recipients and effective_email_alert_settings().get('enabled')))
    return event_id
