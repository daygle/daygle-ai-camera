"""Users and Profile APIRouter.

Extracted from ``app/main.py`` (Phase-6 of the hybrid-pattern router split).
Same template as ``app/api/recordings_router.py`` (Phase-3),
``app/api/cameras_router.py`` (Phase-4), and
``app/api/events_router.py`` (Phase-5): ``import app.main as main`` at
module level, every global / helper read through ``main.<name>`` *inside*
handler bodies.

Handlers moved (5):

- PUT    /api/profile
- POST   /api/profile/password
- GET    /api/users
- POST   /api/users
- PATCH  /api/users/{user_id}

Note: the user prompt for Phase-6 also listed ``/api/profile/notifications``.
That endpoint does not exist in ``app/main.py`` (the front-end profile page
exposes notification preferences inline via the main ``/api/profile``
PUT body, not as a separate sub-route), so it is deliberately not part
of this extraction. The notifications fields ``date_format`` /
``time_format`` / ``timezone`` ride on ``/api/profile``'s body.

The splice was AST tree-filter + unparse (Phase-2 / Phase-3 / Phase-4 /
Phase-5 safe pattern). See ``app/api/__init__.py`` for the full
hybrid-pattern rules.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.auth`` — module-level binding set by ``app.main``'s top-of-file
  ``import app.auth as auth``. Every ``main.auth.<fn>(...)`` call here is
  a verbatim copy of the original ``auth.<fn>(...)`` call from ``main.py``.
- ``main.require_admin`` / ``main.require_user`` — auth gates. ``require_user``
  reads the current session user from ``request.state`` (set by the
  login / session middleware earlier in the request lifecycle) and rejects
  unauthenticated callers; ``require_admin`` further checks role.
- ``main.write_audit_log`` — admin-only mutation ledger.
  ``require_admin`` gates the calls via Starlette request headers, and
  ``write_audit_log`` records the resulting user/admin mutation.
- ``main.AuthError`` — domain exception from ``app.auth``. Reaching it
  via ``main.AuthError`` keeps the alias-once-in-main convention that
  Phases 3-5 established; importing it directly from ``app.auth`` would
  bypass the hybrid pattern.

Tests go through ``LocalClient.request`` rather than calling
``main.<attr>`` directly, so no back-compat alias on ``app.main`` is
needed for these endpoints. The existing
``tests/test_api_router_split_invariants.py::test_all_main_attr_references_resolve_on_app_main``
+ ``routes-coverage`` invariant exercises every ``main.<attr>`` lookup
below on every test run.

Security notes
- ``change_profile_password`` is explicitly the password-change route
  (Phase-7 audit). The body intentionally requires the current password
  via ``auth.change_password(int(user['id']), current_password, new_password)``
  — the ``AuthService`` rejects mismatches with ``AuthError``. The router
  does NOT add any extra credential check; the helper is the source of
  truth.
- ``create_user`` / ``update_user`` are admin-only via ``require_admin``.
- ``update_profile`` updates ``request.state.user = updated`` after the
  AuthService mutates the row so the rest of the request lifecycle sees
  the new values (no-op if no cookie middleware is in scope).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

import app.main as main

router = APIRouter()


@router.put('/api/profile')
async def update_profile(request: Request):
    user = main.require_user(request)
    payload = await request.json()
    try:
        updated = main.auth.update_profile(
            int(user['id']),
            username=payload.get('username'),
            first_name=payload.get('first_name'),
            last_name=payload.get('last_name'),
            email=payload.get('email'),
            timezone_name=payload.get('timezone'),
            date_format=payload.get('date_format'),
            time_format=payload.get('time_format'),
        )
    except main.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.state.user = updated
    return updated


@router.post('/api/profile/password')
async def change_profile_password(request: Request):
    user = main.require_user(request)
    payload = await request.json()
    try:
        main.auth.change_password(
            int(user['id']),
            str(payload.get('current_password') or ''),
            str(payload.get('new_password') or ''),
        )
    except main.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True}


@router.get('/api/users')
def list_users(request: Request):
    main.require_user(request)
    return main.auth.list_users()


@router.post('/api/users')
async def create_user(request: Request):
    main.require_admin(request)
    payload = await request.json()
    try:
        user = main.auth.create_user(
            payload.get('username', ''),
            payload.get('password', ''),
            payload.get('role', 'viewer'),
            first_name=payload.get('first_name', ''),
            last_name=payload.get('last_name', ''),
            email=payload.get('email', ''),
        )
        main.write_audit_log(request, 'create', 'user', user['id'], {'username': user['username'], 'role': user['role']})
        return user
    except main.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch('/api/users/{user_id}')
async def update_user(user_id: int, request: Request):
    main.require_admin(request)
    payload = await request.json()
    changes: dict[str, Any] = {}
    if 'role' in payload:
        changes['role'] = payload['role']
    if 'is_active' in payload:
        changes['is_active'] = payload['is_active']
    if 'password' in payload:
        changes['password_changed'] = True
    try:
        user = main.auth.update_user(
            user_id,
            role=payload.get('role'),
            is_active=payload.get('is_active'),
            password=payload.get('password'),
        )
        main.write_audit_log(request, 'update', 'user', user_id, {'target_username': user.get('username'), **changes})
        return user
    except main.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
