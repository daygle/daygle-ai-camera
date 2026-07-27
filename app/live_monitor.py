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
from app.config_facades import effective_email_alert_settings, effective_live_config
from app.detection_state import (
    detect_frame_motion,
    detection_label_set,
    record_live_detection_history,
)
from app.detection_status import _camera_has_live_alert_stream, update_live_detection_status
from app.detector import DetectorUnavailableError
from app.event_debounce import (
    clear_live_camera_backoff,
    live_event_is_debounced,
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
from app.utils import build_stream_url, normalize_bool_setting
from app.zone_detection import (
    detection_has_matching_record_rule,
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

def run_live_alert_monitor_once(live_settings: dict[str, Any] | None=None) -> int:
    if live_settings is None:
        live_settings = effective_live_config()
    background_detection_enabled = normalize_bool_setting(live_settings.get('background_detection_enabled'), True)
    processed = 0
    for selected_config in list(_state.cameras_config):
        camera_id = str(selected_config.get('id') or 'camera')
        if not _camera_has_live_alert_stream(selected_config):
            continue
        now = time.time()
        stream_url = build_stream_url(selected_config)
        cam_rec_config = _state.camera_event_recording_config(selected_config)
        if stream_url:
            _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config)
            if cam_rec_config.get('continuous'):
                _state.recording_service.start_continuous_chunk_recording(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config, on_chunk_complete=_make_continuous_chunk_callback(camera_id))
        if not background_detection_enabled:
            continue
        with _state._live_backoff_lock:
            retry_after = _state.live_detection_retry_after.get(camera_id, 0)
        if retry_after and now < retry_after:
            continue
        detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.25))
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
        interval = max(0.1, float(live_settings.get('detection_interval_seconds', 0.25)))
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
        _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=_state.camera_event_recording_config(settings))
    live_cfg = effective_live_config()
    if normalize_bool_setting(live_cfg.get('background_detection_enabled'), True):
        return
    detection_interval_seconds = float(live_cfg.get('detection_interval_seconds', 0.25))
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
    detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.25))
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
    frame_has_motion, frame_motion_confidence, diff_mask, frame_motion_intensity = detect_frame_motion(camera_id, image, pixel_threshold=_pixel_threshold, gate_fraction=_gate_fraction, scale_fraction=_scale_fraction, background_alpha=_background_alpha)
    if not frame_has_motion and (not force_scan):
        # Append the raw (ungated) intensity BEFORE returning so the /live
        # motion sparkline reflects sub-gate ambient activity instead of a
        # flat line of zeros - the alert path still skipped inference because
        # frame_has_motion is False.
        with _state._motion_history_lock:
            _state._motion_history[camera_id].append((now, float(frame_motion_intensity)))
        update_live_detection_status(camera_id, state='checked', reason='No motion detected; ONNX inference skipped.', detected_labels=[], matched_labels=[], detections=[])
        return None
    if not frame_has_motion:
        frame_motion_confidence = 0.0
        diff_mask = None
    # Mirror the raw frame-motion intensity into the per-camera ring buffer so
    # /api/live/motion-history can return a smooth 4Hz sparkline that reflects
    # actual frame motion (not just above-alert-gate motion) without polluting
    # /api/live/detection-status's polling cadence.
    with _state._motion_history_lock:
        _state._motion_history[camera_id].append((now, float(frame_motion_intensity)))
    min_conf = compute_minimum_rule_confidence()
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
    motion_detections = zone_motion_detections(settings, frame_motion_confidence, diff_mask=diff_mask, gate_fraction=_gate_fraction, scale_fraction=_scale_fraction)
    object_detections = filter_detections_for_camera(detections, settings)
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
        update_live_detection_status(camera_id, state='checked', reason='No detections matched this camera and its zone areas.', detected_labels=raw_labels, matched_labels=[], detections=list(detections))
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
        _confident_object_detections = list(object_detections)
    recording_detections = [{**detection, 'alert_matched': bool(zone_detection_alert_rule_names(settings, detection) & triggered_rule_names) if has_object_zone_rules else str(detection.get('label') or '').lower() in triggered_labels, 'alert_triggered': zone_record_on_detect(detection, settings)} for detection in _confident_object_detections]
    if motion_detections:
        _motion_record = zone_motion_record_on_detect(settings)
        recording_detections.append({**strongest_motion, 'label': 'motion', 'motion_event': True, 'alert_matched': 'motion' in triggered_labels, 'alert_triggered': 'motion' in triggered_labels or _motion_record or detection_has_matching_record_rule({**strongest_motion, 'label': 'motion'}, zone_rules)})
    matched_labels = [str(detection.get('label')) for detection in alert_detections if detection.get('label')]
    camera_recording_config = _state.camera_event_recording_config(settings)
    should_record_event, _trigger_type, _trigger_label = _state.recording_service.should_record(recording_detections, camera_recording_config)
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
    _matching = [label_cooldowns[_lbl] for _lbl in debounced_labels if _lbl in label_cooldowns]
    debounce_seconds = max(_matching) if _matching else global_debounce
    frame_capture_time = datetime.fromtimestamp(frame_capture_ts, tz=timezone.utc).isoformat()
    if should_record_event and live_event_is_debounced(camera_id, debounced_labels, debounce_seconds):
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
    if recording_id is not None:
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
