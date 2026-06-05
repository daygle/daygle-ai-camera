from __future__ import annotations

import copy
import importlib.util
import logging
import mimetypes
import os
import re
import secrets
import tempfile
from urllib.request import urlretrieve
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.alerts import AlertEngine
from app.anpr import AnprPipeline, normalize_plate, plate_matches
from app.auth import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, AuthError, AuthService
from app.database import EventDatabase
from app.detector import DetectorUnavailableError, MockDetector, create_detector, load_labels
from app.email_alerts import EmailAlertError, EmailAlertService
from app.mock_camera import MockCamera
from app.recordings import RecordingService
from app.settings import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, load_settings
from app.storage import Storage

logger = logging.getLogger('daygle.ai')

YOLOV8N_ONNX_URL = 'https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.onnx'
ONE_PIXEL_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc``\x00\x00\x00\x04'
    b'\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'
)

config = load_settings()

auth_config = config.get('auth', {})
auth_enabled = bool(auth_config.get('enabled', True))

@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    log_detector_initialization()
    yield


app = FastAPI(title='Daygle AI Camera', lifespan=app_lifespan)

BASE_DIR = Path(__file__).resolve().parent.parent
web_dir = BASE_DIR / 'web'
static_dir = web_dir
if static_dir.exists():
    app.mount('/static', StaticFiles(directory=static_dir), name='static')

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def effective_ai_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('ai', {}))
    override = database.get_setting('ai')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_camera_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('camera', {}))
    override = database.get_setting('camera')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_anpr_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('anpr', {}))
    override = database.get_setting('anpr')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_recording_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('recording', {}))
    override = database.get_setting('recording')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_storage_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('storage', {}))
    override = database.get_setting('storage')
    if isinstance(override, dict):
        database_path = settings.get('database')
        settings.update(override)
        settings['database'] = database_path
    return settings


def effective_auth_config() -> dict[str, Any]:
    settings = copy.deepcopy(auth_config)
    override = database.get_setting('auth')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_alert_rules() -> list[dict[str, Any]]:
    return database.list_alert_rules()


def effective_email_alert_settings() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('alerts', {}).get('email', {}))
    override = database.get_setting('alert_email')
    if isinstance(override, dict):
        settings.update(override)
    return settings


database = EventDatabase(config['storage']['database'])
camera_config = effective_camera_config()
camera = MockCamera(
    width=int(camera_config.get('width', 1280)),
    height=int(camera_config.get('height', 720)),
    fps=int(camera_config.get('fps', 15)),
)
storage = Storage({**config, 'storage': effective_storage_config()})
recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})
anpr_pipeline = AnprPipeline(effective_anpr_config())
auth = AuthService(config['storage']['database'], effective_auth_config())
SESSION_COOKIE_NAME = str(effective_auth_config().get('cookie_name', SESSION_COOKIE))


database.seed_alert_rules(config.get('alerts', {}).get('rules', []), utc_now())
detector = create_detector(effective_ai_config())
last_detector_error: str | None = getattr(detector, 'unavailable_reason', None)
mock_detector = MockDetector(effective_ai_config().get('categories', []), float(effective_ai_config().get('confidence', 0.45)))
alerts = AlertEngine(effective_alert_rules())

def config_file_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)


def active_ai_config_source() -> str:
    if database.has_setting('ai'):
        return 'database'
    if config_file_path().exists():
        return 'config.yaml'
    return 'default'


def onnx_runtime_installed() -> bool:
    return importlib.util.find_spec('onnxruntime') is not None


def model_exists(ai_settings: dict[str, Any]) -> bool:
    model_path = str(ai_settings.get('model_path') or '')
    return bool(model_path) and Path(model_path).exists()


def detector_loaded_for(settings: dict[str, Any]) -> bool:
    configured_backend = str(settings.get('backend', 'mock')).lower()
    active_backend = getattr(detector, 'backend', 'unknown')
    if configured_backend == 'mock':
        return active_backend == 'mock'
    if configured_backend == 'onnx':
        return active_backend == 'onnx' and bool(getattr(detector, 'available', False))
    return False


def ai_status_payload(ai_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = ai_settings or effective_ai_config()
    active_backend = getattr(detector, 'backend', 'unknown')
    configured_backend = str(settings.get('backend', 'mock')).lower()
    detector_loaded = detector_loaded_for(settings)
    model_loaded = bool(configured_backend == 'onnx' and active_backend == 'onnx' and getattr(detector, 'available', False))
    runtime_installed = onnx_runtime_installed()
    exists = model_exists(settings)
    detector_reason = getattr(detector, 'unavailable_reason', None)
    error = last_detector_error or detector_reason
    if configured_backend == 'onnx' and not exists:
        mode = 'MODEL MISSING'
        error = error or f"ONNX model not found: {settings.get('model_path')}"
    elif configured_backend == 'onnx' and not model_loaded:
        mode = 'MODEL FAILED'
    elif configured_backend == 'onnx':
        mode = 'ONNX ACTIVE'
        error = detector_reason
    else:
        mode = 'MOCK MODE'
        error = detector_reason if active_backend == 'mock' else error
    inference_available = detector_loaded
    return {
        'current_backend': configured_backend,
        'active_backend': active_backend,
        'configured_backend': configured_backend,
        'mode': mode,
        'model_loaded': model_loaded,
        'detector_loaded': detector_loaded,
        'model_path': str(settings.get('model_path') or ''),
        'labels_path': str(settings.get('labels_path') or ''),
        'model_exists': exists,
        'onnx_runtime_installed': runtime_installed,
        'inference_available': inference_available,
        'error': error,
        'last_detector_error': error,
        'active_config_source': active_ai_config_source(),
    }


def log_detector_initialization(context: str = 'startup') -> None:
    ai_status = ai_status_payload()
    logger.info(
        'AI detector %s: active_backend=%s configured_backend=%s model_loaded=%s '
        'inference_available=%s model_path=%s labels_path=%s error=%s',
        context,
        ai_status['active_backend'],
        ai_status['configured_backend'],
        ai_status['model_loaded'],
        ai_status['inference_available'],
        ai_status['model_path'] or '<none>',
        ai_status['labels_path'] or '<none>',
        ai_status['error'] or '<none>',
    )

PUBLIC_PREFIXES = ('/static/',)
PUBLIC_PATHS = {'/login', '/setup'}
ADMIN_PATHS = {'/settings', '/alert-settings', '/system-settings', '/users'}
ADMIN_API_PREFIXES = ('/api/users', '/api/settings')
MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


@app.middleware('http')
async def authentication_middleware(request: Request, call_next):
    if not auth_enabled:
        return await call_next(request)

    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
        return await call_next(request)

    has_users = auth.users_exist()
    if not has_users:
        if path.startswith('/api/'):
            return JSONResponse({'detail': 'Initial administrator setup is required.'}, status_code=403)
        return RedirectResponse('/setup', status_code=303)

    session = auth.get_session(request.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        if path.startswith('/api/'):
            return JSONResponse({'detail': 'Authentication required'}, status_code=401)
        return RedirectResponse('/login', status_code=303)

    request.state.session = session
    request.state.user = session['user']

    admin_required = path in ADMIN_PATHS or path.startswith('/api/users') or path.startswith('/api/settings/ai') or path.startswith('/api/settings/anpr') or path.startswith('/api/settings/system') or (
        path.startswith('/api/settings/alert-email') and request.method in MUTATING_METHODS
    ) or (
        path.startswith('/api/settings/alerts') and request.method in MUTATING_METHODS
    )
    if admin_required and session['user']['role'] != 'admin':
        return JSONResponse({'detail': 'Admin access required'}, status_code=403)

    if (path.startswith('/api/') or path == '/logout') and request.method in MUTATING_METHODS:
        csrf_header = request.headers.get(CSRF_HEADER)
        if not csrf_header or csrf_header != session['csrf_token']:
            return JSONResponse({'detail': 'CSRF token missing or invalid'}, status_code=403)

    return await call_next(request)


@app.middleware('http')
async def app_navigation_middleware(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get('content-type', '')
    if request.url.path in PUBLIC_PATHS or not content_type.startswith('text/html'):
        return response
    body = b''
    async for chunk in response.body_iterator:
        body += chunk
    marker = b'</body>'
    script = b'<script src="/static/nav.js"></script>'
    if marker in body and script not in body:
        body = body.replace(marker, script + marker)
    headers = dict(response.headers)
    headers.pop('content-length', None)
    return Response(content=body, status_code=response.status_code, headers=headers, media_type='text/html')


def set_session_cookie(response: Response, request: Request, token: str, expires_at: str) -> None:
    session_hours = float(effective_auth_config().get('session_timeout_hours', 12))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
        expires=expires_at,
        max_age=int(session_hours * 3600),
    )


def set_csrf_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=request.url.scheme == 'https', samesite='lax', max_age=3600)


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE)


async def form_data(request: Request) -> dict[str, str]:
    body = (await request.body()).decode('utf-8')
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}


def auth_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{escape(title)} · Daygle AI Camera</title><link rel="stylesheet" href="/static/styles.css" /></head>
<body><main class="auth-shell"><section class="card auth-card"><p class="eyebrow">Daygle AI Camera</p>{body}</section></main></body></html>""")


def csrf_token_response(request: Request, title: str, body_template: str, *, status_code: int = 200) -> HTMLResponse:
    token = secrets.token_urlsafe(32)
    response = auth_page(title, body_template.format(csrf=escape(token)))
    response.status_code = status_code
    set_csrf_cookie(response, token, request)
    return response


def require_user(request: Request) -> dict[str, Any]:
    return request.state.user


def require_session(request: Request) -> dict[str, Any]:
    return request.state.session


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='Admin access required')
    return user


def attach_event_recording(event_id: int, event_time: str, source: str, detections: list[dict[str, Any]]) -> int | None:
    metadata = recording_service.event_recording_metadata(event_id, event_time, source, detections)
    if metadata is None:
        return None
    recording_id = database.add_recording(created_at=utc_now(), **metadata)
    purge_recordings_by_policy()
    return recording_id


def deliver_email_alerts(triggered: list[dict[str, Any]], event_id: int) -> None:
    if not triggered:
        return
    rules_by_name = {str(rule.get('name')): rule for rule in effective_alert_rules()}
    mailer = EmailAlertService(effective_email_alert_settings())
    for alert in triggered:
        rule = rules_by_name.get(str(alert.get('rule_name')))
        if not rule or not rule.get('email_enabled'):
            continue
        try:
            mailer.send_alert(alert, event_id=event_id, recipients=rule.get('email_recipients', []))
        except EmailAlertError as exc:
            logger.warning('Failed to send email alert for event %s rule %s: %s', event_id, alert.get('rule_name'), exc)


plate_alert_last_triggered: dict[str, float] = {}


def process_anpr_for_event(event_id: int, detections: list[dict[str, Any]], image_path: str | None, created_at: str) -> list[dict[str, Any]]:
    plate_results = anpr_pipeline.process_event(event_id=event_id, detections=detections, image_path=image_path, storage=storage)
    stored: list[dict[str, Any]] = []
    for result in plate_results:
        plate = database.upsert_plate(result['plate_number'], created_at)
        plate_event = database.add_plate_event(
            event_id=event_id,
            plate_id=plate['id'],
            plate_number=result['plate_number'],
            confidence=float(result['confidence']),
            image_path=result.get('image_path'),
            created_at=created_at,
        )
        stored.append(plate_event)
    trigger_plate_alerts(stored)
    return stored


def trigger_plate_alerts(plate_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import time

    triggered: list[dict[str, Any]] = []
    rules = database.list_plate_alert_rules()
    for plate_event in plate_events:
        plate = plate_event.get('plate') or {}
        for rule in rules:
            if not rule.get('enabled', True):
                continue
            rule_key = f"{rule['id']}:{plate_event['plate_number']}"
            last = plate_alert_last_triggered.get(rule_key, 0)
            if time.time() - last < int(rule.get('cooldown_seconds', 60)):
                continue
            rule_type = str(rule.get('rule_type'))
            matched = False
            if rule_type == 'plate':
                matched = plate_matches(rule.get('plate_pattern'), plate_event['plate_number'])
            elif rule_type == 'unknown':
                matched = not plate.get('is_whitelisted') and not plate.get('is_blacklisted')
            elif rule_type == 'blacklisted':
                matched = bool(plate.get('is_blacklisted'))
            if matched:
                plate_alert_last_triggered[rule_key] = time.time()
                triggered.append({'rule_name': rule['rule_name'], 'plate_number': plate_event['plate_number'], 'plate_event_id': plate_event['id']})
    return triggered


def delete_recording_files(recordings: list[dict[str, Any]]) -> None:
    for recording in recordings:
        file_path = Path(str(recording.get('file_path') or ''))
        if file_path.exists() and file_path.is_file():
            file_path.unlink(missing_ok=True)
        thumbnail_path = recording.get('thumbnail_path')
        if thumbnail_path:
            thumbnail = Path(str(thumbnail_path))
            if thumbnail.exists() and thumbnail.is_file():
                thumbnail.unlink(missing_ok=True)


def purge_recordings_by_policy(*, force: bool = False) -> dict[str, Any]:
    recording_settings = effective_recording_config()
    if not force and not _bool_value(recording_settings.get('auto_purge_enabled', True)):
        return {'purged': 0, 'files_deleted': 0, 'bytes_deleted': 0, 'recordings': []}
    retention_days = int(recording_settings.get('retention_days', 14))
    max_storage_gb = int(recording_settings.get('max_storage_gb', 20))
    older_than = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    max_storage_bytes = max_storage_gb * 1024 * 1024 * 1024
    purged = database.purge_recordings(older_than=older_than, max_storage_bytes=max_storage_bytes)
    bytes_deleted = 0
    files_deleted = 0
    for recording in purged:
        file_path = Path(str(recording.get('file_path') or ''))
        if file_path.exists() and file_path.is_file():
            bytes_deleted += file_path.stat().st_size
            files_deleted += 1
    delete_recording_files(purged)
    return {'purged': len(purged), 'files_deleted': files_deleted, 'bytes_deleted': bytes_deleted, 'recordings': purged}


@app.get('/login')
def login_page(request: Request, error: str | None = None):
    if auth_enabled and auth.users_exist() and auth.get_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return RedirectResponse('/', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return csrf_token_response(request, 'Login', f"""
<h1>Sign in</h1><p class="muted">Enter your Daygle AI Camera credentials.</p>{error_html}
<form class="form-stack" method="post" action="/login">
  <input type="hidden" name="csrf_token" value="{{csrf}}" />
  <label>Username<input name="username" autocomplete="username" required /></label>
  <label>Password<input name="password" type="password" autocomplete="current-password" required /></label>
  <button class="primary" type="submit">Sign in</button>
</form>""")


@app.post('/login')
async def login(request: Request):
    data = await form_data(request)
    if data.get('csrf_token') != request.cookies.get(CSRF_COOKIE):
        return login_page(request, 'Security token expired. Try again.')
    try:
        _user, token, _csrf_token, expires_at = auth.authenticate(
            data.get('username', ''), data.get('password', ''), request.client.host if request.client else 'unknown'
        )
    except AuthError as exc:
        return login_page(request, str(exc))
    response = RedirectResponse('/', status_code=303)
    set_session_cookie(response, request, token, expires_at)
    response.delete_cookie(CSRF_COOKIE)
    return response


@app.get('/setup')
def setup_page(request: Request, error: str | None = None):
    if auth_enabled and auth.users_exist():
        return RedirectResponse('/login', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return csrf_token_response(request, 'Initial setup', f"""
<h1>Create administrator</h1><p class="muted">This one-time setup is disabled after the first user is created.</p>{error_html}
<form class="form-stack" method="post" action="/setup">
  <input type="hidden" name="csrf_token" value="{{csrf}}" />
  <label>Username<input name="username" value="admin" autocomplete="username" required /></label>
  <label>Password<input name="password" type="password" autocomplete="new-password" required /></label>
  <label>Confirm password<input name="confirm_password" type="password" autocomplete="new-password" required /></label>
  <button class="primary" type="submit">Create admin account</button>
</form>""")


@app.post('/setup')
async def setup(request: Request):
    if auth.users_exist():
        return RedirectResponse('/login', status_code=303)
    data = await form_data(request)
    if data.get('csrf_token') != request.cookies.get(CSRF_COOKIE):
        return setup_page(request, 'Security token expired. Try again.')
    if data.get('password') != data.get('confirm_password'):
        return setup_page(request, 'Passwords do not match.')
    try:
        auth.create_user(data.get('username', ''), data.get('password', ''), role='admin')
    except AuthError as exc:
        return setup_page(request, str(exc))
    return RedirectResponse('/login', status_code=303)


@app.get('/logout')
def logout_get(request: Request):
    auth.delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = RedirectResponse('/login', status_code=303)
    clear_auth_cookies(response)
    return response


@app.post('/logout')
def logout_post(request: Request):
    session = require_session(request)
    if request.headers.get(CSRF_HEADER) != session['csrf_token']:
        return JSONResponse({'detail': 'CSRF token missing or invalid'}, status_code=403)
    auth.delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({'ok': True})
    clear_auth_cookies(response)
    return response


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
        return body, None, content_type

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
        return payload.rstrip(b'\r\n-'), filename, uploaded_type

    raise HTTPException(status_code=400, detail='Multipart upload must include a file field named file')


@app.get('/')
def root():
    index_path = web_dir / 'index.html'
    if index_path.exists():
        return FileResponse(index_path)
    return {'application': 'Daygle AI Camera', 'status': 'running'}


@app.get('/events')
@app.get('/alerts')
@app.get('/search')
@app.get('/recordings')
def dashboard_aliases():
    return root()


@app.get('/settings')
def settings_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Setup / AI Settings · Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell page-stack"><header class="hero"><div><p class="eyebrow">Administration</p><h1>Setup / AI Settings</h1><p class="muted">Configure AI detection, install models, and reload the detector.</p></div><div class="hero-actions"><a class="button-link secondary-link" href="/system-settings">System settings</a><a class="button-link secondary-link" href="/alert-settings">Alert settings</a><a class="button-link" href="/">Dashboard</a></div></header><section class="card"><h2>AI status</h2><div id="settingsMessage" class="muted"></div><div id="aiStatusPanel" class="status-panel"></div><div class="button-row"><button id="checkModelBtn" class="secondary" type="button">Check model</button><button id="downloadModelBtn" type="button">Download YOLOv8n ONNX</button><button id="reloadDetectorBtn" class="secondary" type="button">Reload detector</button><button id="testDetectorBtn" class="secondary" type="button">Test detector</button></div></section><section class="card"><h2>AI settings</h2><form id="aiSettingsForm" class="form-grid"><label><span>AI enabled</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><label><span>Backend</span><select name="backend"><option value="mock">mock</option><option value="onnx">onnx</option></select></label><label><span>Confidence</span><input name="confidence" type="number" min="0" max="1" step="0.01" /></label><label><span>IOU threshold</span><input name="iou_threshold" type="number" min="0" max="1" step="0.01" /></label><label><span>Input size</span><input name="input_size" type="number" min="32" max="2048" step="32" /></label><label><span>Model path</span><input name="model_path" /></label><label><span>Labels path</span><input name="labels_path" /></label><button type="submit">Save AI settings</button></form></section></main><script src="/static/settings.js"></script></body></html>""")


@app.get('/alert-settings')
def alert_settings_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Alert Settings · Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell page-stack"><header class="hero"><div><p class="eyebrow">Administration</p><h1>Alert Settings</h1><p class="muted">Manage detection rules and email delivery through your mail server.</p></div><div class="hero-actions"><a class="button-link secondary-link" href="/settings">AI settings</a><a class="button-link" href="/">Dashboard</a></div></header><section class="card"><h2>Email delivery</h2><div id="settingsMessage" class="muted"></div><form id="emailSettingsForm" class="form-grid"><label><span>Email alerts</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><input name="host" placeholder="SMTP host" /><input name="port" type="number" min="1" max="65535" placeholder="Port" /><input name="from_address" type="email" placeholder="From address" /><input name="username" placeholder="SMTP username" /><input name="password" type="password" placeholder="SMTP password" autocomplete="new-password" /><label><span>STARTTLS</span><select name="use_tls"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><label><span>SSL</span><select name="use_ssl"><option value="false">Disabled</option><option value="true">Enabled</option></select></label><button type="submit">Save mail server</button></form></section><section class="card"><h2>Alert rules</h2><form id="alertRuleForm" class="form-grid"><input type="hidden" name="id" /><input name="name" placeholder="Rule name" required /><input name="object" list="labelOptions" placeholder="Object label" required /><datalist id="labelOptions"></datalist><input name="min_confidence" type="number" min="0" max="1" step="0.01" placeholder="Min confidence" value="0.6" /><input name="cooldown_seconds" type="number" min="0" step="1" placeholder="Cooldown seconds" value="60" /><label><span>Rule</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><label><span>Email</span><select name="email_enabled"><option value="false">Disabled</option><option value="true">Enabled</option></select></label><input name="email_recipients" placeholder="Email recipients, comma separated" /><input name="active_start" type="time" /><input name="active_end" type="time" /><button type="submit">Save alert rule</button><button id="cancelEditRule" class="secondary" type="button">Cancel edit</button></form><div id="alertRules" class="list"></div></section></main><script src="/static/alert-settings.js"></script></body></html>""")


@app.get('/profile')
def profile_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Profile · Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell page-stack"><header class="hero"><div><p class="eyebrow">Account</p><h1>Profile</h1><p class="muted">Manage your display preferences and password.</p></div><a class="button-link" href="/">Dashboard</a></header><section class="card"><h2>Profile details</h2><div id="profileMessage" class="muted"></div><div id="profileSummary" class="status-panel"></div><form id="profileForm" class="form-grid"><input name="timezone" placeholder="Timezone" required /><label><span>Date format</span><select name="date_format"><option value="locale">Browser locale</option><option value="iso">YYYY-MM-DD</option><option value="au">DD/MM/YYYY</option><option value="us">MM/DD/YYYY</option></select></label><label><span>Time format</span><select name="time_format"><option value="24h">24 hour</option><option value="12h">12 hour</option></select></label><button type="submit">Save profile</button></form></section><section class="card"><h2>Change password</h2><form id="passwordForm" class="form-grid"><input name="current_password" type="password" placeholder="Current password" autocomplete="current-password" required /><input name="new_password" type="password" placeholder="New password" autocomplete="new-password" required /><input name="confirm_password" type="password" placeholder="Confirm password" autocomplete="new-password" required /><button type="submit">Change password</button></form></section></main><script src="/static/profile.js"></script></body></html>""")


@app.get('/anpr')
def anpr_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>ANPR · Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell page-stack"><header class="hero"><div><p class="eyebrow">Recognition</p><h1>ANPR</h1><p class="muted">Search plates, review sightings, and manage plate alerts.</p></div><a class="button-link" href="/">Dashboard</a></header><section class="card"><h2>Plate search</h2><div id="anprMessage" class="muted"></div><div class="search-row"><input id="plateSearchInput" placeholder="ABC123, 1ABC2D, XYZ999..." /><button id="plateSearchBtn">Search</button><button id="plateClearBtn" class="secondary">Recent</button></div><div id="plateResults" class="list"></div></section><section class="grid main-grid"><article class="card"><div class="section-header"><h2>Recent plates</h2></div><div id="recentPlates" class="list"></div></article><article class="card"><div class="section-header"><h2>Plate details</h2></div><div id="plateDetails" class="list"></div></article></section><section class="card"><h2>Plate alert rules</h2><form id="plateAlertRuleForm" class="form-grid"><input type="hidden" name="id" /><input name="rule_name" placeholder="Rule name" required /><label><span>Type</span><select name="rule_type"><option value="plate">Specific plate</option><option value="unknown">Unknown plate</option><option value="blacklisted">Blacklisted plate</option></select></label><input name="plate_pattern" placeholder="Plate pattern" /><input name="cooldown_seconds" type="number" min="0" placeholder="Cooldown seconds" value="60" /><label><span>Enabled</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><button type="submit">Save rule</button><button id="cancelPlateRuleEdit" class="secondary" type="button">Cancel edit</button></form><div id="plateAlertRules" class="list"></div></section></main><script src="/static/anpr.js"></script></body></html>""")


@app.get('/system-settings')
def system_settings_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>System Settings · Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell page-stack"><header class="hero"><div><p class="eyebrow">Administration</p><h1>System Settings</h1><p class="muted">Move day-to-day camera, recording, storage, and login settings out of YAML.</p></div><div class="hero-actions"><a class="button-link secondary-link" href="/settings">AI settings</a><a class="button-link" href="/">Dashboard</a></div></header><section class="card"><h2>Camera</h2><div id="systemMessage" class="muted"></div><form id="cameraSettingsForm" class="form-grid"><label><span>Backend</span><select name="backend"><option value="mock">mock</option></select></label><input name="device" placeholder="Device" /><input name="width" type="number" min="160" max="7680" step="1" placeholder="Width" /><input name="height" type="number" min="120" max="4320" step="1" placeholder="Height" /><input name="fps" type="number" min="1" max="120" step="1" placeholder="FPS" /><label><span>Flip</span><select name="flip"><option value="none">none</option><option value="horizontal">horizontal</option><option value="vertical">vertical</option><option value="both">both</option></select></label><button type="submit">Save camera</button></form></section><section class="card"><h2>ANPR</h2><form id="anprSettingsForm" class="form-grid"><label><span>ANPR</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><label><span>OCR backend</span><select name="backend"><option value="mock">mock</option><option value="paddleocr">paddleocr</option><option value="easyocr">easyocr</option></select></label><input name="min_confidence" type="number" min="0" max="1" step="0.01" placeholder="Min confidence" /><input name="vehicle_labels" placeholder="Vehicle labels: car, truck, bus, motorcycle" /><button type="submit">Save ANPR</button></form></section><section class="card"><h2>Recording policy</h2><form id="recordingSettingsForm" class="form-grid"><label><span>Recording</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><label><span>Primary mode</span><select name="mode"><option value="motion">motion</option><option value="continuous">continuous</option><option value="human">human</option><option value="objects">objects</option><option value="off">off</option></select></label><label><span>Continuous</span><select name="continuous"><option value="false">Disabled</option><option value="true">Enabled</option></select></label><label><span>Record on motion</span><select name="record_on_motion"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><label><span>Record on human</span><select name="record_on_human"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><input name="record_on_objects" placeholder="Objects: cat, dog, package, parcel" /><input name="pre_event_seconds" type="number" min="0" max="300" placeholder="Pre-event seconds" /><input name="post_event_seconds" type="number" min="0" max="300" placeholder="Post-event seconds" /><input name="max_clip_seconds" type="number" min="1" max="3600" placeholder="Max clip seconds" /><input name="format" placeholder="Format: avi or mp4" /><button type="submit">Save recording</button></form></section><section class="card"><h2>Retention</h2><form id="retentionSettingsForm" class="form-grid"><label><span>Auto purge</span><select name="auto_purge_enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><input name="retention_days" type="number" min="1" max="3650" placeholder="Retention days" /><input name="max_storage_gb" type="number" min="1" max="100000" placeholder="Max storage GB" /><button type="submit">Save retention</button><button id="purgeRecordingsBtn" class="secondary" type="button">Purge now</button></form></section><section class="card"><h2>Storage</h2><form id="storageSettingsForm" class="form-grid"><input name="data_dir" placeholder="Data directory" /><input name="snapshots_dir" placeholder="Snapshots directory" /><input name="events_dir" placeholder="Events directory" /><input name="recordings_dir" placeholder="Recordings directory" /><input name="plates_dir" placeholder="Plate images directory" /><button type="submit">Save storage</button></form><p class="muted">Database file location stays in config.yaml because it is needed before the web UI can load.</p></section><section class="card"><h2>Login security</h2><form id="authSettingsForm" class="form-grid"><input name="session_timeout_hours" type="number" min="0.25" max="720" step="0.25" placeholder="Session timeout hours" /><input name="max_login_attempts" type="number" min="1" max="100" placeholder="Max login attempts" /><input name="lockout_minutes" type="number" min="1" max="1440" placeholder="Lockout minutes" /><button type="submit">Save login security</button></form><p class="muted">Auth enablement and cookie name remain bootstrap settings.</p></section></main><script src="/static/system-settings.js"></script></body></html>""")

@app.get('/users')
def users_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Users · Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell"><header class="hero"><div><p class="eyebrow">Administration</p><h1>User Management</h1><p class="muted">Create users, change roles, disable accounts, and reset passwords.</p></div><a class="button-link" href="/">Dashboard</a></header><section class="card"><div id="userMessage" class="muted"></div><div id="users" class="list"></div></section><section class="card"><h2>Create user</h2><form id="createUserForm" class="form-grid"><input name="username" placeholder="Username" required /><select name="role"><option value="viewer">viewer</option><option value="admin">admin</option></select><input name="password" type="password" placeholder="Temporary password" required /><button>Create</button></form></section></main><script src="/static/users.js"></script></body></html>""")


@app.get('/api/auth/me')
def me(request: Request):
    session = require_session(request)
    return {'user': session['user'], 'csrf_token': session['csrf_token'], 'expires_at': session['expires_at']}


@app.put('/api/profile')
async def update_profile(request: Request):
    user = require_user(request)
    payload = await request.json()
    try:
        updated = auth.update_profile(
            int(user['id']),
            timezone_name=payload.get('timezone'),
            date_format=payload.get('date_format'),
            time_format=payload.get('time_format'),
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.state.user = updated
    return updated


@app.post('/api/profile/password')
async def change_profile_password(request: Request):
    user = require_user(request)
    payload = await request.json()
    try:
        auth.change_password(int(user['id']), str(payload.get('current_password') or ''), str(payload.get('new_password') or ''))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True}


@app.get('/api/status')
def status():
    frame = camera.get_frame()
    ai_state = ai_status_payload()
    return {
        'status': 'online',
        'mode': camera_config.get('backend', 'mock'),
        'ai_backend': ai_state['active_backend'],
        'ai_available': ai_state['inference_available'],
        'ai_error': ai_state['error'],
        'ai_mode': ai_state['mode'],
        'frame_number': frame['frame_number'],
        'uptime_seconds': frame['uptime_seconds'],
        'resolution': {'width': frame['width'], 'height': frame['height']},
    }


@app.get('/api/status/ai')
def ai_status():
    return ai_status_payload()


@app.post('/api/mock/detect')
def generate_detection(force: bool = True):
    frame = camera.get_frame()
    active_mock_detector = detector if hasattr(detector, 'detect') else mock_detector
    detections = active_mock_detector.detect(frame['frame_number'], force=force)

    if not detections:
        return {'created': False, 'message': 'No detections generated'}

    snapshot_path = storage.save_mock_snapshot(frame, detections)
    alerts.rules = effective_alert_rules()
    triggered = alerts.process(detections)

    event_time = datetime.now(timezone.utc).isoformat()
    event_id = database.add_event(
        created_at=event_time,
        source='mock-camera',
        snapshot_path=snapshot_path,
        detections=detections,
        alert_triggered=bool(triggered),
    )
    recording_id = attach_event_recording(event_id, event_time, 'mock-camera', detections)
    plate_events = process_anpr_for_event(event_id, detections, snapshot_path, event_time)

    for alert in triggered:
        database.add_alert(
            created_at=datetime.now(timezone.utc).isoformat(),
            rule_name=alert['rule_name'],
            event_id=event_id,
            label=alert['label'],
            confidence=alert['confidence'],
            message=alert['message'],
        )
    deliver_email_alerts(triggered, event_id)

    return {
        'created': True,
        'event_id': event_id,
        'recording_id': recording_id,
        'detections': detections,
        'alerts': triggered,
        'plate_events': plate_events,
    }


@app.post('/api/detect/test-image')
async def detect_test_image(request: Request):
    image_bytes, filename, content_type = await _read_uploaded_image(request)
    if not image_bytes:
        raise HTTPException(status_code=400, detail='Uploaded image is empty')

    ai_settings = effective_ai_config()
    ai_state = ai_status_payload(ai_settings)
    if ai_settings.get('backend') == 'onnx' and not ai_state['detector_loaded']:
        raise HTTPException(status_code=400, detail=ai_state['last_detector_error'] or 'ONNX detector is not loaded.')
    if ai_settings.get('backend') == 'mock' and getattr(detector, 'backend', None) != 'mock':
        raise HTTPException(status_code=400, detail='Mock detector is not loaded. Save AI settings or reload the detector.')

    try:
        detections = detector.detect_image(image_bytes)
    except DetectorUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    snapshot_path = storage.save_image_snapshot(image_bytes, filename)
    alerts.rules = effective_alert_rules()
    triggered = alerts.process(detections)
    event_time = datetime.now(timezone.utc).isoformat()
    event_id = database.add_event(
        created_at=event_time,
        source='test-image',
        snapshot_path=snapshot_path,
        detections=detections,
        alert_triggered=bool(triggered),
        metadata={
            'filename': filename,
            'content_type': content_type,
            'ai_backend': ai_state['configured_backend'],
            'detector_backend': ai_state['active_backend'],
        },
    )

    recording_id = attach_event_recording(event_id, event_time, 'test-image', detections)
    plate_events = process_anpr_for_event(event_id, detections, snapshot_path, event_time)

    for alert in triggered:
        database.add_alert(
            created_at=datetime.now(timezone.utc).isoformat(),
            rule_name=alert['rule_name'],
            event_id=event_id,
            label=alert['label'],
            confidence=alert['confidence'],
            message=alert['message'],
        )
    deliver_email_alerts(triggered, event_id)

    return {
        'created': True,
        'event_id': event_id,
        'recording_id': recording_id,
        'detections': detections,
        'alerts': triggered,
        'plate_events': plate_events,
        'snapshot_path': snapshot_path,
        'ai_backend': ai_state['configured_backend'],
        'backend_used': ai_state['configured_backend'],
        'detector_backend': ai_state['active_backend'],
    }


@app.get('/api/events')
def events(label: str | None = None, limit: int = Query(50, ge=1, le=200)):
    return database.search_events(label=label, limit=limit)


@app.get('/api/events/{event_id}')
def event_detail(event_id: int):
    event = database.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    return event


@app.get('/api/alerts')
def alert_history(limit: int = Query(25, ge=1, le=200)):
    return database.alerts(limit=limit)


@app.get('/api/stats')
def stats():
    return database.stats()


@app.get('/api/config')
def runtime_config():
    ai_state = ai_status_payload()
    return {
        'server': {'host': config.get('server', {}).get('host'), 'port': config.get('server', {}).get('port')},
        'camera': effective_camera_config(),
        'ai': {
            'enabled': effective_ai_config().get('enabled'),
            'backend': effective_ai_config().get('backend'),
            'confidence': effective_ai_config().get('confidence'),
            'iou_threshold': effective_ai_config().get('iou_threshold'),
            'input_size': effective_ai_config().get('input_size'),
            'model_path': effective_ai_config().get('model_path'),
            'labels_path': effective_ai_config().get('labels_path'),
            'active_backend': ai_state['active_backend'],
            'mode': ai_state['mode'],
            'available': ai_state['inference_available'],
            'model_loaded': ai_state['model_loaded'],
            'error': ai_state['error'],
            'categories': effective_ai_config().get('categories', []),
        },
        'anpr': effective_anpr_config(),
        'alerts': config.get('alerts', {}),
        'auth': {
            'enabled': auth_enabled,
            'session_timeout_hours': effective_auth_config().get('session_timeout_hours'),
            'max_login_attempts': effective_auth_config().get('max_login_attempts'),
            'lockout_minutes': effective_auth_config().get('lockout_minutes'),
        },
        'storage': {
            'database': effective_storage_config().get('database'),
            'snapshots_dir': effective_storage_config().get('snapshots_dir'),
            'recordings_dir': effective_storage_config().get('recordings_dir'),
            'plates_dir': effective_storage_config().get('plates_dir'),
        },
        'recording': effective_recording_config(),
    }


@app.get('/api/recordings')
def recordings(label: str | None = None, limit: int = Query(50, ge=1, le=200)):
    return database.list_recordings(label=label, limit=limit)


@app.post('/api/recordings/purge')
def purge_recordings(request: Request):
    require_admin(request)
    return purge_recordings_by_policy(force=True)


@app.get('/api/recordings/{recording_id}')
def recording_detail(recording_id: int):
    recording = database.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    return recording


@app.get('/api/recordings/{recording_id}/stream')
def stream_recording(recording_id: int, request: Request):
    recording = database.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    file_path = Path(recording['file_path'])
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail='Recording media file not found')

    file_size = file_path.stat().st_size
    media_type = mimetypes.guess_type(file_path.name)[0] or 'video/mp4'
    range_header = request.headers.get('range')
    if not range_header:
        return FileResponse(file_path, media_type=media_type)

    match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip())
    if not match:
        return Response(status_code=416, headers={'Content-Range': f'bytes */{file_size}'})
    start_text, end_text = match.groups()
    start = int(start_text) if start_text else 0
    end = int(end_text) if end_text else file_size - 1
    if start >= file_size or end < start:
        return Response(status_code=416, headers={'Content-Range': f'bytes */{file_size}'})
    end = min(end, file_size - 1)
    chunk_size = end - start + 1

    def iter_file():
        with file_path.open('rb') as handle:
            handle.seek(start)
            remaining = chunk_size
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iter_file(),
        status_code=206,
        media_type=media_type,
        headers={
            'Content-Range': f'bytes {start}-{end}/{file_size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(chunk_size),
        },
    )


@app.delete('/api/recordings/{recording_id}')
def delete_recording(recording_id: int, request: Request):
    require_admin(request)
    recording = database.delete_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    delete_recording_files([recording])
    return {'ok': True}


@app.get('/api/users')
def list_users(request: Request):
    require_user(request)
    return auth.list_users()


@app.post('/api/users')
async def create_user(request: Request):
    payload = await request.json()
    try:
        return auth.create_user(payload.get('username', ''), payload.get('password', ''), payload.get('role', 'viewer'))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch('/api/users/{user_id}')
async def update_user(user_id: int, request: Request):
    payload = await request.json()
    try:
        return auth.update_user(user_id, role=payload.get('role'), is_active=payload.get('is_active'), password=payload.get('password'))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get('/api/plates')
def list_plates(limit: int = Query(50, ge=1, le=200)):
    return database.list_plates(limit=limit)


@app.get('/api/plates/search')
def search_plates(q: str = '', limit: int = Query(50, ge=1, le=200)):
    return database.search_plates(normalize_plate(q), limit=limit)


@app.post('/api/plates/whitelist')
async def whitelist_plate(request: Request):
    require_admin(request)
    payload = await request.json()
    plate_number = normalize_plate(payload.get('plate_number') or '')
    if not plate_number:
        raise HTTPException(status_code=400, detail='plate_number is required.')
    return database.update_plate_status(plate_number, notes=payload.get('notes'), is_whitelisted=True, is_blacklisted=False)


@app.post('/api/plates/blacklist')
async def blacklist_plate(request: Request):
    require_admin(request)
    payload = await request.json()
    plate_number = normalize_plate(payload.get('plate_number') or '')
    if not plate_number:
        raise HTTPException(status_code=400, detail='plate_number is required.')
    return database.update_plate_status(plate_number, notes=payload.get('notes'), is_whitelisted=False, is_blacklisted=True)


@app.get('/api/plates/{plate_id}')
def get_plate(plate_id: int):
    plate = database.get_plate(plate_id)
    if plate is None:
        raise HTTPException(status_code=404, detail='Plate not found')
    return plate


@app.get('/api/plate-alerts')
def list_plate_alerts():
    return database.list_plate_alert_rules()


@app.post('/api/plate-alerts')
async def create_plate_alert(request: Request):
    require_admin(request)
    return database.create_plate_alert_rule(validate_plate_alert_rule(await request.json()), utc_now())


@app.put('/api/plate-alerts/{rule_id}')
async def update_plate_alert(rule_id: int, request: Request):
    require_admin(request)
    rule = database.update_plate_alert_rule(rule_id, validate_plate_alert_rule(await request.json(), partial=True), utc_now())
    if rule is None:
        raise HTTPException(status_code=404, detail='Plate alert rule not found')
    return rule


@app.delete('/api/plate-alerts/{rule_id}')
def delete_plate_alert(rule_id: int, request: Request):
    require_admin(request)
    if not database.delete_plate_alert_rule(rule_id):
        raise HTTPException(status_code=404, detail='Plate alert rule not found')
    return {'ok': True}


def validate_ai_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_ai_config()
    allowed = {'enabled', 'backend', 'confidence', 'iou_threshold', 'input_size', 'model_path', 'labels_path'}
    updated = {key: current.get(key) for key in allowed if key in current}
    for key, value in payload.items():
        if key in allowed:
            updated[key] = value
    enabled_value = updated.get('enabled', True)
    if isinstance(enabled_value, str):
        updated['enabled'] = enabled_value.lower() in {'1', 'true', 'yes', 'on'}
    else:
        updated['enabled'] = bool(enabled_value)
    backend = str(updated.get('backend', 'mock')).lower()
    if backend not in {'mock', 'onnx'}:
        raise HTTPException(status_code=400, detail='AI backend must be mock or onnx.')
    updated['backend'] = backend
    for field in ('confidence', 'iou_threshold'):
        try:
            updated[field] = float(updated.get(field, 0.45))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f'{field} must be a number.') from exc
        if not 0 <= updated[field] <= 1:
            raise HTTPException(status_code=400, detail=f'{field} must be between 0 and 1.')
    try:
        updated['input_size'] = int(updated.get('input_size', 640))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='input_size must be an integer.') from exc
    if updated['input_size'] < 32 or updated['input_size'] > 2048:
        raise HTTPException(status_code=400, detail='input_size must be between 32 and 2048.')
    updated['model_path'] = str(updated.get('model_path') or current.get('model_path') or 'models/yolov8n.onnx')
    updated['labels_path'] = str(updated.get('labels_path') or current.get('labels_path') or 'models/coco.names')
    return updated


def detector_status(ai_settings: dict[str, Any]) -> dict[str, Any]:
    ai_status = ai_status_payload(ai_settings)
    return {
        **ai_settings,
        'active_backend': ai_status['active_backend'],
        'configured_backend': ai_status['configured_backend'],
        'current_backend': ai_status['current_backend'],
        'mode': ai_status['mode'],
        'available': ai_status['inference_available'],
        'model_loaded': ai_status['model_loaded'],
        'detector_loaded': ai_status['detector_loaded'],
        'model_exists': ai_status['model_exists'],
        'onnx_runtime_installed': ai_status['onnx_runtime_installed'],
        'active_config_source': ai_status['active_config_source'],
        'error': ai_status['error'],
        'last_detector_error': ai_status['last_detector_error'],
        'categories': ai_settings.get('categories', config.get('ai', {}).get('categories', [])),
    }


def available_labels() -> list[str]:
    ai_settings = effective_ai_config()
    labels = load_labels(ai_settings.get('labels_path'), ai_settings.get('categories', []))
    return labels or list(ai_settings.get('categories', []))


def validate_alert_rule(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    rule: dict[str, Any] = {}
    required = ('name', 'object')
    for field in required:
        if not partial and not str(payload.get(field, '')).strip():
            raise HTTPException(status_code=400, detail=f'{field} is required.')
    for field in ('name', 'object'):
        if field in payload:
            value = str(payload.get(field, '')).strip()
            if not value:
                raise HTTPException(status_code=400, detail=f'{field} cannot be blank.')
            rule[field] = value
    if 'min_confidence' in payload or not partial:
        try:
            value = float(payload.get('min_confidence', 0.5))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='min_confidence must be a number.') from exc
        if not 0 <= value <= 1:
            raise HTTPException(status_code=400, detail='min_confidence must be between 0 and 1.')
        rule['min_confidence'] = value
    if 'cooldown_seconds' in payload or not partial:
        try:
            value = int(payload.get('cooldown_seconds', 60))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='cooldown_seconds must be an integer.') from exc
        if value < 0:
            raise HTTPException(status_code=400, detail='cooldown_seconds cannot be negative.')
        rule['cooldown_seconds'] = value
    if 'enabled' in payload or not partial:
        rule['enabled'] = bool(payload.get('enabled', True))
    if 'email_enabled' in payload or not partial:
        rule['email_enabled'] = bool(payload.get('email_enabled', False))
    if 'email_recipients' in payload or not partial:
        raw_recipients = payload.get('email_recipients', [])
        if isinstance(raw_recipients, str):
            recipients = [value.strip() for value in raw_recipients.split(',') if value.strip()]
        elif isinstance(raw_recipients, list):
            recipients = [str(value).strip() for value in raw_recipients if str(value).strip()]
        else:
            raise HTTPException(status_code=400, detail='email_recipients must be a list or comma-separated string.')
        for recipient in recipients:
            if '@' not in recipient:
                raise HTTPException(status_code=400, detail='Email recipients must be valid email addresses.')
        rule['email_recipients'] = recipients
    for field in ('active_start', 'active_end'):
        if field in payload:
            value = payload.get(field) or None
            if value is not None and not re.fullmatch(r'\d{2}:\d{2}', str(value)):
                raise HTTPException(status_code=400, detail=f'{field} must use HH:MM format.')
            rule[field] = value
    return rule


def validate_alert_email_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_email_alert_settings()
    allowed = {'enabled', 'host', 'port', 'username', 'password', 'from_address', 'use_tls', 'use_ssl'}
    updated = {key: current.get(key) for key in allowed if key in current}
    for key, value in payload.items():
        if key in allowed:
            updated[key] = value
    for key in ('enabled', 'use_tls', 'use_ssl'):
        value = updated.get(key, False)
        updated[key] = value.lower() in {'1', 'true', 'yes', 'on'} if isinstance(value, str) else bool(value)
    try:
        updated['port'] = int(updated.get('port') or 587)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='SMTP port must be an integer.') from exc
    if not 1 <= updated['port'] <= 65535:
        raise HTTPException(status_code=400, detail='SMTP port must be between 1 and 65535.')
    for key in ('host', 'username', 'password', 'from_address'):
        updated[key] = str(updated.get(key) or '').strip()
    if updated['enabled'] and not updated['host']:
        raise HTTPException(status_code=400, detail='SMTP host is required when email alerts are enabled.')
    if updated['enabled'] and not updated['from_address']:
        raise HTTPException(status_code=400, detail='From address is required when email alerts are enabled.')
    if updated['from_address'] and '@' not in updated['from_address']:
        raise HTTPException(status_code=400, detail='From address must be a valid email address.')
    if updated['use_ssl']:
        updated['use_tls'] = False
    return updated


def validate_plate_alert_rule(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    rule: dict[str, Any] = {}
    if not partial and not str(payload.get('rule_name', '')).strip():
        raise HTTPException(status_code=400, detail='rule_name is required.')
    if 'rule_name' in payload:
        value = str(payload.get('rule_name') or '').strip()
        if not value:
            raise HTTPException(status_code=400, detail='rule_name cannot be blank.')
        rule['rule_name'] = value
    if 'rule_type' in payload or not partial:
        rule_type = str(payload.get('rule_type', 'plate')).lower()
        if rule_type not in {'plate', 'unknown', 'blacklisted'}:
            raise HTTPException(status_code=400, detail='rule_type must be plate, unknown, or blacklisted.')
        rule['rule_type'] = rule_type
    if 'plate_pattern' in payload or not partial:
        pattern = normalize_plate(payload.get('plate_pattern') or '')
        if rule.get('rule_type', payload.get('rule_type')) == 'plate' and not pattern:
            raise HTTPException(status_code=400, detail='plate_pattern is required for specific plate rules.')
        rule['plate_pattern'] = pattern or None
    if 'enabled' in payload or not partial:
        rule['enabled'] = _bool_value(payload.get('enabled', True))
    if 'cooldown_seconds' in payload or not partial:
        rule['cooldown_seconds'] = _int_field(payload, 'cooldown_seconds', 60, 0, 86400)
    return rule


def _bool_value(value: Any) -> bool:
    return value.lower() in {'1', 'true', 'yes', 'on'} if isinstance(value, str) else bool(value)


def _int_field(payload: dict[str, Any], field: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(payload.get(field, default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f'{field} must be an integer.') from exc
    if value < minimum or value > maximum:
        raise HTTPException(status_code=400, detail=f'{field} must be between {minimum} and {maximum}.')
    return value


def validate_camera_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_camera_config()
    updated = {key: current.get(key) for key in ('backend', 'device', 'width', 'height', 'fps', 'flip') if key in current}
    updated.update({key: payload[key] for key in ('backend', 'device', 'flip') if key in payload})
    backend = str(updated.get('backend', 'mock')).lower()
    if backend != 'mock':
        raise HTTPException(status_code=400, detail='Only the mock camera backend is currently available.')
    updated['backend'] = backend
    updated['device'] = payload.get('device', current.get('device', 0))
    updated['width'] = _int_field({**current, **payload}, 'width', 1280, 160, 7680)
    updated['height'] = _int_field({**current, **payload}, 'height', 720, 120, 4320)
    updated['fps'] = _int_field({**current, **payload}, 'fps', 15, 1, 120)
    flip = str(updated.get('flip', 'none')).lower()
    if flip not in {'none', 'horizontal', 'vertical', 'both'}:
        raise HTTPException(status_code=400, detail='flip must be none, horizontal, vertical, or both.')
    updated['flip'] = flip
    return updated


def validate_anpr_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_anpr_config()
    merged = {**current, **payload}
    backend = str(merged.get('backend', 'mock')).lower()
    if backend not in {'mock', 'paddleocr', 'easyocr'}:
        raise HTTPException(status_code=400, detail='ANPR backend must be mock, paddleocr, or easyocr.')
    try:
        min_confidence = float(merged.get('min_confidence', 0.75))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='ANPR min_confidence must be a number.') from exc
    if not 0 <= min_confidence <= 1:
        raise HTTPException(status_code=400, detail='ANPR min_confidence must be between 0 and 1.')
    raw_labels = merged.get('vehicle_labels', [])
    if isinstance(raw_labels, str):
        vehicle_labels = [label.strip().lower() for label in raw_labels.split(',') if label.strip()]
    elif isinstance(raw_labels, list):
        vehicle_labels = [str(label).strip().lower() for label in raw_labels if str(label).strip()]
    else:
        raise HTTPException(status_code=400, detail='vehicle_labels must be a list or comma-separated string.')
    return {
        'enabled': _bool_value(merged.get('enabled', True)),
        'backend': backend,
        'min_confidence': min_confidence,
        'vehicle_labels': vehicle_labels or ['car', 'truck', 'bus', 'motorcycle'],
    }


def validate_recording_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_recording_config()
    merged = {**current, **payload}
    mode = str(merged.get('mode', 'motion')).lower()
    if mode not in {'off', 'continuous', 'motion', 'human', 'objects'}:
        raise HTTPException(status_code=400, detail='Recording mode must be off, continuous, motion, human, or objects.')
    fmt = str(merged.get('format', 'avi')).strip().lstrip('.').lower() or 'avi'
    if not re.fullmatch(r'[a-z0-9]{2,8}', fmt):
        raise HTTPException(status_code=400, detail='Recording format must be a short file extension.')
    raw_objects = merged.get('record_on_objects', [])
    if isinstance(raw_objects, str):
        object_labels = [label.strip().lower() for label in raw_objects.split(',') if label.strip()]
    elif isinstance(raw_objects, list):
        object_labels = [str(label).strip().lower() for label in raw_objects if str(label).strip()]
    else:
        raise HTTPException(status_code=400, detail='record_on_objects must be a list or comma-separated string.')
    return {
        'enabled': _bool_value(merged.get('enabled', True)),
        'mode': mode,
        'continuous': _bool_value(merged.get('continuous', mode == 'continuous')),
        'record_on_motion': _bool_value(merged.get('record_on_motion', True)),
        'record_on_human': _bool_value(merged.get('record_on_human', True)),
        'record_on_objects': object_labels,
        'pre_event_seconds': _int_field(merged, 'pre_event_seconds', 5, 0, 300),
        'post_event_seconds': _int_field(merged, 'post_event_seconds', 10, 0, 300),
        'max_clip_seconds': _int_field(merged, 'max_clip_seconds', 60, 1, 3600),
        'format': fmt,
        'retention_days': _int_field(merged, 'retention_days', 14, 1, 3650),
        'max_storage_gb': _int_field(merged, 'max_storage_gb', 20, 1, 100000),
        'auto_purge_enabled': _bool_value(merged.get('auto_purge_enabled', True)),
    }


def validate_storage_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_storage_config()
    updated = {key: str(current.get(key) or '') for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir', 'plates_dir', 'database')}
    for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir', 'plates_dir'):
        if key in payload:
            value = str(payload.get(key) or '').strip()
            if not value:
                raise HTTPException(status_code=400, detail=f'{key} cannot be blank.')
            updated[key] = value
    updated['database'] = str(config.get('storage', {}).get('database') or updated.get('database') or 'data/daygle_ai_camera.sqlite3')
    return updated


def validate_auth_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_auth_config()
    merged = {**current, **payload}
    try:
        session_timeout_hours = float(merged.get('session_timeout_hours', 12))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='session_timeout_hours must be a number.') from exc
    if session_timeout_hours < 0.25 or session_timeout_hours > 720:
        raise HTTPException(status_code=400, detail='session_timeout_hours must be between 0.25 and 720.')
    return {
        'session_timeout_hours': session_timeout_hours,
        'max_login_attempts': _int_field(merged, 'max_login_attempts', 5, 1, 100),
        'lockout_minutes': _int_field(merged, 'lockout_minutes', 15, 1, 1440),
    }


def apply_camera_settings(settings: dict[str, Any]) -> None:
    global camera, camera_config
    camera_config = settings
    camera = MockCamera(width=int(settings['width']), height=int(settings['height']), fps=int(settings['fps']))


def apply_storage_and_recording_settings() -> None:
    global storage, recording_service
    storage = Storage({**config, 'storage': effective_storage_config()})
    recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})


def apply_anpr_settings() -> None:
    global anpr_pipeline
    anpr_pipeline = AnprPipeline(effective_anpr_config())


@app.get('/api/settings/ai')
def get_ai_settings():
    return detector_status(effective_ai_config())


def reload_detector(ai_settings: dict[str, Any]) -> tuple[bool, str | None]:
    global detector, mock_detector, last_detector_error
    previous_detector = detector
    candidate = create_detector(ai_settings)
    candidate_error = getattr(candidate, 'unavailable_reason', None)
    if ai_settings['backend'] == 'onnx' and not getattr(candidate, 'available', False):
        detector = previous_detector
        last_detector_error = candidate_error or 'Failed to load ONNX detector.'
        log_detector_initialization('reload_failed')
        return False, last_detector_error
    detector = candidate
    last_detector_error = candidate_error
    if ai_settings['backend'] == 'mock':
        mock_detector = candidate  # type: ignore[assignment]
    else:
        mock_detector = MockDetector(ai_settings.get('categories', config.get('ai', {}).get('categories', [])), float(ai_settings.get('confidence', 0.45)))
    log_detector_initialization('reload')
    return True, last_detector_error


@app.put('/api/settings/ai')
async def update_ai_settings(request: Request):
    payload = await request.json()
    new_settings = validate_ai_settings(payload)
    database.set_setting('ai', new_settings, utc_now())
    reloaded, error = reload_detector(new_settings)
    response = detector_status(new_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
    return response


@app.post('/api/settings/ai/reload')
def reload_ai_detector():
    ai_settings = effective_ai_config()
    reloaded, error = reload_detector(ai_settings)
    response = detector_status(ai_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
    if not reloaded:
        return JSONResponse(response, status_code=400)
    return response


@app.post('/api/settings/ai/check-model')
def check_ai_model():
    return ai_status_payload(effective_ai_config())


@app.post('/api/settings/ai/download-yolov8n')
def download_yolov8n_model():
    ai_settings = effective_ai_config()
    destination = BASE_DIR / 'models' / 'yolov8n.onnx'
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent, suffix='.tmp') as handle:
            temp_path = Path(handle.name)
        urlretrieve(YOLOV8N_ONNX_URL, temp_path)  # noqa: S310 - fixed upstream YOLOv8n model URL
        if temp_path.stat().st_size <= 0:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError('Downloaded model file is empty.')
        temp_path.replace(destination)
    except Exception as exc:  # pragma: no cover - network and filesystem dependent
        if 'temp_path' in locals():
            temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f'Failed to download YOLOv8n ONNX model: {exc}') from exc

    updated = validate_ai_settings({**ai_settings, 'model_path': str(destination.relative_to(BASE_DIR))})
    database.set_setting('ai', updated, utc_now())
    reloaded, error = reload_detector(updated)
    return {
        'ok': True,
        'message': f'Downloaded YOLOv8n ONNX to {destination.relative_to(BASE_DIR)}.',
        'model_path': str(destination.relative_to(BASE_DIR)),
        'bytes': destination.stat().st_size,
        'reload_succeeded': reloaded,
        'reload_error': error,
        'status': ai_status_payload(updated),
    }


@app.post('/api/settings/ai/test-detector')
def test_ai_detector():
    ai_settings = effective_ai_config()
    ai_state = ai_status_payload(ai_settings)
    if ai_settings.get('backend') == 'onnx' and not ai_state['detector_loaded']:
        raise HTTPException(status_code=400, detail=ai_state['last_detector_error'] or 'ONNX detector is not loaded.')
    if not hasattr(detector, 'detect_image'):
        raise HTTPException(status_code=400, detail='Configured detector cannot run image inference.')
    try:
        detections = detector.detect_image(ONE_PIXEL_PNG)
    except DetectorUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'backend_used': ai_state['configured_backend'], 'detections': detections, 'status': ai_state}


@app.get('/api/settings/alerts')
def get_alert_rules():
    return {'rules': database.list_alert_rules(), 'available_labels': available_labels()}


@app.get('/api/settings/alert-email')
def get_alert_email_settings():
    return effective_email_alert_settings()


@app.put('/api/settings/alert-email')
async def update_alert_email_settings(request: Request):
    payload = await request.json()
    settings = validate_alert_email_settings(payload)
    return database.set_setting('alert_email', settings, utc_now())


@app.get('/api/settings/system')
def get_system_settings():
    return {
        'camera': effective_camera_config(),
        'anpr': effective_anpr_config(),
        'recording': effective_recording_config(),
        'storage': effective_storage_config(),
        'auth': {
            'session_timeout_hours': effective_auth_config().get('session_timeout_hours'),
            'max_login_attempts': effective_auth_config().get('max_login_attempts'),
            'lockout_minutes': effective_auth_config().get('lockout_minutes'),
        },
        'bootstrap': {
            'database': config.get('storage', {}).get('database'),
            'auth_enabled': auth_enabled,
            'cookie_name': SESSION_COOKIE_NAME,
            'server': config.get('server', {}),
        },
    }


@app.put('/api/settings/system/camera')
async def update_camera_settings(request: Request):
    settings = validate_camera_settings(await request.json())
    database.set_setting('camera', settings, utc_now())
    apply_camera_settings(settings)
    return settings


@app.get('/api/settings/anpr')
def get_anpr_settings():
    return effective_anpr_config()


@app.put('/api/settings/anpr')
async def update_anpr_settings(request: Request):
    settings = validate_anpr_settings(await request.json())
    database.set_setting('anpr', settings, utc_now())
    apply_anpr_settings()
    return settings


@app.put('/api/settings/system/anpr')
async def update_system_anpr_settings(request: Request):
    return await update_anpr_settings(request)


@app.put('/api/settings/system/recording')
async def update_recording_settings(request: Request):
    settings = validate_recording_settings(await request.json())
    database.set_setting('recording', settings, utc_now())
    apply_storage_and_recording_settings()
    return settings


@app.put('/api/settings/system/storage')
async def update_storage_settings(request: Request):
    settings = validate_storage_settings(await request.json())
    database.set_setting('storage', settings, utc_now())
    apply_storage_and_recording_settings()
    return settings


@app.put('/api/settings/system/auth')
async def update_auth_settings(request: Request):
    settings = validate_auth_settings(await request.json())
    database.set_setting('auth', settings, utc_now())
    auth.apply_config(settings)
    return settings


@app.post('/api/settings/alerts')
async def create_alert_rule(request: Request):
    payload = await request.json()
    rule = database.create_alert_rule(validate_alert_rule(payload), utc_now())
    alerts.rules = effective_alert_rules()
    return rule


@app.put('/api/settings/alerts/{rule_id}')
async def update_alert_rule(rule_id: int, request: Request):
    payload = await request.json()
    rule = database.update_alert_rule(rule_id, validate_alert_rule(payload, partial=True), utc_now())
    if rule is None:
        raise HTTPException(status_code=404, detail='Alert rule not found')
    alerts.rules = effective_alert_rules()
    return rule


@app.delete('/api/settings/alerts/{rule_id}')
def delete_alert_rule(rule_id: int):
    if not database.delete_alert_rule(rule_id):
        raise HTTPException(status_code=404, detail='Alert rule not found')
    alerts.rules = effective_alert_rules()
    return {'ok': True}


if __name__ == '__main__':
    import uvicorn

    server_config = config.get('server', {})
    uvicorn.run(
        'app.main:app',
        host=server_config.get('host', '0.0.0.0'),
        port=int(server_config.get('port', 8080)),
        reload=False,
    )
