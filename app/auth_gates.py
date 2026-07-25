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
from app.config_facades import effective_auth_config


# Default trust set - matches the legacy ``_state._LOOPBACK`` behaviour so a
# loopback direct peer is still trusted when ``trusted_proxies`` is missing
# from the YAML/DB override (e.g. unit tests that import ``app.auth_gates``
# without running ``_startup()``).
_DEFAULT_TRUSTED_PROXIES: frozenset[str] = frozenset({"127.0.0.1", "::1"})


def _trusted_proxies() -> frozenset[str]:
    """Return the set of peer IPs whose ``X-Forwarded-For`` header is trusted.

    Reads ``auth.trusted_proxies`` from ``effective_auth_config()`` so a
    database override wins over the YAML default. Falls back to the
    in-memory ``_state.auth_config`` snapshot (and finally to loopback)
    when the database singleton is not yet initialised - keeps the helper
    unit-test-friendly without forcing every test to mock ``effective_auth_config``.
    Accepted value types: ``list[str]`` (preferred), ``str`` (comma-separated).
    """
    config: dict[str, Any] | None = None
    try:
        if getattr(_state, "database", None) is not None:
            config = effective_auth_config()
    except Exception:
        config = None
    if config is None:
        config = getattr(_state, "auth_config", None) or {}
    trusted = config.get("trusted_proxies")
    if isinstance(trusted, str):
        return frozenset(part.strip() for part in trusted.split(",") if part.strip()) or _DEFAULT_TRUSTED_PROXIES
    if isinstance(trusted, (list, tuple, set)):
        parsed = frozenset(str(item).strip() for item in trusted if item)
        return parsed or _DEFAULT_TRUSTED_PROXIES
    return _DEFAULT_TRUSTED_PROXIES


def _auth_enabled() -> bool:
    """Live (post-DB-override) auth-enabled flag.

    Replaces the stale ``_state.auth_config.get('enabled', True)`` reads that
    used to disagree with ``authentication_middleware`` (which already reads
    ``effective_auth_config()``), causing a split-brain where middleware let
    unauthenticated requests through when admin disabled auth in the DB but
    the route gates still raised 401. Routers and shared helpers should call
    this function instead of reading ``_state.auth_config`` directly.
    """
    return bool(effective_auth_config().get('enabled', True))


def require_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, 'user', None)
    if user is None:
        if not _auth_enabled():
            return {'id': None, 'role': 'admin', 'username': 'anonymous'}
        raise HTTPException(status_code=401, detail='Authentication required')
    return user


def require_session(request: Request) -> dict[str, Any]:
    session = getattr(request.state, 'session', None)
    if session is None:
        if not _auth_enabled():
            anon: dict[str, Any] = {'id': None, 'role': 'admin', 'username': 'anonymous'}
            return {'user': anon, 'csrf_token': '', 'expires_at': ''}
        raise HTTPException(status_code=401, detail='Authentication required')
    return session


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='Admin access required')
    return user


def _request_ip(request: Request) -> str:
    direct = getattr(request, 'client', None).host if getattr(request, 'client', None) else ''
    # Honour X-Forwarded-For ONLY when the direct peer is in the configured
    # trusted-proxies set. Default is loopback so a localhost dev server
    # trusts its own reverse proxy while a Docker / LAN deployment must
    # explicitly whitelist the upstream-proxy IP. This defends against
    # client-side IP spoofing when the app is exposed beyond the loopback
    # interface (CSRF protection / rate-limit / audit-log poisoning).
    if direct in _trusted_proxies():
        forwarded = request.headers.get('x-forwarded-for')
        if forwarded:
            # X-Forwarded-For per RFC 7239 convention (and the XFF de-facto
            # format used by Nginx / Traefik / Caddy / HAProxy / Cloudflare)
            # is a comma-separated list where the LEFTMOST entry is the
            # ORIGINAL client IP and each subsequent entry is one proxy hop.
            # Reverse proxies commonly append their own IP to the right, so
            # ``[-1]`` (the most recent hop) is the proxy, not the client.
            # Always pick ``[0]`` -- and tolerate accidental whitespace by
            # stripping each candidate before extracting the first entry.
            first_hop = forwarded.split(',')[0].strip()
            if first_hop:
                return first_hop
    return direct or 'unknown'
