"""Web-page APIRouter.

Page-render routes for the Daygle AI Camera UI. All state is accessed via
Depends() providers in ``app.deps``.

Routes:
- GET  /              -- root
- GET  /favicon.ico   -- favicon
- GET  /login         -- login_page
- GET  /setup         -- setup_page
- GET  /live          -- live_page
- GET  /zones         -- zones_page
- GET  /alerts        -- alerts_page
- GET  /detection/sounds -- sounds_page (/sounds redirects)
- GET  /detection     -- objects_page (/objects redirects)
- GET  /detection/faces -- face_recognition_page (/face-recognition redirects)
- GET  /cameras       -- cameras_page
- GET  /events        -- events_page
- GET  /search        -- dashboard_aliases
- GET  /recordings    -- recordings_page
- GET  /snapshots     -- snapshots_page
- GET  /recordings/timeline -- recordings_timeline_page
- GET  /models        -- models_page (/onnx redirects)
- GET  /models/faces  -- face_models_page (/arcface redirects)
- GET  /models/cameras -- camera_models_page (/camera-models redirects)
- GET  /models/sound  -- yamnet_tflite_page (/yamnet-tflite, /yamnet redirect)
- GET  /models/settings -- model_settings_page
- GET  /ai            -- ai_settings_page (308 redirect)
- GET  /profile       -- profile_page
- GET  /settings      -- system_settings_page
- GET  /users         -- users_page
- GET  /audit         -- audit_page
- GET  /camera-log    -- camera_log_page
- GET  /application-log -- application_log_page
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.auth_helpers import _session_cookie_name, csrf_token_response
from app.auth_gates import require_admin
from app.deps import get_auth, get_auth_enabled, get_web_dir

router = APIRouter()

# Paths that must never be honoured as a ``returnTo`` target, even when the
# caller forwards them via ``?returnTo=...``. Centralised so /login, /setup,
# /logout never loop, and so authenticated admin paths can be guarded if a
# pre-setup request ever lands on a non-admin account down the line.
_LOGIN_DISALLOWED_REDIRECT_PREFIXES = (
    '/login', '/logout', '/setup', '/api/', '/static/',
)


def _safe_return_to(raw: str | None) -> str:
    """Validate a ``returnTo`` query value and return a safe redirect target.

    Accepts ONLY same-origin relative paths whose first character is a
    single ``/`` (NOT ``//`` - protocol-relative URLs would let a crafted
    ``?returnTo=//evil.com`` hijack the redirect). Anything else - absolute
    URLs, non-slash prefixes, paths that point back at /login/setup/logout
    or to the JSON API or static-asset routes - collapses to ``/`` so a
    typo or attacker tweak can never strand the user in an infinite auth
    loop or worse.
    """
    text = str(raw or '')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return '/'
    candidate = text.strip()
    if (
        not candidate
        or not candidate.startswith('/')
        or candidate.startswith('//')
        # Browsers can treat backslashes as URL separators; reject them rather
        # than allowing ``\\\\evil.example`` to become an external target.
        or '\\' in candidate
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate)
    ):
        return '/'
    if any(candidate == prefix.rstrip('/') or candidate.startswith(prefix) for prefix in _LOGIN_DISALLOWED_REDIRECT_PREFIXES):
        return '/'
    # Rebuild the target with a constant origin-relative prefix. Besides making
    # the invariant obvious to readers, this is the form CodeQL recognizes as
    # a safe relative redirect: user input can only supply the path suffix.
    return '/' + candidate[1:]


@router.get('/')
def root(web_dir: Path = Depends(get_web_dir)):
    # Live footage is the home page (the old dashboard's counts live on
    # Events and its system metrics on Admin > System).
    live_path = web_dir / 'live.html'
    if live_path.exists():
        return FileResponse(live_path)
    return {'application': 'Daygle AI Camera', 'status': 'running'}


def _page(web_dir: Path, filename: str):
    """Serve ``web/<filename>``, falling back to the home page."""
    page_path = web_dir / filename
    if page_path.exists():
        return FileResponse(page_path)
    return root(web_dir=web_dir)


@router.get('/favicon.ico')
def favicon(web_dir: Path = Depends(get_web_dir)):
    favicon_path = web_dir / 'favicon.svg'
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type='image/svg+xml')
    raise HTTPException(status_code=404, detail='Favicon not found')


def login_page(
    request: Request,
    error: str | None = None,
    return_to: str | None = None,
    *,
    auth,
    auth_enabled: bool,
):
    """Render the login page.

    ``auth`` and ``auth_enabled`` must be resolved by the caller. Route
    handler at :func:`_login_page_route` injects them via ``Depends()``.
    """
    if auth_enabled and auth.users_exist() and auth.get_session(request.cookies.get(_session_cookie_name())):
        # If the caller was bounced away from a real page (e.g. session timed
        # out while the tab was idle and api() triggered handleSessionLoss),
        # honour their intended destination rather than dumping them at /.
        safe_return = _safe_return_to(return_to)
        return RedirectResponse(safe_return or '/', status_code=303)
    safe_return = _safe_return_to(return_to)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return_field = (
        f'  <input type="hidden" name="return_to" value="{escape(safe_return)}" />\n'
        if safe_return not in ('', '/')
        else ''
    )
    return csrf_token_response(
        request,
        'Login',
        '\n<h1>Sign In</h1><p class="muted">Enter your Daygle AI Camera credentials.</p>'
        f'{error_html}\n<form class="form-stack" method="post" action="/login">\n'
        '  <input type="hidden" name="csrf_token" value="{csrf}" />\n'
        f'{return_field}'
        '  <label>Username<input name="username" autocomplete="username" required /></label>\n'
        '  <label>Password<input name="password" type="password" autocomplete="current-password" required /></label>\n'
        '  <button class="primary" type="submit">Sign In</button>\n'
        '</form>',
    )


def setup_page(
    request: Request,
    error: str | None = None,
    *,
    auth,
    auth_enabled: bool,
):
    """Render the setup page.

    ``auth`` and ``auth_enabled`` must be resolved by the caller. Route
    handler at :func:`_setup_page_route` injects them via ``Depends()``.
    """
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


@router.get('/login')
def _login_page_route(
    request: Request,
    error: str | None = None,
    return_to: str | None = None,
    auth=Depends(get_auth),
    auth_enabled: bool = Depends(get_auth_enabled),
):
    """Route handler - injects deps and delegates to :func:`login_page`."""
    return login_page(request, error=error, return_to=return_to or request.query_params.get('returnTo'), auth=auth, auth_enabled=auth_enabled)


@router.get('/setup')
def _setup_page_route(
    request: Request,
    auth=Depends(get_auth),
    auth_enabled: bool = Depends(get_auth_enabled),
):
    """Route handler - injects deps and delegates to :func:`setup_page`."""
    return setup_page(request, auth=auth, auth_enabled=auth_enabled)


@router.get('/live')
def live_page():
    # Live is now the home page; keep old bookmarks working.
    return RedirectResponse('/', status_code=308)


@router.get('/system')
def system_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    # Admin > System > Health: CPU, memory, GPU and storage at a glance.
    require_admin(request)
    return _page(web_dir, 'system.html')


@router.get('/zones')
def zones_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'zones.html')


@router.get('/alerts')
def alerts_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'alerts.html')


@router.get('/detection/sounds')
def sounds_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'sounds.html')


@router.get('/sounds')
def legacy_sounds_page():
    return RedirectResponse('/detection/sounds', status_code=308)


@router.get('/detection')
def objects_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'objects.html')


@router.get('/objects')
def legacy_objects_page():
    return RedirectResponse('/detection', status_code=308)


@router.get('/cameras')
def cameras_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'cameras.html')


@router.get('/events')
def events_page(web_dir: Path = Depends(get_web_dir)):
    # Granular detection events (one row per occurrence), the single activity
    # feed. An alert is just a property of an event (shown as an indicator on
    # the row), so there is no separate alerts page. Served to any
    # authenticated user like /recordings; the middleware enforces the session
    # and /api/events applies per-user recording scoping.
    return _page(web_dir, 'events.html')


@router.get('/search')
def dashboard_aliases():
    # Footage search lives on the Events page.
    return RedirectResponse('/events', status_code=308)


@router.get('/recordings')
def recordings_page(web_dir: Path = Depends(get_web_dir)):
    return _page(web_dir, 'recordings.html')


@router.get('/snapshots')
def snapshots_page(web_dir: Path = Depends(get_web_dir)):
    # The Snapshots library: every event that captured a frame, served to any
    # authenticated user like /recordings and /events. The middleware enforces
    # the session and /api/snapshots applies per-user recording scoping.
    return _page(web_dir, 'snapshots.html')


@router.get('/recordings/timeline')
def recordings_timeline_page(web_dir: Path = Depends(get_web_dir)):
    return _page(web_dir, 'timeline.html')


@router.get('/recordings/{recording_id}')
def recording_playback_page(recording_id: str, web_dir: Path = Depends(get_web_dir)):
    # Dedicated, deep-linkable playback page for a single clip. Declared AFTER
    # ``/recordings/timeline`` so that literal route matches first. A plain
    # ``str`` path param (not an ``:int`` converter) is used deliberately: the
    # route-coverage invariant in tests/test_api_router_split_invariants.py
    # matches a decorator's template string against the compiled route regex,
    # and a converter regex (``[0-9]+``) does not match the literal
    # ``{recording_id:int}`` text. Non-numeric ids are rejected here with a 404
    # so only real clip ids reach the page. Same access level as the list (no
    # require_admin) so viewers can watch. The client reads the id from the URL.
    if not recording_id.isdigit():
        raise HTTPException(status_code=404, detail='Not found')
    return _page(web_dir, 'recordings.html')


# Intelligence > Models: one tab row of sibling pages under /models. The
# pre-/models URLs redirect so bookmarks keep working.
@router.get('/models')
def models_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'models.html')


@router.get('/models/faces')
def face_models_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'arcface.html')


@router.get('/models/cameras')
def camera_models_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'camera-models.html')


@router.get('/models/sound')
def yamnet_tflite_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'yamnet-tflite.html')


@router.get('/models/settings')
def model_settings_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'models-settings.html')


@router.get('/onnx')
def onnx_page():
    return RedirectResponse('/models', status_code=308)


@router.get('/camera-models')
def legacy_camera_models_page():
    return RedirectResponse('/models/cameras', status_code=308)


@router.get('/ai')
def ai_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    # Intelligence > AI: the local vision model (verification, descriptions,
    # search, tag alerts). Previously a legacy redirect to /onnx.
    require_admin(request)
    return _page(web_dir, 'ai.html')


@router.get('/detection/faces')
def face_recognition_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'face-recognition.html')


@router.get('/face-recognition')
def legacy_face_recognition_page():
    return RedirectResponse('/detection/faces', status_code=308)


@router.get('/arcface')
def arcface_page():
    return RedirectResponse('/models/faces', status_code=308)


@router.get('/people')
def people_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    # People enrolment moved into the Face Recognition page's People tab;
    # the hash opens that tab directly via initDaygleTabs' deep-linking.
    return RedirectResponse('/detection/faces#people', status_code=303)


@router.get('/yamnet-tflite')
def legacy_yamnet_tflite_page():
    return RedirectResponse('/models/sound', status_code=308)


@router.get('/yamnet')
def yamnet_page():
    return RedirectResponse('/models/sound', status_code=308)


@router.get('/profile')
def profile_page(web_dir: Path = Depends(get_web_dir)):
    return _page(web_dir, 'profile.html')


@router.get('/settings')
def system_settings_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'settings.html')


@router.get('/users')
def users_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'users.html')


@router.get('/audit')
def audit_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    # Defense-in-depth: the middleware already blocks non-admin users via
    # the ADMIN_PATHS set, but the handler itself also checks so that a
    # future refactor that accidentally drops /audit from ADMIN_PATHS does
    # not silently expose the audit log.
    require_admin(request)
    return _page(web_dir, 'audit.html')


@router.get('/camera-log')
def camera_log_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'camera-log.html')


@router.get('/application-log')
def application_log_page(request: Request, web_dir: Path = Depends(get_web_dir)):
    require_admin(request)
    return _page(web_dir, 'application-log.html')
