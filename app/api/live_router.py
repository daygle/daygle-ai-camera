"""Live APIRouter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

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


@router.get('/api/live/snapshot')
def live_snapshot(request: Request, camera_id: str | None = None, stream: str = 'detection', recording_service=Depends(get_recording_service)):
    # M1 fix: defence-in-depth handler gate (mirror
    # ``live_detection_status_api``).
    require_user(request)
    selected_config = get_camera_config(camera_id)
    resolved_id = str(selected_config.get('id') or camera_id or '')

    # When stream=recording and a recording_stream_path is configured, grab a
    # single frame directly from the recording stream so operators can verify
    # the high-res stream is working from the Live page.
    if stream == 'recording':
        from app.utils import build_recording_stream_url
        rec_url = build_recording_stream_url(selected_config)
        if rec_url:
            frame_bytes = recording_service.grab_frame_from_url(rec_url)
            if frame_bytes is not None:
                return Response(content=frame_bytes, media_type='image/jpeg')
            raise HTTPException(status_code=503, detail='Could not grab a frame from the recording stream. Verify the Recording Stream Path in Camera Settings.')
        # Fall through to detection stream when no recording stream is configured

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
