"""Auth-gate helpers extracted from ``app/main.py`` (Phase-16).

Phase-16 audit of ``app/main.py`` identified the auth-gate cluster as the
highest-ROI group for extraction:

- Highest cross-router reach frequency amongst all helper groups (~19 sites
  across 13+ routers; ``require_admin`` alone is reached by every mutating
  admin-protected router in the codebase).
- Tightest cohesion (all four helpers are about session/role gating; they
  share the same input (``Request``) and the same State attributes set by
  ``authentication_middleware`` from Phase-15).
- Smallest extraction risk (~22 lines combined; no module-level state of
  their own to migrate; no decorator bindings to move; type-hint-only
  imports for the helpers themselves).

This module follows the same hybrid-pattern template as ``app/middleware.py``::

    import app.main as main
    # helpers reach main.<attr> at CALL time, not at module top

Callables moved (4):

- ``require_admin`` -- session-role gate; raises ``HTTPException(403)`` if
  ``request.state.user.role != 'admin'``. Returns the user dict.
- ``require_user`` -- returns ``request.state.user`` (set by
  ``authentication_middleware`` after a successful session lookup).
- ``require_session`` -- returns ``request.state.session`` (raw session
  dict with ``csrf_token``, ``expires_at``, ``user``).
- ``_request_ip`` -- loopback-aware client-IP extraction: if the direct
  connection is loopback (``127.0.0.1``, ``::1``, localhost), trust the
  first hop of the ``X-Forwarded-For`` header (so behind a reverse proxy
  the operator's real IP is reported); otherwise return the direct peer
  IP. Returns ``'unknown'`` if no client info is available.

Pool A from-import rebinds (preserved at the bottom of ``app/main.py``):

::

    from app.auth_gates import (
        require_admin as require_admin,
        require_user as require_user,
        require_session as require_session,
        _request_ip as _request_ip,
    )

This is the back-compat contract that lets every consumer router call
``main.require_admin(request)`` etc. without rewriting their imports.

Helpers KEPT on ``app.main`` (this module calls them via ``main.<attr>``):

- ``main._LOOPBACK`` -- the set of loopback identifiers ``_request_ip``
  matches against. Kept on ``app.main`` because ``_LOOPBACK`` is a
  transport-level constant that other future code in ``main.py`` (e.g.
  IP-rate-limit logic) may want to read, and the hybrid-pattern rules
  prefer keeping module-level constants at ``app.main`` (Pool A) with
  consumers reaching them via Pool C. Moving it into ``auth_gates.py``
  would split the constant's existence across two modules.

Re-typed imports: ``from fastapi import HTTPException, Request`` works at
top-level (Phase-15 verified ``HTTPException`` is reachable from
``fastapi.*``).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

import app.state as _state


def require_user(request: Request) -> dict[str, Any]:
    return request.state.user


def require_session(request: Request) -> dict[str, Any]:
    return request.state.session


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='Admin access required')
    return user


def _request_ip(request: Request) -> str:
    direct = request.client.host if request.client else ''
    if direct in _state._LOOPBACK:
        forwarded = request.headers.get('x-forwarded-for')
        if forwarded:
            return forwarded.split(',')[0].strip()
    return direct or 'unknown'
