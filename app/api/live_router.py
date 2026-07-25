"""Live APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

import time

import app.state as _state
from app.auth_gates import require_user
from app.config_facades import get_camera_config
from app.deps import get_recording_service
from app.detection_status import live_detection_status_payload
from app.utils import build_stream_url
from app.zone_detection import get_camera_instance

router = APIRouter()


@router.get('/api/live/detection-status')
def live_detection_status_api(request: Request, camera_id: str | None = None):
    # M1 fix: defence-in-depth. The auth middleware already enforces a
    # session for any non-public /api/* path; this handler-level gate
    # is the second line if a future refactor reorders middleware or
    # accidentally moves this path into PUBLIC_PATHS.
    require_user(request)
    return live_detection_status_payload(camera_id)


@router.get('/api/live/motion-history')
def live_motion_history(request: Request, camera_id: str | None = None, window_seconds: int = 60) -> dict[str, object]:
    """Return the per-camera motion-intensity history for the last N seconds.

    Powers the /live page's "Live motion" chart strip. The underlying ring
    buffer is fed by ``app.live_monitor.process_live_stream_alerts`` at the
    monitor's native cadence (~4 Hz) so clients refreshing at 0.5 Hz still
    see a smooth curve. ``window_seconds`` is clamped to [5, 300] to avoid
    unbounded snapshot work on slow clients.
    """
    selected_config = get_camera_config(camera_id)
    resolved_id = str(selected_config.get('id') or camera_id or 'camera')
    # Clamp the requested window to a sane range; a worst-case monitor runs
    # at ~4Hz, so MOTION_HISTORY_CAP // 4 seconds is the buffer's actual reach.
    # Anything beyond that returns the same answer, so we trim it here and
    # report the effective window back to the client.
    require_user(request)
    window = min(max(5, int(window_seconds or 60)), 300)
    cap_seconds = max(5, _state.MOTION_HISTORY_CAP // 4)
    effective_window = min(window, cap_seconds)
    cutoff = time.time() - float(effective_window)
    with _state._motion_history_lock:
        buffer = _state._motion_history.get(resolved_id) or []
        samples = [
            {'ts': float(ts), 'confidence': float(conf)}
            for (ts, conf) in list(buffer)
            if ts >= cutoff
        ]
    return {
        'camera_id': resolved_id,
        'camera_name': selected_config.get('name'),
        'window_seconds': effective_window,
        'sample_count': len(samples),
        'samples': samples,
    }


@router.get('/api/live/snapshot')
def live_snapshot(request: Request, camera_id: str | None = None, recording_service=Depends(get_recording_service)):
    # M1 fix: defence-in-depth handler gate (mirror
    # ``live_detection_status_api`` / ``live_motion_history``).
    require_user(request)
    selected_config = get_camera_config(camera_id)
    resolved_id = str(selected_config.get('id') or camera_id or '')
    has_stream = bool(resolved_id and build_stream_url(selected_config))
    if has_stream:
        sample = recording_service.latest_frame_jpeg(resolved_id)
        if sample is not None:
            return Response(content=sample[0], media_type='image/jpeg')
    try:
        selected_camera = get_camera_instance(camera_id)
    except HTTPException:
        if has_stream:
            raise HTTPException(status_code=503, detail='Camera ingest is warming up; no frame available yet.') from None
        raise
    if hasattr(selected_camera, 'read_jpeg'):
        try:
            image_bytes, frame = selected_camera.read_jpeg()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(content=image_bytes, media_type='image/jpeg')
    raise HTTPException(status_code=503, detail='Live snapshots require an ONVIF/RTSP camera backend.')
