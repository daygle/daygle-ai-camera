from __future__ import annotations
import logging
import logging.handlers
import subprocess  # noqa: F401 -- tests monkeypatch via main.subprocess
import threading
import app.state as _state
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.alerts import AlertEngine
from app.auth import AuthService, utc_now  # noqa: F401 -- utc_now used as main.utc_now() in tests
from app.database import EventDatabase
from app.detector import create_detector
from app.email_alerts import EmailAlertService  # noqa: F401 -- tests monkeypatch via main.EmailAlertService
from app.push_notifications import PushNotificationService  # noqa: F401 -- tests monkeypatch via main.PushNotificationService
from app.recordings import RecordingService
from app.settings import load_settings
from app.storage import Storage

from app.ai_settings import log_detector_initialization
from app.camera_instance import create_camera_instances
from app.config_facades import effective_auth_config, effective_ai_config, effective_cameras_config, effective_recording_config, effective_storage_config
from app.diagnostics import log_camera_diagnostic
from app.live_monitor import start_live_alert_monitor, stop_live_alert_monitor
from app.sound_monitor import apply_sound_settings, stop_sound_monitor

_logger = logging.getLogger('daygle.ai')
logger = logging.getLogger('daygle.ai')  # noqa: F811 -- tests monkeypatch main.logger


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
config = load_settings()
_state.config = config
auth_config = config.get('auth', {})
_state.auth_config = auth_config

# Singletons are None until _startup() runs (called from lifespan or test bootstrap).
database: Any = None
storage: Any = None
recording_service: Any = None
auth: Any = None
alerts: Any = None


def _startup() -> None:
    """Create and register all application singletons.

    Called from ``app_lifespan`` when the server starts, and explicitly from
    ``tests/test_api.py::_load_app`` for tests that exercise internal logic
    without starting an HTTP server. Idempotent: a second call is a no-op so
    test monkeypatches applied after the first call are preserved when the
    lifespan also calls this.
    """
    global database, storage, recording_service, auth, alerts
    if _state.database is not None:
        return
    database = EventDatabase(config['storage']['database'])
    _state.database = database
    _state.camera_config = {}
    _state.cameras_config = []
    _state.camera_instances = {}
    _state.camera = None
    storage = Storage({**config, 'storage': effective_storage_config()})
    _state.storage = storage
    recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})
    _state.recording_service = recording_service
    auth = AuthService(config['storage']['database'], effective_auth_config())
    _state.auth = auth
    _state.detector = create_detector(effective_ai_config())
    _state.last_detector_error = getattr(_state.detector, 'unavailable_reason', None)
    alerts = AlertEngine([])
    _state.alerts = alerts
    cameras_config = effective_cameras_config()
    _state.cameras_config = cameras_config
    camera_config = cameras_config[0] if cameras_config else {}
    _state.camera_config = camera_config
    camera_instances = create_camera_instances(cameras_config)
    _state.camera_instances = camera_instances
    _state.camera = camera_instances[camera_config['id']] if camera_config else None
    _state.recording_service.diagnostic_callback = log_camera_diagnostic


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    _startup()
    removed = _state.database.cleanup_incomplete_recordings()
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
from app.middleware import authentication_middleware, app_navigation_middleware
app.middleware('http')(authentication_middleware)
app.middleware('http')(app_navigation_middleware)
