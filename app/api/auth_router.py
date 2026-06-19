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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import CSRF_COOKIE, CSRF_HEADER, AuthError, SESSION_COOKIE, utc_now
from app.auth_gates import _request_ip, require_session
from app.auth_helpers import clear_auth_cookies, set_session_cookie
from app.config_facades import effective_auth_config
from app.deps import get_auth, get_database
from app.request_helpers import form_data
from app.main import auth_enabled, logger
from app.api.web_router import login_page, setup_page

router = APIRouter()


def _session_cookie_name() -> str:
    return str(effective_auth_config().get('cookie_name', SESSION_COOKIE))


@router.post('/login')
async def login(request: Request, db=Depends(get_database), auth=Depends(get_auth)):
    data = await form_data(request)
    if data.get('csrf_token') != request.cookies.get(CSRF_COOKIE):
        return login_page(request, 'Security token expired. Try again.')
    username = data.get('username', '')
    ip = _request_ip(request)
    try:
        _user, token, _csrf_token, expires_at = auth.authenticate(username, data.get('password', ''), ip)
    except AuthError as exc:
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
        return login_page(request, str(exc))
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
    response = RedirectResponse('/', status_code=303)
    set_session_cookie(response, request, token, expires_at)
    response.delete_cookie(CSRF_COOKIE)
    return response


@router.post('/setup')
async def setup(request: Request, auth=Depends(get_auth)):
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
        return setup_page(request, str(exc))
    return RedirectResponse('/login', status_code=303)


@router.get('/logout')
def logout_get(request: Request):
    return RedirectResponse('/login', status_code=303)


@router.post('/logout')
def logout_post(request: Request, db=Depends(get_database), auth=Depends(get_auth)):
    session = require_session(request)
    if request.headers.get(CSRF_HEADER) != session['csrf_token']:
        return JSONResponse({'detail': 'CSRF token missing or invalid'}, status_code=403)
    from app.request_helpers import write_audit_log
    write_audit_log(request, db, 'logout', 'session')
    auth.delete_session(request.cookies.get(_session_cookie_name()))
    response = JSONResponse({'ok': True})
    clear_auth_cookies(response)
    return response
