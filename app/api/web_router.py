"""Web-page APIRouter.

Extracted from ``app/main.py`` (Phase 13 of the hybrid-pattern router split).
Same template as ``app/api/admin_router.py`` (Phase 11) and
``app/api/live_router.py`` (Phase 10): ``import app.main as main`` at module
level, every global / helper read through ``main.<name>`` *inside* handler
bodies.

This file deliberately bundles ALL UI / HTML page routes into a single
router rather than splitting them one-per-file:

- ``GET  /``                       -- ``root``
- ``GET  /favicon.ico``            -- ``favicon``
- ``GET  /login``                  -- ``login_page``  (the sign-in form page)
- ``GET  /setup``                  -- ``setup_page``  (the first-admin wizard page)
- ``GET  /live``                   -- ``live_page``
- ``GET  /zones``                  -- ``zones_page``
- ``GET  /sounds``                 -- ``sounds_page``
- ``GET  /cameras``                -- ``cameras_page``
- ``GET  /alerts`` ``/events`` ``/search``  -- ``dashboard_aliases`` (3 routes)
- ``GET  /recordings``             -- ``recordings_page``
- ``GET  /recordings/timeline``    -- ``recordings_timeline_page``
- ``GET  /onnx``                   -- ``onnx_page``
- ``GET  /ai``                     -- ``ai_settings_page`` (308 redirect to /onnx)
- ``GET  /yamnet-tflite``          -- ``yamnet_tflite_page``
- ``GET  /yamnet``                 -- ``yamnet_page``  (308 redirect to /yamnet-tflite)
- ``GET  /profile``                -- ``profile_page``
- ``GET  /settings``               -- ``system_settings_page``
- ``GET  /users``                  -- ``users_page``
- ``GET  /audit``                  -- ``audit_page``
- ``GET  /camera-log``             -- ``camera_log_page``

The motivation: page-render handlers are nearly identical shims ("if
``<page>.html`` exists, return a ``FileResponse``; otherwise return
``root()``"). Splitting them one-per-file would multiply boilerplate;
bundling them into one ``web_router`` mirrors the Phase-11 admin_router
cleanup-class precedent while keeping the page-rendering contract in a
single place. POST endpoints that drive the auth/setup flows (POST
``/login``, POST ``/setup``, GET/POST ``/logout``) live in a sibling
``auth_router.py`` -- the GET counterparts (login_page, setup_page) stay
here because they're pure HTML renders, not state transitions.

BODY-REWRITE NOTE
Each handler in this file originally referenced module-level state in
``main.py`` via bare names (``web_dir``, ``FileResponse``, ``auth_enabled``,
``auth``, ``SESSION_COOKIE_NAME``, ``csrf_token_response``). After
extraction to this router, those bare names resolve to ZERO attributes
in our namespace -- handlers would NameError at request time. Per
hybrid-pattern uniformity (rule 5 of ``app/api/__init__.py``), each
bare call is rewritten as ``main.<bare>``.

STD-LIB NOTE
``html.escape`` (stdlib) is imported directly at the router top --
it's not app state. ``HTTPException``, ``APIRouter``, ``Request`` stay at
top-level imports (FastAPI builtins reachable via ``fastapi.*``). The
FastAPI *response class* family (``FileResponse``, ``JSONResponse``,
``RedirectResponse``) lives under ``fastapi.responses``, NOT at the
``fastapi`` top-level -- so we do NOT re-import them here. They are
re-exported by ``app.main`` (which has its own
``from fastapi.responses import FileResponse, JSONResponse, RedirectResponse``),
so every bare-name call site must use ``main.FileResponse(...)``,
``main.JSONResponse(...)``, ``main.RedirectResponse(...)`` per the
hybrid-pattern rule 5. This matches the Phase-9 settings_system_router
+ Phase-12 camera_log/update/utility_router idiom exactly -- and the
test harness (``monkeypatch.setattr(main, 'FileResponse', ...)``) still
reaches the same class object via the ``main.`` attribute alias.

DASHBOARD ALIASES NOTE
``dashboard_aliases`` is one function definition reused for 3 routes
(``/alerts``, ``/events``, ``/search``). Each decorator registers the
same handler at a different URL -- FastAPI accepts this and the
rebind stays the same because all 3 routes return ``root()``
(inheriting the dashboard shell). The router preserves this verbatim.

PAGE-FALLBACK NOTE
Every page handler except ``favicon`` and ``ai_settings_page`` +
``yamnet_page`` implements the same if-exists-then-serve-else-fallback
pattern. The fallback target (``root()``) is also extracted to this
router (it's the GET ``/`` handler). When a page HTML is missing, the
router returns the dashboard root -- same behavior as ``main.py``.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.web_dir`` -- module-level Path to the ``web/`` assets directory.
- ``main.FileResponse`` -- the FastAPI re-exported FileResponse class
  (originally imported at the top of ``main.py``); router reaches via
  re-export so monkeypatch.setattr in tests still hits the same class.
- ``main.auth_enabled`` -- bool flag for whether auth flow is active.
- ``main.auth`` -- the AuthService instance with ``.users_exist()`` and
  ``.get_session()`` (used by login_page to short-circuit already-logged-in
  requests to ``/``).
- ``main.SESSION_COOKIE_NAME`` -- the resolved session cookie name
  (login_page reads via ``request.cookies.get(main.SESSION_COOKIE_NAME)``).
- ``main.csrf_token_response`` -- helper that wraps the GET login_page /
  setup_page HTML in a CSRF-cookie-setting response.

Tests go through ``LocalClient.request`` rather than calling
``main.<page_name>`` directly, so no back-compat alias on ``app.main``
is needed. The Phase 7.1 invariant
``tests/test_api_router_split_invariants.py::test_app_api_imports_in_main_are_consumed``
will catch any orphan-import regression if a future refactor drops the
``from app.api.web_router import router as web_router`` rebind line in
``app/main.py``.
"""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, HTTPException, Request

import app.main as main

router = APIRouter()


@router.get('/')
def root():
    index_path = main.web_dir / 'index.html'
    if index_path.exists():
        return main.FileResponse(index_path)
    return {'application': 'Daygle AI Camera', 'status': 'running'}


@router.get('/favicon.ico')
def favicon():
    favicon_path = main.web_dir / 'favicon.svg'
    if favicon_path.exists():
        return main.FileResponse(favicon_path, media_type='image/svg+xml')
    raise HTTPException(status_code=404, detail='Favicon not found')


@router.get('/login')
def login_page(request: Request, error: str | None = None):
    if main.auth_enabled and main.auth.users_exist() and main.auth.get_session(request.cookies.get(main.SESSION_COOKIE_NAME)):
        return main.RedirectResponse('/', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return main.csrf_token_response(
        request,
        'Login',
        '\n<h1>Sign In</h1><p class="muted">Enter your Daygle AI Camera credentials.</p>'
        f'{error_html}\n<form class="form-stack" method="post" action="/login">\n'
        '  <input type="hidden" name="csrf_token" value="{{csrf}}" />\n'
        '  <label>Username<input name="username" autocomplete="username" required /></label>\n'
        '  <label>Password<input name="password" type="password" autocomplete="current-password" required /></label>\n'
        '  <button class="primary" type="submit">Sign In</button>\n'
        '</form>',
    )


@router.get('/setup')
def setup_page(request: Request, error: str | None = None):
    if main.auth_enabled and main.auth.users_exist():
        return main.RedirectResponse('/login', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return main.csrf_token_response(
        request,
        'Initial setup',
        '\n<h1>Create administrator</h1><p class="muted">This one-time setup is disabled after the first user is created.</p>'
        f'{error_html}\n<form class="form-stack" method="post" action="/setup">\n'
        '  <input type="hidden" name="csrf_token" value="{{csrf}}" />\n'
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
    live_path = main.web_dir / 'live.html'
    if live_path.exists():
        return main.FileResponse(live_path)
    return root()


@router.get('/zones')
def zones_page():
    zones_path = main.web_dir / 'zones.html'
    if zones_path.exists():
        return main.FileResponse(zones_path)
    return root()


@router.get('/sounds')
def sounds_page():
    sounds_path = main.web_dir / 'sounds.html'
    if sounds_path.exists():
        return main.FileResponse(sounds_path)
    return root()


@router.get('/cameras')
def cameras_page():
    cameras_path = main.web_dir / 'cameras.html'
    if cameras_path.exists():
        return main.FileResponse(cameras_path)
    return root()


@router.get('/alerts')
@router.get('/events')
@router.get('/search')
def dashboard_aliases():
    return root()


@router.get('/recordings')
def recordings_page():
    recordings_path = main.web_dir / 'recordings.html'
    if recordings_path.exists():
        return main.FileResponse(recordings_path)
    return root()


@router.get('/recordings/timeline')
def recordings_timeline_page():
    timeline_path = main.web_dir / 'timeline.html'
    if timeline_path.exists():
        return main.FileResponse(timeline_path)
    return root()


@router.get('/onnx')
def onnx_page():
    ai_path = main.web_dir / 'onnx.html'
    if ai_path.exists():
        return main.FileResponse(ai_path)
    return root()


@router.get('/ai')
def ai_settings_page():
    return main.RedirectResponse('/onnx', status_code=308)


@router.get('/yamnet-tflite')
def yamnet_tflite_page():
    yamnet_path = main.web_dir / 'yamnet-tflite.html'
    if yamnet_path.exists():
        return main.FileResponse(yamnet_path)
    return root()


@router.get('/yamnet')
def yamnet_page():
    return main.RedirectResponse('/yamnet-tflite', status_code=308)


@router.get('/profile')
def profile_page():
    profile_path = main.web_dir / 'profile.html'
    if profile_path.exists():
        return main.FileResponse(profile_path)
    return root()


@router.get('/settings')
def system_settings_page():
    settings_path = main.web_dir / 'settings.html'
    if settings_path.exists():
        return main.FileResponse(settings_path)
    return root()


@router.get('/users')
def users_page():
    users_path = main.web_dir / 'users.html'
    if users_path.exists():
        return main.FileResponse(users_path)
    return root()


@router.get('/audit')
def audit_page():
    audit_path = main.web_dir / 'audit.html'
    if audit_path.exists():
        return main.FileResponse(audit_path)
    return root()


@router.get('/camera-log')
def camera_log_page():
    page_path = main.web_dir / 'camera-log.html'
    if page_path.exists():
        return main.FileResponse(page_path)
    return root()
