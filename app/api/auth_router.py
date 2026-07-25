"""Authentication-flow APIRouter.

Handles auth state transitions (login, setup, logout). Direct imports replace
the ``import app.main as main`` hybrid pattern.

Routes:
- POST /login      -- login
- POST /setup      -- setup
- GET  /logout     -- logout_get
- POST /logout     -- logout_post
"""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import CSRF_COOKIE, CSRF_HEADER, AuthError, SESSION_COOKIE, utc_now
from app.auth_gates import _request_ip, require_session
from app.auth_helpers import clear_auth_cookies, set_session_cookie
from app.rate_limiter import login_limiter
from app.config_facades import effective_auth_config
from app.deps import get_auth, get_auth_enabled, get_database, get_logger
from app.request_helpers import form_data
from app.api.web_router import _safe_return_to, login_page, setup_page

router = APIRouter()


def _session_cookie_name() -> str:
    return str(effective_auth_config().get('cookie_name', SESSION_COOKIE))


@router.post('/login')
async def login(request: Request, db=Depends(get_database), auth=Depends(get_auth), auth_enabled=Depends(get_auth_enabled), logger=Depends(get_logger)):
    data = await form_data(request)
    if data.get('csrf_token') != request.cookies.get(CSRF_COOKIE):
        return login_page(request, 'Security token expired. Try again.')
    username = data.get('username', '')
    ip = _request_ip(request)

    # ── Rate-limit check ─────────────────────────────────────────────
    # Exponential backoff per IP before reaching the auth service.
    # Prevents brute-force attackers from getting immediate feedback
    # (a timing oracle) and logs every rate-limited attempt to the
    # immutable audit trail.
    wait = login_limiter.get_wait_seconds(ip)
    if wait > 0:
        retry_after = str(int(wait) + 1)
        try:
            db.add_audit_log(
                created_at=utc_now(),
                user_id=None,
                username=username or 'unknown',
                action='login',
                resource='rate_limit',
                ip_address=ip,
                status='failed',
                details={'reason': f'Rate limited — retry after {retry_after}s.'},
            )
        except Exception as unexpected_exc:
            logger.warning('Unexpected error during login rate-limit log: %s', unexpected_exc)
        from app.auth_helpers import csrf_token_response
        error_msg = f'Too many login attempts. Please wait {retry_after} seconds before trying again.'
        page_body = (
            '\n<h1>Sign In</h1><p class="muted">Enter your Daygle AI Camera credentials.</p>'
            f'<p class="error">{escape(error_msg)}</p>'
            '<form class="form-stack" method="post" action="/login">'
            '  <input type="hidden" name="csrf_token" value="{csrf}" />'
            '  <label>Username<input name="username" autocomplete="username" required /></label>'
            '  <label>Password<input name="password" type="password" autocomplete="current-password" required /></label>'
            '  <button class="primary" type="submit">Sign In</button>'
            '</form>'
        )
        response = csrf_token_response(request, 'Login', page_body, status_code=429)
        response.headers['Retry-After'] = retry_after
        return response

    try:
        _user, token, _csrf_token, expires_at = auth.authenticate(username, data.get('password', ''), ip)
    except AuthError as exc:
        login_limiter.record_failure(ip)
        try:
            db.add_audit_log(
                created_at=utc_now(),
                user_id=None,
                username=username,
                action='login',
                resource='session',
                ip_address=ip,
                status='failed',
                details={'reason': str(exc)},
            )
        except Exception as unexpected_exc:
            logger.warning('Unexpected error during login callback: %s', unexpected_exc)
        return login_page(request, str(exc), auth=auth, auth_enabled=auth_enabled)

    login_limiter.record_success(ip)
    try:
        db.add_audit_log(
            created_at=utc_now(),
            user_id=int(_user['id']),
            username=str(_user['username']),
            action='login',
            resource='session',
            ip_address=ip,
            status='success',
        )
    except Exception as unexpected_exc:
        logger.warning('Unexpected error during login: %s', unexpected_exc)
    # Honour the page the user was on before being kicked to /login. Validated
    # through the same hardened _safe_return_to() used by the GET handler so
    # no caller can craft a post-body return_to to bypass the loop guards.
    safe_return = _safe_return_to(data.get('return_to'))
    response = RedirectResponse(safe_return or '/', status_code=303)
    set_session_cookie(response, request, token, expires_at)
    response.delete_cookie(CSRF_COOKIE)
    return response


@router.post('/setup')
async def setup(request: Request, auth=Depends(get_auth), auth_enabled=Depends(get_auth_enabled)):
    if auth.users_exist():
        return RedirectResponse('/login', status_code=303)
    data = await form_data(request)
    if data.get('csrf_token') != request.cookies.get(CSRF_COOKIE):
        return setup_page(request, 'Security token expired. Try again.')
    if data.get('password') != data.get('confirm_password'):
        return setup_page(request, 'Passwords do not match.')
    try:
        auth.create_user(
            data.get('username', ''),
            data.get('password', ''),
            role='admin',
            first_name=data.get('first_name', ''),
            last_name=data.get('last_name', ''),
            email=data.get('email', ''),
        )
    except AuthError as exc:
        return setup_page(request, str(exc), auth=auth, auth_enabled=auth_enabled)
    return RedirectResponse('/login', status_code=303)


@router.get('/logout')
def logout_get(request: Request):
    return RedirectResponse('/login', status_code=303)


@router.post('/logout')
def logout_post(request: Request, db=Depends(get_database), auth=Depends(get_auth)):
    session = require_session(request)
    from app.request_helpers import write_audit_log
    # Resilient CSRF check: if the token is stale (e.g. session timed out and was
    # re-created, or the cross-tab sync overwrote the cached token), still honour
    # the user's intent to log out. A stale CSRF token should never prevent logout.
    csrf_ok = request.headers.get(CSRF_HEADER) == session['csrf_token']
    if not csrf_ok:
        write_audit_log(request, db, 'logout', 'session', details={'csrf_mismatch': True})
    else:
        write_audit_log(request, db, 'logout', 'session')
    auth.delete_session(request.cookies.get(_session_cookie_name()))
    response = JSONResponse({'ok': True})
    clear_auth_cookies(response)
    return response
