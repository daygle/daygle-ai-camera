"""Live-alert-monitor lifecycle cluster extracted from ``app/main.py`` (Phase-32)."""

from __future__ import annotations
import logging
import threading
import time
from typing import Any


logger = logging.getLogger('daygle.ai')

def run_live_alert_monitor_once(live_settings: dict[str, Any] | None=None) -> int:
    import app.main as main
    if live_settings is None:
        live_settings = main.effective_live_config()
    background_detection_enabled = main.normalize_bool_setting(live_settings.get('background_detection_enabled'), True)
    processed = 0
    for selected_config in list(main.cameras_config):
        camera_id = str(selected_config.get('id') or 'camera')
        if not main._camera_has_live_alert_stream(selected_config):
            continue
        now = time.time()
        stream_url = main.build_stream_url(selected_config)
        cam_rec_config = main.camera_event_recording_config(selected_config)
        if stream_url:
            main.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config)
            if cam_rec_config.get('continuous'):
                main.recording_service.start_continuous_chunk_recording(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config, on_chunk_complete=main._make_continuous_chunk_callback(camera_id))
        if not background_detection_enabled:
            continue
        with main._live_backoff_lock:
            retry_after = main.live_detection_retry_after.get(camera_id, 0)
        if retry_after and now < retry_after:
            continue
        detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.25))
        with main.live_detection_worker_lock:
            if camera_id in main.active_live_detection_cameras:
                continue
            if now - main.live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
                continue
            main.live_detection_last_checked[camera_id] = now
            main.active_live_detection_cameras.add(camera_id)

        def _detect_bg(cid: str=camera_id, cfg: dict[str, Any]=dict(selected_config)) -> None:
            from app.main import (
                read_ingest_frame, schedule_live_camera_backoff,
                clear_live_camera_backoff, process_live_stream_alerts,
            )
            try:
                sample = main.read_ingest_frame(cid)
                if sample is None:
                    if not main.recording_service.ingest_has_produced_frame(cid):
                        return
                    cam_instance = main.camera_instances.get(cid)
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
                        main.schedule_live_camera_backoff(cid, 'No fresh frame available from the camera ingest.')
                        return
                image, frame = sample
                main.clear_live_camera_backoff(cid)
                main.process_live_stream_alerts(image, frame, cfg, enforce_interval=False)
            except Exception as exc:
                logger.warning('Background live alert check failed for camera %s: %s', cid, exc)
                main.schedule_live_camera_backoff(cid, str(exc))
            finally:
                with main.live_detection_worker_lock:
                    main.active_live_detection_cameras.discard(cid)
        threading.Thread(target=_detect_bg, name=f'live-detection-{camera_id}', daemon=True).start()
        processed += 1
    return processed

def _prune_frame_motion_state() -> None:
    """Remove background model and scan timestamp entries for cameras no longer in the active config."""
    import app.main as main
    active_ids = {str(cfg.get('id') or '') for cfg in main.cameras_config if cfg.get('id')}
    with main._frame_motion_lock:
        stale = [cid for cid in main._frame_motion_prev if cid not in active_ids]
        for cid in stale:
            del main._frame_motion_prev[cid]
    for cid in stale:
        main._periodic_scan_last_ts.pop(cid, None)
        main._frame_motion_error_cameras.discard(cid)
    if stale:
        logger.debug('Pruned stale motion state for cameras: %s', stale)

def live_alert_monitor_loop() -> None:
    import app.main as main
    _last_prune = 0.0
    while not main.live_alert_monitor_stop.is_set():
        live_settings = main.effective_live_config()
        run_live_alert_monitor_once(live_settings)
        main._check_cameras_health()
        now = time.time()
        if now - _last_prune > 300:
            _prune_frame_motion_state()
            main.purge_camera_diagnostics_by_policy()
            _last_prune = now
        interval = max(0.1, float(live_settings.get('detection_interval_seconds', 0.25)))
        main.live_alert_monitor_stop.wait(interval)

def start_live_alert_monitor() -> None:
    import app.main as main
    if main.live_alert_monitor_thread and main.live_alert_monitor_thread.is_alive():
        return
    main.live_alert_monitor_stop.clear()
    main.live_alert_monitor_thread = threading.Thread(target=live_alert_monitor_loop, name='live-alert-monitor', daemon=True)
    main.live_alert_monitor_thread.start()

def stop_live_alert_monitor() -> None:
    import app.main as main
    main.live_alert_monitor_stop.set()
    if main.live_alert_monitor_thread and main.live_alert_monitor_thread.is_alive():
        main.live_alert_monitor_thread.join(timeout=5)
    main.live_alert_monitor_thread = None
