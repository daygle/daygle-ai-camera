"""Authentication-flow APIRouter.

Extracted from ``app/main.py`` (Phase 13 of the hybrid-pattern router split).
Same template as ``app/api/admin_router.py`` (Phase 11) and
``app/api/live_router.py`` (Phase 10): ``import app.main as main`` at module
level, every global / helper read through ``main.<name>`` *inside* handler
bodies.

This file deliberately bundles the four POST/GET handlers that drive the
auth + setup state-machine into a single router rather than splitting
them one-per-file:

- ``POST /login``     -- ``login``     (attempt session creation)
- ``POST /setup``     -- ``setup``     (first-time admin wizard POST)
- ``GET  /logout``    -- ``logout_get`` (303 redirect to /login)
- ``POST /logout``    -- ``logout_post`` (kill session + clear cookies)

The motivation: these 4 endpoints share a single state-transition
surface (``CSRF_COOKIE``, ``CSRF_HEADER``, ``SESSION_COOKIE_NAME``,
``AuthError`` exception class, ``auth.authenticate``,
``auth.create_user``, ``auth.delete_session``, ``database.add_audit_log``,
``set_session_cookie``, ``clear_auth_cookies``, ``require_session``,
``form_data``, ``_request_ip``). Splitting them one-per-file would
multiply this contract; bundling into one ``auth_router`` mirrors the
Phase-11 admin_router cleanup-class precedent while centralizing the
auth-flow risk surface.

The companion ``web_router.py`` continues to hold the GET counterparts
(``/login`` ``setup_page`` ``/logout`` ``root``); those are pure HTML
renders, not state transitions, so the natural domain line lives in
``web_router``. The two routers together cover the full
login/logout/setup state machine: web_router = GET pages,
auth_router = POST endpoints.

BODY-REWRITE NOTE
Handlers in the original ``main.py`` referenced module-level state via
bare names (``form_data``, ``_request_ip``, ``CSRF_COOKIE``, ``CSRF_HEADER``,
``SESSION_COOKIE_NAME``, ``auth``, ``AuthError``, ``database``, ``utc_now``,
``logger``, ``require_session``, ``write_audit_log``, ``set_session_cookie``,
``clear_auth_cookies``). After extraction to this router, those bare
names resolve to ZERO attributes in our namespace -- handlers would
NameError at request time. Per hybrid-pattern uniformity (rule 5 of
``app/api/__init__.py``), each bare call is rewritten as
``main.<bare>``.

CSRF-FLOW NOTE
Both POST handlers verify ``data.get('csrf_token') ==
request.cookies.get(main.CSRF_COOKIE)`` before proceeding -- this is the
hybrid pattern preserving the exact same CSRF check chain as main.py,
no behavior change.

AUDIT-LOG NOTE
The original ``login`` handler wrote two ``database.add_audit_log`` rows
(failed + success cases) wrapped in try/except -- so an audit-log
write failure never blocks the login flow. The router preserves the
double-write + try/except pattern verbatim.

STD-LIB NOTE
No new stdlib imports are needed; ``auth_router.py`` reaches all
helpers via ``main.<bare>`` reads.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.form_data`` -- async helper that reads ``application/x-www-form-urlencoded``
  bodies into a dict.
- ``main._request_ip`` -- helper that derives the client IP from the
  ``X-Forwarded-For`` header (set by reverse proxies) with a fallback
  to ``request.client.host``.
- ``main.CSRF_COOKIE`` -- cookie name for the per-session CSRF token.
- ``main.CSRF_HEADER`` -- header name (``X-CSRF-Token``) that the
  POST handlers compare against the session's stored CSRF token.
- ``main.SESSION_COOKIE_NAME`` -- resolved session cookie name used by
  ``auth.delete_session`` to look up the session in the auth service.
- ``main.auth`` -- AuthService instance with ``.authenticate()``,
  ``.users_exist()``, ``.create_user()``, ``.delete_session()``.
- ``main.AuthError`` -- Exception class raised by ``auth.authenticate``
  and ``auth.create_user`` on validation/credential failures.
- ``main.database`` -- EventDatabase instance; ``.add_audit_log()`` is
  used by both the login success and login failure paths.
- ``main.utc_now`` -- ISO-formatted UTC timestamp; ``created_at``
  parameter on ``database.add_audit_log``.
- ``main.logger`` -- module-level logger; warning emitter for swallowed
  audit-log write errors.
- ``main.require_session`` -- session-cookie validator that returns the
  session dict or raises 401.
- ``main.write_audit_log`` -- audit-log emitter for the logout path.
- ``main.set_session_cookie`` -- cookie setter for the login response.
- ``main.clear_auth_cookies`` -- cookie-clearer for the logout response.

FastAPI builtins stay at top-level imports: ``APIRouter``,
``JSONResponse``, ``RedirectResponse``, ``Request``.

Tests go through ``LocalClient.request`` rather than calling
``main.login`` / ``main.setup`` / ``main.logout_post`` directly, so no
back-compat alias on ``app.main`` is needed. The Phase 7.1 invariant
``tests/test_api_router_split_invariants.py::test_app_api_imports_in_main_are_consumed``
will catch any orphan-import regression if a future refactor drops the
``from app.api.auth_router import router as auth_router`` rebind line in
``app/main.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

import app.main as main

router = APIRouter()


@router.post('/login')
async def login(request: Request):
    data = await main.form_data(request)
    if data.get('csrf_token') != request.cookies.get(main.CSRF_COOKIE):
        return main.login_page(request, 'Security token expired. Try again.')
    username = data.get('username', '')
    ip = main._request_ip(request)
    try:
        _user, token, _csrf_token, expires_at = main.auth.authenticate(username, data.get('password', ''), ip)
    except main.AuthError as exc:
        try:
            main.database.add_audit_log(
                created_at=main.utc_now(),
                user_id=None,
                username=username,
                action='login',
                resource='session',
                ip_address=ip,
                status='failed',
                details={'reason': str(exc)},
            )
        except Exception as unexpected_exc:
            main.logger.warning('Unexpected error during login callback: %s', unexpected_exc)
        return main.login_page(request, str(exc))
    try:
        main.database.add_audit_log(
            created_at=main.utc_now(),
            user_id=int(_user['id']),
            username=str(_user['username']),
            action='login',
            resource='session',
            ip_address=ip,
            status='success',
        )
    except Exception as unexpected_exc:
        main.logger.warning('Unexpected error during login: %s', unexpected_exc)
    response = main.RedirectResponse('/', status_code=303)
    main.set_session_cookie(response, request, token, expires_at)
    response.delete_cookie(main.CSRF_COOKIE)
    return response


@router.post('/setup')
async def setup(request: Request):
    if main.auth.users_exist():
        return main.RedirectResponse('/login', status_code=303)
    data = await main.form_data(request)
    if data.get('csrf_token') != request.cookies.get(main.CSRF_COOKIE):
        return main.setup_page(request, 'Security token expired. Try again.')
    if data.get('password') != data.get('confirm_password'):
        return main.setup_page(request, 'Passwords do not match.')
    try:
        main.auth.create_user(
            data.get('username', ''),
            data.get('password', ''),
            role='admin',
            first_name=data.get('first_name', ''),
            last_name=data.get('last_name', ''),
            email=data.get('email', ''),
        )
    except main.AuthError as exc:
        return main.setup_page(request, str(exc))
    return main.RedirectResponse('/login', status_code=303)


@router.get('/logout')
def logout_get(request: Request):
    return main.RedirectResponse('/login', status_code=303)


@router.post('/logout')
def logout_post(request: Request):
    session = main.require_session(request)
    if request.headers.get(main.CSRF_HEADER) != session['csrf_token']:
        return main.JSONResponse({'detail': 'CSRF token missing or invalid'}, status_code=403)
    main.write_audit_log(request, 'logout', 'session')
    main.auth.delete_session(request.cookies.get(main.SESSION_COOKIE_NAME))
    response = main.JSONResponse({'ok': True})
    main.clear_auth_cookies(response)
    return response
