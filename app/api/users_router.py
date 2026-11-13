"""Users and Profile APIRouter.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.auth import AuthError, normalize_ntfy_topic
from app.auth_gates import require_admin, require_user
from app.deps import get_auth, get_database
from app.request_helpers import read_json_object, write_audit_log

router = APIRouter()


def _account_id(user: dict[str, Any]) -> int:
    """Return the signed-in account id, rejecting the anonymous pseudo-user.

    With ``auth.enabled: false`` ``require_user`` returns an anonymous
    administrator carrying ``id: None``: there is no profile row to read or
    update in that configuration, and ``int(user['id'])`` used to raise
    ``TypeError`` and surface as HTTP 500 from the profile page, the password
    form and the nav theme toggle's background save. 400 rather than 401 is
    deliberate -- the frontend treats 401 as session loss and would bounce the
    user to /login, which is meaningless when authentication is switched off.
    """
    user_id = user.get('id')
    if user_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                'Profile settings require an authenticated account; '
                'authentication is disabled.'
            ),
        )
    return int(user_id)


@router.put('/api/profile')
async def update_profile(request: Request, auth=Depends(get_auth)):
    user = require_user(request)
    user_id = _account_id(user)
    payload = await read_json_object(request)
    try:
        # bcrypt verification is CPU-bound: keep it off the event loop.
        updated = await run_in_threadpool(
            auth.update_profile,
            user_id,
            username=payload.get('username'),
            first_name=payload.get('first_name'),
            last_name=payload.get('last_name'),
            email=payload.get('email'),
            ntfy_topic=payload.get('ntfy_topic'),
            timezone_name=payload.get('timezone'),
            date_format=payload.get('date_format'),
            time_format=payload.get('time_format'),
            theme=payload.get('theme'),
            # H4 fix: forward the optional ``current_password`` field so
            # ``auth.update_profile`` can verify it when the request
            # actually changes email or username. Non-sensitive updates
            # leave it at ``None`` and bypass the verify.
            current_password=payload.get('current_password') or None,
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.state.user = updated
    return updated


@router.post('/api/profile/password')
async def change_profile_password(request: Request, auth=Depends(get_auth)):
    user = require_user(request)
    user_id = _account_id(user)
    payload = await read_json_object(request)
    session = getattr(request.state, 'session', None) or {}
    try:
        await run_in_threadpool(
            auth.change_password,
            user_id,
            str(payload.get('current_password') or ''),
            str(payload.get('new_password') or ''),
            keep_session_token=session.get('session_token'),
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True}


@router.get('/api/users')
def list_users(request: Request, auth=Depends(get_auth)):
    # H2 fix: tighten from ``require_user`` (any signed-in account) to
    # ``require_admin`` so viewers can no longer enumerate every other
    # username + email on the system. The auth middleware already
    # enforces the session check; this gate is the second line for
    # role separation.
    require_admin(request)
    return auth.list_users()


@router.post('/api/users')
async def create_user(request: Request, db=Depends(get_database), auth=Depends(get_auth)):
    require_admin(request)
    payload = await read_json_object(request)
    try:
        # Validate before creating so a bad topic does not leave a half-made user.
        ntfy_topic = normalize_ntfy_topic(payload.get('ntfy_topic', ''))
        user = await run_in_threadpool(
            auth.create_user,
            payload.get('username', ''),
            payload.get('password', ''),
            payload.get('role', 'viewer'),
            first_name=payload.get('first_name', ''),
            last_name=payload.get('last_name', ''),
            email=payload.get('email', ''),
        )
        if ntfy_topic:
            user = await run_in_threadpool(auth.update_user, int(user['id']), ntfy_topic=ntfy_topic)
        write_audit_log(request, db, 'create', 'user', user['id'], {'username': user['username'], 'role': user['role']})
        return user
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch('/api/users/{user_id}')
async def update_user(user_id: int, request: Request, db=Depends(get_database), auth=Depends(get_auth)):
    require_admin(request)
    payload = await read_json_object(request)
    changes: dict[str, Any] = {}
    if 'username' in payload:
        changes['username'] = payload['username']
    if 'first_name' in payload:
        changes['first_name'] = payload['first_name']
    if 'last_name' in payload:
        changes['last_name'] = payload['last_name']
    if 'email' in payload:
        changes['email'] = payload['email']
    if 'ntfy_topic' in payload:
        changes['ntfy_topic'] = payload['ntfy_topic']
    if 'role' in payload:
        changes['role'] = payload['role']
    if 'is_active' in payload:
        changes['is_active'] = payload['is_active']
    if 'password' in payload:
        changes['password_changed'] = True
    try:
        user = await run_in_threadpool(
            auth.update_user,
            user_id,
            username=payload.get('username'),
            first_name=payload.get('first_name'),
            last_name=payload.get('last_name'),
            email=payload.get('email'),
            role=payload.get('role'),
            is_active=payload.get('is_active'),
            password=payload.get('password'),
            ntfy_topic=payload.get('ntfy_topic'),
        )
        write_audit_log(request, db, 'update', 'user', user_id, {'target_username': user.get('username'), **changes})
        return user
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
