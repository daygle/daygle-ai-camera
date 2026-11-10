"""Session/role gates and client-IP resolution shared by the API routers.

Exported helpers:

- ``require_user`` -- returns the authenticated user dict from
  ``request.state``; raises 401 when auth is enabled and no session is set,
  and returns an anonymous admin when auth is disabled.
- ``require_session`` -- returns the full session dict (``user``,
  ``csrf_token``, ``expires_at``) with the same 401/anonymous behaviour.
- ``require_admin`` -- ``require_user`` plus an admin-role check (403).
- ``_request_ip`` -- client IP for audit logging and rate limiting.
  ``X-Forwarded-For`` is honoured only when the direct peer is a configured
  trusted proxy, so an untrusted client cannot spoof its address.
- ``is_trusted_proxy`` -- membership test for ``auth.trusted_proxies``,
  accepting single IPs and CIDR ranges.

The auth-enabled flag and the trusted-proxy list are read live through
``effective_auth_config()``, so a database override saved from the UI applies
without a restart. Shared constants (public/admin paths, mutating methods)
live in ``app.state``.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Any

from fastapi import HTTPException, Request

import app.state as _state
from app.config_facades import effective_auth_config


# Default trust set: a loopback direct peer is trusted when ``trusted_proxies``
# is missing from the YAML/DB override (e.g. unit tests that import
# ``app.auth_gates`` without running ``_startup()``).
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


@lru_cache(maxsize=32)
def _trusted_networks(entries: frozenset[str]) -> tuple[Any, ...]:
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def is_trusted_proxy(peer: str | None) -> bool:
    """True when the direct peer is a configured trusted proxy.

    ``trusted_proxies`` accepts IPs *and* CIDR ranges (the settings validator
    allows both), so membership is a network match, not a string comparison:
    a ``192.168.1.0/24`` entry previously never matched any peer.
    """
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(str(peer).split('%', 1)[0])
    except ValueError:
        return peer in _trusted_proxies()
    return any(address in network for network in _trusted_networks(_trusted_proxies()))


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
    if is_trusted_proxy(direct):
        forwarded = request.headers.get('x-forwarded-for')
        if forwarded:
            # Each proxy (Nginx ``$proxy_add_x_forwarded_for``, Traefik, Caddy,
            # HAProxy, Cloudflare) APPENDS the address it received the request
            # from, so only the entries a trusted proxy wrote are trustworthy:
            # everything to their left is whatever the client chose to send.
            # Taking the leftmost entry let any client pick its own IP with a
            # ``X-Forwarded-For`` header (``spoofed, real`` after the proxy
            # appends), defeating the per-IP login backoff and poisoning audit
            # logs. Walk from the right instead, skip hops that are themselves
            # trusted proxies, and return the first untrusted address: the
            # nearest client no trusted party vouches past.
            hops = [hop.strip() for hop in forwarded.split(',')]
            if hops and hops[0]:
                for hop in reversed(hops):
                    if hop and not is_trusted_proxy(hop):
                        return hop
                # Every entry is a trusted proxy (internal chain): the
                # leftmost is the furthest hop a trusted party recorded.
                return hops[0]
    return direct or 'unknown'
