"""Live APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from app.config_facades import get_camera_config
from app.deps import get_recording_service
from app.detection_status import live_detection_status_payload
from app.main import build_stream_url
from app.zone_detection import get_camera_instance

router = APIRouter()


@router.get('/api/live/detection-status')
def live_detection_status_api(camera_id: str | None = None):
    return live_detection_status_payload(camera_id)


@router.get('/api/live/snapshot')
def live_snapshot(camera_id: str | None = None, recording_service=Depends(get_recording_service)):
    selected_config = get_camera_config(camera_id)
    resolved_id = str(selected_config.get('id') or camera_id or '')
    if resolved_id and build_stream_url(selected_config):
        sample = recording_service.latest_frame_jpeg(resolved_id)
        if sample is not None:
            return Response(content=sample[0], media_type='image/jpeg')
    selected_camera = get_camera_instance(camera_id)
    if hasattr(selected_camera, 'read_jpeg'):
        try:
            image_bytes, frame = selected_camera.read_jpeg()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(content=image_bytes, media_type='image/jpeg')
    raise HTTPException(status_code=503, detail='Live snapshots require an ONVIF/RTSP camera backend.')
