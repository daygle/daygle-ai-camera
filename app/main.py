from __future__ import annotations

import secrets
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
from app.detector import MockDetector
from app.mock_camera import MockCamera
from app.settings import load_settings
from app.storage import Storage

config = load_settings()

auth_config = config.get('auth', {})
auth_enabled = bool(auth_config.get('enabled', True))

app = FastAPI(title='Daygle AI Camera')

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

detector = MockDetector(config['ai']['categories'], config['ai']['confidence'])
alerts = AlertEngine(config['alerts']['rules'])
database = EventDatabase(config['storage']['database'])
storage = Storage(config)
auth = AuthService(config['storage']['database'], auth_config)

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

    session = auth.get_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        if path.startswith('/api/'):
            return JSONResponse({'detail': 'Authentication required'}, status_code=401)
        return RedirectResponse('/login', status_code=303)

    request.state.session = session
    request.state.user = session['user']

    if (path in ADMIN_PATHS or any(path.startswith(prefix) for prefix in ADMIN_API_PREFIXES)) and session['user']['role'] != 'admin':
        return JSONResponse({'detail': 'Admin access required'}, status_code=403)

    if path.startswith('/api/') and request.method in MUTATING_METHODS:
        csrf_header = request.headers.get(CSRF_HEADER)
        if not csrf_header or csrf_header != session['csrf_token']:
            return JSONResponse({'detail': 'CSRF token missing or invalid'}, status_code=403)

    return await call_next(request)


def set_session_cookie(response: Response, request: Request, token: str, expires_at: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
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
    response.delete_cookie(SESSION_COOKIE)
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
    if auth_enabled and auth.users_exist() and auth.get_session(request.cookies.get(SESSION_COOKIE)):
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
    auth.delete_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse('/login', status_code=303)
    clear_auth_cookies(response)
    return response


@app.post('/logout')
def logout_post(request: Request):
    auth.delete_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({'ok': True})
    clear_auth_cookies(response)
    return response


@app.get('/')
def root():
    index_path = web_dir / 'index.html'
    if index_path.exists():
        return FileResponse(index_path)
    return {'application': 'Daygle AI Camera', 'status': 'running'}


@app.get('/events')
@app.get('/alerts')
@app.get('/search')
@app.get('/settings')
def dashboard_aliases():
    return root()


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
    return {
        'status': 'online',
        'mode': camera_config.get('backend', 'mock'),
        'ai_backend': config.get('ai', {}).get('backend', 'mock'),
        'frame_number': frame['frame_number'],
        'uptime_seconds': frame['uptime_seconds'],
        'resolution': {'width': frame['width'], 'height': frame['height']},
    }


@app.post('/api/mock/detect')
def generate_detection(force: bool = True):
    frame = camera.get_frame()
    detections = detector.detect(frame['frame_number'], force=force)

    if not detections:
        return {'created': False, 'message': 'No detections generated'}

    snapshot_path = storage.save_mock_snapshot(frame, detections)
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
    return {
        'server': {'host': config.get('server', {}).get('host'), 'port': config.get('server', {}).get('port')},
        'camera': config.get('camera', {}),
        'ai': {
            'enabled': config.get('ai', {}).get('enabled'),
            'backend': config.get('ai', {}).get('backend'),
            'confidence': config.get('ai', {}).get('confidence'),
            'categories': config.get('ai', {}).get('categories', []),
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


if __name__ == '__main__':
    import uvicorn

    server_config = config.get('server', {})
    uvicorn.run(
        'app.main:app',
        host=server_config.get('host', '0.0.0.0'),
        port=int(server_config.get('port', 8080)),
        reload=False,
    )
