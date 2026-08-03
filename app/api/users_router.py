"""Users and Profile APIRouter.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import AuthError
from app.auth_gates import require_admin, require_user
from app.deps import get_auth, get_database
from app.request_helpers import write_audit_log

router = APIRouter()


@router.put('/api/profile')
async def update_profile(request: Request, auth=Depends(get_auth)):
    user = require_user(request)
    payload = await request.json()
    try:
        updated = auth.update_profile(
            int(user['id']),
            username=payload.get('username'),
            first_name=payload.get('first_name'),
            last_name=payload.get('last_name'),
            email=payload.get('email'),
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
    payload = await request.json()
    try:
        auth.change_password(
            int(user['id']),
            str(payload.get('current_password') or ''),
            str(payload.get('new_password') or ''),
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
    payload = await request.json()
    try:
        user = auth.create_user(
            payload.get('username', ''),
            payload.get('password', ''),
            payload.get('role', 'viewer'),
            first_name=payload.get('first_name', ''),
            last_name=payload.get('last_name', ''),
            email=payload.get('email', ''),
        )
        write_audit_log(request, db, 'create', 'user', user['id'], {'username': user['username'], 'role': user['role']})
        return user
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch('/api/users/{user_id}')
async def update_user(user_id: int, request: Request, db=Depends(get_database), auth=Depends(get_auth)):
    require_admin(request)
    payload = await request.json()
    changes: dict[str, Any] = {}
    if 'username' in payload:
        changes['username'] = payload['username']
    if 'first_name' in payload:
        changes['first_name'] = payload['first_name']
    if 'last_name' in payload:
        changes['last_name'] = payload['last_name']
    if 'email' in payload:
        changes['email'] = payload['email']
    if 'role' in payload:
        changes['role'] = payload['role']
    if 'is_active' in payload:
        changes['is_active'] = payload['is_active']
    if 'password' in payload:
        changes['password_changed'] = True
    try:
        user = auth.update_user(
            user_id,
            username=payload.get('username'),
            first_name=payload.get('first_name'),
            last_name=payload.get('last_name'),
            email=payload.get('email'),
            role=payload.get('role'),
            is_active=payload.get('is_active'),
            password=payload.get('password'),
        )
        write_audit_log(request, db, 'update', 'user', user_id, {'target_username': user.get('username'), **changes})
        return user
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
