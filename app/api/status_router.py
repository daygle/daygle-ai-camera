"""System Status APIRouter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.ai_settings import ai_status_payload
from app.deps import get_cameras_config
from app.detection_status import live_detection_status_payload
from app.zone_detection import get_camera_instance
from app.config_facades import get_camera_config

router = APIRouter()


@router.get('/healthz')
def healthz():
    """Unauthenticated liveness probe for process monitors.

    Serves the same contract as the authenticated ``/api/status`` response
    (the fields the dashboard consumes) so monitors get extra diagnostics for
    free, and deliberately reports no camera or AI state: it answers the only
    question a liveness probe asks -- is the HTTP server up? -- without
    exposing camera names, stream configuration, or detector errors to an
    unauthenticated caller. Camera/AI health belongs to the authenticated
    ``/api/status`` and the Cameras page.

    PUBLIC_PATHS in ``app/state.py`` bypasses authentication for this path so
    systemd timers, uptime monitors, and the Docker HEALTHCHECK can call it
    without a session. Do not add per-camera or model state here.
    """
    return {
        'status': 'ok',
        'service': 'daygle-ai-camera',
        'ai_backend': None,
        'ai_available': None,
        'ai_error': None,
        'ai_mode': None,
        'live_detection': {},
    }


@router.get('/api/status')
def status(camera_id: str | None = None, cameras_config=Depends(get_cameras_config)):
    if not cameras_config:
        ai_state = ai_status_payload()
        return {'status': 'online', 'mode': None, 'camera_id': None, 'camera_name': None, 'camera_detection': {}, 'ai_backend': ai_state['active_backend'], 'ai_available': ai_state['inference_available'], 'ai_error': ai_state['error'], 'ai_mode': ai_state['mode'], 'live_detection': live_detection_status_payload(camera_id), 'frame_number': 0, 'uptime_seconds': 0, 'resolution': {'width': 0, 'height': 0}, 'fps': {'configured': None, 'detected': None, 'effective': 15, 'source': 'fallback'}}
    selected_camera = get_camera_instance(camera_id)
    selected_config = get_camera_config(camera_id)
    frame = selected_camera.get_frame()
    ai_state = ai_status_payload()
    return {'status': 'online', 'mode': selected_config.get('backend', 'onvif'), 'camera_id': selected_config.get('id'), 'camera_name': selected_config.get('name'), 'camera_detection': selected_config.get('detection', {}), 'ai_backend': ai_state['active_backend'], 'ai_available': ai_state['inference_available'], 'ai_error': ai_state['error'], 'ai_mode': ai_state['mode'], 'live_detection': live_detection_status_payload(camera_id), 'frame_number': frame['frame_number'], 'uptime_seconds': frame['uptime_seconds'], 'resolution': {'width': frame['width'], 'height': frame['height']}, 'fps': {'configured': frame.get('configured_fps'), 'detected': frame.get('detected_fps'), 'effective': frame.get('effective_fps', frame.get('fps')), 'source': frame.get('fps_source', 'fallback')}}


@router.get('/api/status/ai')
def ai_status():
    return ai_status_payload()
