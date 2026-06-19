"""Live-alert-monitor lifecycle cluster extracted from ``app/main.py`` (Phase-32)."""

from __future__ import annotations
import logging
import threading
import time
from typing import Any

import app.state as _state
from app.config_facades import effective_live_config

logger = logging.getLogger('daygle.ai')

def run_live_alert_monitor_once(live_settings: dict[str, Any] | None=None) -> int:
    from app.main import (
        normalize_bool_setting, _camera_has_live_alert_stream, build_stream_url,
        camera_event_recording_config, read_ingest_frame, schedule_live_camera_backoff,
        clear_live_camera_backoff, process_live_stream_alerts, _make_continuous_chunk_callback,
    )
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
        cam_rec_config = camera_event_recording_config(selected_config)
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

        def _detect_bg(cid: str=camera_id, cfg: dict[str, Any]=dict(selected_config)) -> None:
            from app.main import (
                read_ingest_frame, schedule_live_camera_backoff,
                clear_live_camera_backoff, process_live_stream_alerts,
            )
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
                process_live_stream_alerts(image, frame, cfg, enforce_interval=False)
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
    from app.main import _check_cameras_health, purge_camera_diagnostics_by_policy
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
