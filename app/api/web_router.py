"""Web-page APIRouter.

Page-render routes for the Daygle AI Camera UI. All state is accessed via
direct imports rather than the hybrid ``import app.main as main`` pattern.

Routes:
- GET  /              -- root
- GET  /favicon.ico   -- favicon
- GET  /login         -- login_page
- GET  /setup         -- setup_page
- GET  /live          -- live_page
- GET  /zones         -- zones_page
- GET  /sounds        -- sounds_page
- GET  /cameras       -- cameras_page
- GET  /alerts /events /search -- dashboard_aliases
- GET  /recordings    -- recordings_page
- GET  /recordings/timeline -- recordings_timeline_page
- GET  /onnx          -- onnx_page
- GET  /ai            -- ai_settings_page (308 redirect)
- GET  /yamnet-tflite -- yamnet_tflite_page
- GET  /yamnet        -- yamnet_page (308 redirect)
- GET  /profile       -- profile_page
- GET  /settings      -- system_settings_page
- GET  /users         -- users_page
- GET  /audit         -- audit_page
- GET  /camera-log    -- camera_log_page
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.auth import CSRF_COOKIE, SESSION_COOKIE
from app.auth_helpers import csrf_token_response
from app.config_facades import effective_auth_config
from app.main import auth, auth_enabled, web_dir

router = APIRouter()

def _session_cookie_name() -> str:
    return str(effective_auth_config().get('cookie_name', SESSION_COOKIE))


@router.get('/')
def root():
    index_path = web_dir / 'index.html'
    if index_path.exists():
        return FileResponse(index_path)
    return {'application': 'Daygle AI Camera', 'status': 'running'}


@router.get('/favicon.ico')
def favicon():
    favicon_path = web_dir / 'favicon.svg'
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type='image/svg+xml')
    raise HTTPException(status_code=404, detail='Favicon not found')


@router.get('/login')
def login_page(request: Request, error: str | None = None):
    if auth_enabled and auth.users_exist() and auth.get_session(request.cookies.get(_session_cookie_name())):
        return RedirectResponse('/', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return csrf_token_response(
        request,
        'Login',
        '\n<h1>Sign In</h1><p class="muted">Enter your Daygle AI Camera credentials.</p>'
        f'{error_html}\n<form class="form-stack" method="post" action="/login">\n'
        '  <input type="hidden" name="csrf_token" value="{csrf}" />\n'
        '  <label>Username<input name="username" autocomplete="username" required /></label>\n'
        '  <label>Password<input name="password" type="password" autocomplete="current-password" required /></label>\n'
        '  <button class="primary" type="submit">Sign In</button>\n'
        '</form>',
    )


@router.get('/setup')
def setup_page(request: Request, error: str | None = None):
    if auth_enabled and auth.users_exist():
        return RedirectResponse('/login', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return csrf_token_response(
        request,
        'Initial setup',
        '\n<h1>Create administrator</h1><p class="muted">This one-time setup is disabled after the first user is created.</p>'
        f'{error_html}\n<form class="form-stack" method="post" action="/setup">\n'
        '  <input type="hidden" name="csrf_token" value="{csrf}" />\n'
        '  <label>First name<input name="first_name" autocomplete="given-name" /></label>\n'
        '  <label>Last name<input name="last_name" autocomplete="family-name" /></label>\n'
        '  <label>Email<input name="email" type="email" autocomplete="email" /></label>\n'
        '  <label>Username<input name="username" value="admin" autocomplete="username" required /></label>\n'
        '  <label>Password<input name="password" type="password" autocomplete="new-password" required /></label>\n'
        '  <label>Confirm password<input name="confirm_password" type="password" autocomplete="new-password" required /></label>\n'
        '  <button class="primary" type="submit">Create Admin Account</button>\n'
        '</form>',
    )


@router.get('/live')
def live_page():
    live_path = web_dir / 'live.html'
    if live_path.exists():
        return FileResponse(live_path)
    return root()


@router.get('/zones')
def zones_page():
    zones_path = web_dir / 'zones.html'
    if zones_path.exists():
        return FileResponse(zones_path)
    return root()


@router.get('/sounds')
def sounds_page():
    sounds_path = web_dir / 'sounds.html'
    if sounds_path.exists():
        return FileResponse(sounds_path)
    return root()


@router.get('/cameras')
def cameras_page():
    cameras_path = web_dir / 'cameras.html'
    if cameras_path.exists():
        return FileResponse(cameras_path)
    return root()


@router.get('/alerts')
@router.get('/events')
@router.get('/search')
def dashboard_aliases():
    return root()


@router.get('/recordings')
def recordings_page():
    recordings_path = web_dir / 'recordings.html'
    if recordings_path.exists():
        return FileResponse(recordings_path)
    return root()


@router.get('/recordings/timeline')
def recordings_timeline_page():
    timeline_path = web_dir / 'timeline.html'
    if timeline_path.exists():
        return FileResponse(timeline_path)
    return root()


@router.get('/onnx')
def onnx_page():
    ai_path = web_dir / 'onnx.html'
    if ai_path.exists():
        return FileResponse(ai_path)
    return root()


@router.get('/ai')
def ai_settings_page():
    return RedirectResponse('/onnx', status_code=308)


@router.get('/yamnet-tflite')
def yamnet_tflite_page():
    yamnet_path = web_dir / 'yamnet-tflite.html'
    if yamnet_path.exists():
        return FileResponse(yamnet_path)
    return root()


@router.get('/yamnet')
def yamnet_page():
    return RedirectResponse('/yamnet-tflite', status_code=308)


@router.get('/profile')
def profile_page():
    profile_path = web_dir / 'profile.html'
    if profile_path.exists():
        return FileResponse(profile_path)
    return root()


@router.get('/settings')
def system_settings_page():
    settings_path = web_dir / 'settings.html'
    if settings_path.exists():
        return FileResponse(settings_path)
    return root()


@router.get('/users')
def users_page():
    users_path = web_dir / 'users.html'
    if users_path.exists():
        return FileResponse(users_path)
    return root()


@router.get('/audit')
def audit_page():
    audit_path = web_dir / 'audit.html'
    if audit_path.exists():
        return FileResponse(audit_path)
    return root()


@router.get('/camera-log')
def camera_log_page():
    page_path = web_dir / 'camera-log.html'
    if page_path.exists():
        return FileResponse(page_path)
    return root()
