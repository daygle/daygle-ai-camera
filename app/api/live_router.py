"""Live APIRouter.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth_gates import require_user
from app.config_facades import get_camera_config
from app.deps import get_recording_service
from app.detection_status import live_detection_status_payload
from app.utils import build_stream_url
from app.zone_detection import get_camera_instance

router = APIRouter()


def _queue_detection_snapshot(
    selected_config: dict,
    frame_bytes: bytes,
    *,
    captured_ts: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Feed live-page snapshots into the opt-in foreground detector path.

    When background detection is disabled, the Live page is the intended source
    of detection frames. Previously ``queue_live_stream_alerts`` had no
    production caller, leaving the status (including the motion bar) at
    ``Waiting`` even while snapshots visibly changed. The queue function keeps
    its own setting/interval/worker guards, so this is a cheap no-op when the
    background monitor is enabled and cannot duplicate active work.
    """
    from app.live_monitor import queue_live_stream_alerts

    frame = {
        'frame_number': 0,
        'timestamp': float(captured_ts or time.time()),
        'width': int(width or selected_config.get('width') or 1280),
        'height': int(height or selected_config.get('height') or 720),
    }
    queue_live_stream_alerts(
        frame_bytes,
        frame,
        selected_config,
        allow_when_background_enabled=True,
    )


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
            _queue_detection_snapshot(
                selected_config,
                sample[0],
                captured_ts=sample[1],
            )
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
        _queue_detection_snapshot(
            selected_config,
            image_bytes,
            captured_ts=frame.get('timestamp'),
            width=frame.get('width'),
            height=frame.get('height'),
        )
        return Response(content=image_bytes, media_type='image/jpeg')
    raise HTTPException(status_code=503, detail='Live snapshots require an ONVIF/RTSP camera backend.')
