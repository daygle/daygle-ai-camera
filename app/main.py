from __future__ import annotations
import importlib.metadata
import importlib.util
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import subprocess
import threading
import app.state as _state
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from app.alerts import AlertEngine
from app.auth import SESSION_COOKIE, AuthService, utc_now
from app.database import EventDatabase
from app.detector import create_detector
from app.email_alerts import EmailAlertError, EmailAlertService
from app.push_notifications import PushNotificationError, PushNotificationService
from app.recordings import RecordingService
from app.settings import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, config_file_path, load_settings
from app.storage import Storage

from app.ai_settings import log_detector_initialization
from app.camera_instance import create_camera_instances
from app.config_facades import effective_auth_config, effective_ai_config, effective_cameras_config, effective_recording_config, effective_storage_config
from app.diagnostics import log_camera_diagnostic
from app.live_monitor import start_live_alert_monitor, stop_live_alert_monitor
from app.sound_monitor import apply_sound_settings, stop_sound_monitor
from app.utils import _parse_iso_datetime

_logger = logging.getLogger('daygle.ai')
# Pool A: tests monkeypatch main.logger; internal callers use _logger directly.
logger = logging.getLogger('daygle.ai')

def _configure_file_logging() -> None:
    log_dir = Path(__file__).resolve().parent.parent / 'data' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / 'app.log'
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, logging.handlers.RotatingFileHandler) and existing.baseFilename == str(log_path):
            return
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    if not root.handlers:
        logging.basicConfig(level=logging.INFO)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
_configure_file_logging()
ONE_PIXEL_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'
config = load_settings()
_state.config = config
auth_config = config.get('auth', {})
_state.auth_config = auth_config

@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    removed = database.cleanup_incomplete_recordings()
    if removed:
        _logger.info(f'Cleaned up {len(removed)} incomplete recording(s) from previous session')
    log_detector_initialization()
    start_live_alert_monitor()
    apply_sound_settings()
    try:
        yield
    finally:
        _state.recording_service.stop_prebuffer_workers()
        _state.recording_service.stop_all_continuous_recordings()
        stop_live_alert_monitor()
        stop_sound_monitor()
app = FastAPI(title='Daygle AI Camera', lifespan=app_lifespan)
BASE_DIR = Path(__file__).resolve().parent.parent
static_dir = BASE_DIR / 'web'
if static_dir.exists():
    app.mount('/static', StaticFiles(directory=static_dir), name='static')

database = EventDatabase(config['storage']['database'])
_state.database = database
camera_config: dict[str, Any] = {}
_state.camera_config = camera_config
cameras_config: list[dict[str, Any]] = []
_state.cameras_config = cameras_config
camera_instances: dict[str, Any] = {}
_state.camera_instances = camera_instances
camera = None
_state.camera = camera
storage = Storage({**config, 'storage': effective_storage_config()})
_state.storage = storage
recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})
_state.recording_service = recording_service
auth = AuthService(config['storage']['database'], effective_auth_config())
_state.auth = auth
SESSION_COOKIE_NAME = str(effective_auth_config().get('cookie_name', SESSION_COOKIE))
_state.detector = create_detector(effective_ai_config())
last_detector_error: str | None = getattr(_state.detector, 'unavailable_reason', None)
_state.last_detector_error = last_detector_error
alerts = AlertEngine([])
_state.alerts = alerts




















































cameras_config = effective_cameras_config()
_state.cameras_config = cameras_config
camera_config = cameras_config[0] if cameras_config else {}
_state.camera_config = camera_config
camera_instances = create_camera_instances(cameras_config)
_state.camera_instances = camera_instances
camera = camera_instances[camera_config['id']] if camera_config else None
_state.camera = camera

_state.recording_service.diagnostic_callback = log_camera_diagnostic

GITHUB_REPO = 'daygle/daygle-ai-camera'
_update_in_progress = False
_update_lock = threading.Lock()

def _parse_header_value(header: str, key: str) -> str | None:
    for part in header.split(';'):
        part = part.strip()
        if part.startswith(f'{key}='):
            return part.split('=', 1)[1].strip('"')
    return None

async def _read_uploaded_image(request: Request) -> tuple[bytes, str | None, str | None]:
    content_type = request.headers.get('content-type', '')
    body = await request.body()
    if content_type.startswith('image/'):
        return (body, None, content_type)
    boundary = _parse_header_value(content_type, 'boundary')
    if not boundary:
        raise HTTPException(status_code=400, detail='Expected multipart image upload')
    delimiter = ('--' + boundary).encode('utf-8')
    for part in body.split(delimiter):
        if b'Content-Disposition' not in part or b'name="file"' not in part:
            continue
        header_blob, separator, payload = part.partition(b'\r\n\r\n')
        if not separator:
            continue
        headers = header_blob.decode('utf-8', errors='replace')
        filename = _parse_header_value(headers, 'filename')
        uploaded_type = None
        for line in headers.splitlines():
            if line.lower().startswith('content-type:'):
                uploaded_type = line.split(':', 1)[1].strip()
                break
        if payload.endswith(b'\r\n'):
            payload = payload[:-2]
        return (payload, filename, uploaded_type)
    raise HTTPException(status_code=400, detail='Multipart upload must include a file field named file')

def _recording_timeline_segment(recording: dict[str, Any], day_start: datetime, day_end: datetime) -> dict[str, Any] | None:
    started_at = _parse_iso_datetime(recording.get('started_at'))
    ended_at = _parse_iso_datetime(recording.get('ended_at'))
    duration_seconds = max(0.0, float(recording.get('duration_seconds') or 0.0))
    if started_at is None:
        return None
    if ended_at is None or ended_at <= started_at:
        ended_at = started_at + timedelta(seconds=max(duration_seconds, 1.0))
    visible_start = max(started_at, day_start)
    visible_end = min(ended_at, day_end)
    if visible_end <= visible_start:
        return None
    trigger_type = str(recording.get('trigger_type') or 'motion').lower()
    trigger_label = str(recording.get('trigger_label') or '').strip().lower()
    color_key = trigger_label if trigger_type in {'human', 'object', 'alert'} and trigger_label else trigger_type
    return {**recording, 'timeline_start_seconds': max(0.0, (visible_start - day_start).total_seconds()), 'timeline_end_seconds': min(86400.0, (visible_end - day_start).total_seconds()), 'timeline_duration_seconds': max(1.0, (visible_end - visible_start).total_seconds()), 'color_key': color_key, 'color_label': color_key}













def _current_version() -> str:
    version_file = BASE_DIR / 'VERSION'
    return version_file.read_text(encoding='utf-8').strip() if version_file.exists() else 'unknown'
from app.api.sound_router import router as sound_router
app.include_router(sound_router)
from app.api.settings_ai_router import router as settings_ai_router
app.include_router(settings_ai_router)
from app.api.recordings_router import router as recordings_router
app.include_router(recordings_router)
if __name__ == '__main__':
    import uvicorn
    server_config = config.get('server', {})
    uvicorn.run('app.main:app', host=server_config.get('host', '0.0.0.0'), port=int(server_config.get('port', 8080)), reload=False)
from app.api.cameras_router import router as cameras_router
app.include_router(cameras_router)
from app.api.events_router import router as events_router
app.include_router(events_router)
from app.api.alerts_router import router as alerts_router
app.include_router(alerts_router)
from app.api.users_router import router as users_router
app.include_router(users_router)
from app.api.alert_email_router import router as alert_email_router
app.include_router(alert_email_router)
from app.api.alert_push_router import router as alert_push_router
app.include_router(alert_push_router)
from app.api.camera_offline_router import router as camera_offline_router
app.include_router(camera_offline_router)
from app.api.status_router import router as status_router
app.include_router(status_router)
from app.api.settings_system_router import router as settings_system_router
app.include_router(settings_system_router)
from app.api.live_router import router as live_router
app.include_router(live_router)
from app.api.admin_router import router as admin_router
app.include_router(admin_router)
from app.api.camera_log_router import router as camera_log_router
app.include_router(camera_log_router)
from app.api.update_router import router as update_router
app.include_router(update_router)
from app.api.utility_router import router as utility_router
app.include_router(utility_router)
from app.api.web_router import router as web_router
app.include_router(web_router)
from app.api.auth_router import router as auth_router
app.include_router(auth_router)
from app.api.web_router import login_page as login_page
from app.api.web_router import setup_page as setup_page
_WEB_ROUTER_PAGE_ALIASES = (login_page, setup_page)
from app.middleware import authentication_middleware, app_navigation_middleware
app.middleware('http')(authentication_middleware)
app.middleware('http')(app_navigation_middleware)
from app.auth_gates import _request_ip as _request_ip, require_admin as require_admin, require_session as require_session, require_user as require_user
# Pool A back-compat rebinds for helpers moved to app/auth_helpers.py and
# app/request_helpers.py.  Sibling modules (middleware, tests) still reach
# them as ``main.<name>``; these rebinds keep that working without requiring
# every caller to be updated simultaneously.
from app.auth_helpers import auth_page as auth_page
from app.auth_helpers import csrf_token_response as csrf_token_response
from app.auth_helpers import set_csrf_cookie as set_csrf_cookie
from app.auth_helpers import set_session_cookie as set_session_cookie
from app.auth_helpers import clear_auth_cookies as clear_auth_cookies
from app.request_helpers import form_data as form_data
