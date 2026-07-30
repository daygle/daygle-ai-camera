"""System Status APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.ai_settings import ai_status_payload
from app.deps import get_cameras_config
from app.detection_status import live_detection_status_payload
from app.zone_detection import get_camera_instance
from app.config_facades import get_camera_config

router = APIRouter()


@router.get('/api/status')
def status(camera_id: str | None = None, cameras_config=Depends(get_cameras_config)):
    if not cameras_config:
        ai_state = ai_status_payload()
        return {'status': 'online', 'mode': None, 'camera_id': None, 'camera_name': None, 'camera_detection': {}, 'ai_backend': ai_state['active_backend'], 'ai_available': ai_state['inference_available'], 'ai_error': ai_state['error'], 'ai_mode': ai_state['mode'], 'live_detection': live_detection_status_payload(camera_id), 'frame_number': 0, 'uptime_seconds': 0, 'resolution': {'width': 0, 'height': 0}, 'fps': {'configured': None, 'detected': None, 'effective': 15}}
    selected_camera = get_camera_instance(camera_id)
    selected_config = get_camera_config(camera_id)
    frame = selected_camera.get_frame()
    ai_state = ai_status_payload()
    return {'status': 'online', 'mode': selected_config.get('backend', 'onvif'), 'camera_id': selected_config.get('id'), 'camera_name': selected_config.get('name'), 'camera_detection': selected_config.get('detection', {}), 'ai_backend': ai_state['active_backend'], 'ai_available': ai_state['inference_available'], 'ai_error': ai_state['error'], 'ai_mode': ai_state['mode'], 'live_detection': live_detection_status_payload(camera_id), 'frame_number': frame['frame_number'], 'uptime_seconds': frame['uptime_seconds'], 'resolution': {'width': frame['width'], 'height': frame['height']}, 'fps': {'configured': frame.get('configured_fps'), 'detected': frame.get('detected_fps'), 'effective': frame.get('fps')}}


@router.get('/api/status/ai')
def ai_status():
    return ai_status_payload()
