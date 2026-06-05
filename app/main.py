from __future__ import annotations

import copy
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.alerts import AlertEngine
from app.auth import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, AuthError, AuthService
from app.database import EventDatabase
from app.detector import DetectorUnavailableError, MockDetector, create_detector, load_labels
from app.mock_camera import MockCamera
from app.settings import load_settings
from app.storage import Storage

logger = logging.getLogger('daygle.ai')

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

camera_config = config.get('camera', {})
camera = MockCamera(
    width=int(camera_config.get('width', 1280)),
    height=int(camera_config.get('height', 720)),
    fps=int(camera_config.get('fps', 15)),
)

database = EventDatabase(config['storage']['database'])
storage = Storage(config)
auth = AuthService(config['storage']['database'], auth_config)
SESSION_COOKIE_NAME = str(auth_config.get('cookie_name', SESSION_COOKIE))

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def effective_ai_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('ai', {}))
    override = database.get_setting('ai')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_alert_rules() -> list[dict[str, Any]]:
    return database.list_alert_rules()


database.seed_alert_rules(config.get('alerts', {}).get('rules', []), utc_now())
detector = create_detector(effective_ai_config())
mock_detector = MockDetector(effective_ai_config().get('categories', []), float(effective_ai_config().get('confidence', 0.45)))
alerts = AlertEngine(effective_alert_rules())

def ai_status_payload(ai_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = ai_settings or effective_ai_config()
    active_backend = getattr(detector, 'backend', 'unknown')
    configured_backend = str(settings.get('backend', 'mock')).lower()
    unavailable_reason = getattr(detector, 'unavailable_reason', None)
    model_loaded = bool(active_backend == 'onnx' and getattr(detector, 'available', False))
    inference_available = bool(getattr(detector, 'available', True))
    if configured_backend == 'onnx' and not model_loaded:
        mode = 'MODEL FAILED'
    elif active_backend == 'onnx' and model_loaded:
        mode = 'ONNX ACTIVE'
    else:
        mode = 'MOCK MODE'
    return {
        'active_backend': active_backend,
        'configured_backend': configured_backend,
        'mode': mode,
        'model_loaded': model_loaded,
        'model_path': str(settings.get('model_path') or ''),
        'labels_path': str(settings.get('labels_path') or ''),
        'inference_available': inference_available,
        'error': unavailable_reason,
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
ADMIN_PATHS = {'/settings', '/users'}
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

    admin_required = path in ADMIN_PATHS or path.startswith('/api/users') or path.startswith('/api/settings/ai') or (
        path.startswith('/api/settings/alerts') and request.method in MUTATING_METHODS
    )
    if admin_required and session['user']['role'] != 'admin':
        return JSONResponse({'detail': 'Admin access required'}, status_code=403)

    if (path.startswith('/api/') or path == '/logout') and request.method in MUTATING_METHODS:
        csrf_header = request.headers.get(CSRF_HEADER)
        if not csrf_header or csrf_header != session['csrf_token']:
            return JSONResponse({'detail': 'CSRF token missing or invalid'}, status_code=403)

    return await call_next(request)


def set_session_cookie(response: Response, request: Request, token: str, expires_at: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
        expires=expires_at,
        max_age=int(float(auth_config.get('session_timeout_hours', 12)) * 3600),
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
def dashboard_aliases():
    return root()


@app.get('/settings')
def settings_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Settings · Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell"><header class="hero"><div><p class="eyebrow">Administration</p><h1>AI & Alert Settings</h1><p class="muted">Update runtime AI settings and alert rules stored in SQLite.</p></div><a class="button-link" href="/">Dashboard</a></header><section class="card"><h2>AI settings</h2><div id="settingsMessage" class="muted"></div><form id="aiSettingsForm" class="form-grid"><label><span>AI enabled</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><label><span>Backend</span><select name="backend"><option value="mock">mock</option><option value="onnx">onnx</option></select></label><label><span>Confidence</span><input name="confidence" type="number" min="0" max="1" step="0.01" /></label><label><span>IOU threshold</span><input name="iou_threshold" type="number" min="0" max="1" step="0.01" /></label><label><span>Input size</span><input name="input_size" type="number" min="32" max="2048" step="32" /></label><label><span>Model path</span><input name="model_path" /></label><label><span>Labels path</span><input name="labels_path" /></label><button type="submit">Save AI settings</button></form></section><section class="card"><h2>Alert rules</h2><form id="alertRuleForm" class="form-grid"><input type="hidden" name="id" /><input name="name" placeholder="Rule name" required /><input name="object" list="labelOptions" placeholder="Object label" required /><datalist id="labelOptions"></datalist><input name="min_confidence" type="number" min="0" max="1" step="0.01" placeholder="Min confidence" value="0.6" /><input name="cooldown_seconds" type="number" min="0" step="1" placeholder="Cooldown seconds" value="60" /><label><span>Enabled</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><input name="active_start" type="time" /><input name="active_end" type="time" /><button type="submit">Save alert rule</button><button id="cancelEditRule" class="secondary" type="button">Cancel edit</button></form><div id="alertRules" class="list"></div></section></main><script src="/static/settings.js"></script></body></html>""")


@app.get('/users')
def users_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Users · Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell"><header class="hero"><div><p class="eyebrow">Administration</p><h1>User Management</h1><p class="muted">Create users, change roles, disable accounts, and reset passwords.</p></div><a class="button-link" href="/">Dashboard</a></header><section class="card"><div id="userMessage" class="muted"></div><div id="users" class="list"></div></section><section class="card"><h2>Create user</h2><form id="createUserForm" class="form-grid"><input name="username" placeholder="Username" required /><select name="role"><option value="viewer">viewer</option><option value="admin">admin</option></select><input name="password" type="password" placeholder="Temporary password" required /><button>Create</button></form></section></main><script src="/static/users.js"></script></body></html>""")


@app.get('/api/auth/me')
def me(request: Request):
    session = require_session(request)
    return {'user': session['user'], 'csrf_token': session['csrf_token'], 'expires_at': session['expires_at']}


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

    event_id = database.add_event(
        created_at=datetime.now(timezone.utc).isoformat(),
        source='mock-camera',
        snapshot_path=snapshot_path,
        detections=detections,
        alert_triggered=bool(triggered),
    )

    for alert in triggered:
        database.add_alert(
            created_at=datetime.now(timezone.utc).isoformat(),
            rule_name=alert['rule_name'],
            event_id=event_id,
            label=alert['label'],
            confidence=alert['confidence'],
            message=alert['message'],
        )

    return {
        'created': True,
        'event_id': event_id,
        'detections': detections,
        'alerts': triggered,
    }


@app.post('/api/detect/test-image')
async def detect_test_image(request: Request):
    image_bytes, filename, content_type = await _read_uploaded_image(request)
    if not image_bytes:
        raise HTTPException(status_code=400, detail='Uploaded image is empty')

    try:
        detections = detector.detect_image(image_bytes)
    except DetectorUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    snapshot_path = storage.save_image_snapshot(image_bytes, filename)
    alerts.rules = effective_alert_rules()
    triggered = alerts.process(detections)
    event_id = database.add_event(
        created_at=datetime.now(timezone.utc).isoformat(),
        source='test-image',
        snapshot_path=snapshot_path,
        detections=detections,
        alert_triggered=bool(triggered),
        metadata={
            'filename': filename,
            'content_type': content_type,
            'ai_backend': ai_status_payload()['active_backend'],
        },
    )

    for alert in triggered:
        database.add_alert(
            created_at=datetime.now(timezone.utc).isoformat(),
            rule_name=alert['rule_name'],
            event_id=event_id,
            label=alert['label'],
            confidence=alert['confidence'],
            message=alert['message'],
        )

    return {
        'created': True,
        'event_id': event_id,
        'detections': detections,
        'alerts': triggered,
        'snapshot_path': snapshot_path,
        'ai_backend': ai_status_payload()['active_backend'],
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
        'camera': config.get('camera', {}),
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
        'alerts': config.get('alerts', {}),
        'auth': {
            'enabled': auth_enabled,
            'session_timeout_hours': auth_config.get('session_timeout_hours'),
            'max_login_attempts': auth_config.get('max_login_attempts'),
            'lockout_minutes': auth_config.get('lockout_minutes'),
        },
        'storage': {
            'database': config.get('storage', {}).get('database'),
            'snapshots_dir': config.get('storage', {}).get('snapshots_dir'),
        },
    }


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


def validate_ai_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_ai_config()
    allowed = {'enabled', 'backend', 'confidence', 'iou_threshold', 'input_size', 'model_path', 'labels_path'}
    updated = {key: current.get(key) for key in allowed if key in current}
    for key, value in payload.items():
        if key in allowed:
            updated[key] = value
    updated['enabled'] = bool(updated.get('enabled', True))
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
        'mode': ai_status['mode'],
        'available': ai_status['inference_available'],
        'model_loaded': ai_status['model_loaded'],
        'error': ai_status['error'],
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
    for field in ('active_start', 'active_end'):
        if field in payload:
            value = payload.get(field) or None
            if value is not None and not re.fullmatch(r'\d{2}:\d{2}', str(value)):
                raise HTTPException(status_code=400, detail=f'{field} must use HH:MM format.')
            rule[field] = value
    return rule


@app.get('/api/settings/ai')
def get_ai_settings():
    return detector_status(effective_ai_config())


@app.put('/api/settings/ai')
async def update_ai_settings(request: Request):
    global detector, mock_detector
    payload = await request.json()
    new_settings = validate_ai_settings(payload)
    previous_detector = detector
    candidate = create_detector(new_settings)
    if new_settings['backend'] == 'onnx' and not getattr(candidate, 'available', False):
        detector = previous_detector
        raise HTTPException(status_code=400, detail=getattr(candidate, 'unavailable_reason', 'Failed to load ONNX detector.'))
    detector = candidate
    log_detector_initialization('settings_reload')
    mock_detector = MockDetector(new_settings.get('categories', config.get('ai', {}).get('categories', [])), float(new_settings.get('confidence', 0.45)))
    database.set_setting('ai', new_settings, utc_now())
    return detector_status(new_settings)


@app.get('/api/settings/alerts')
def get_alert_rules():
    return {'rules': database.list_alert_rules(), 'available_labels': available_labels()}


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
