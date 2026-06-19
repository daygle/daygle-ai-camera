"""Phase-16 integration tests for ``app/auth_gates.py``.

Phase-16 extracted the 4 auth-gate helpers (``require_admin``,
``require_user``, ``require_session``, ``_request_ip``) from
``app/main.py`` into ``app/auth_gates.py`` as plain functions. Routers
reach them via ``main.<name>`` (Pool C bare-name reach), preserved by
Pool A from-import rebinds appended at the bottom of ``app/main.py``.

These tests pin two contracts:

1. **Pool A back-compat (identity).** The Pool A rebinds at the bottom of
   ``app/main.py`` MUST wire ``main.<name>`` to the SAME function object
   as ``app.auth_gates.<name>``. If a future refactor drops or shims a
   rebind, ``main.require_admin`` becomes its own (different) callable
   and the routers silently break at request time -- ``is`` comparison
   catches it cleanly.

2. **``_request_ip`` behavior.** Loopback caller with
   ``X-Forwarded-For`` returns the first hop; loopback caller without
   the header returns the loopback direct; non-loopback caller ignores
   ``X-Forwarded-For`` and returns the direct (so a hostile client
   can't spoof its IP through a load-balancer hop that's not
   loopback-trusted).

The end-to-end ``require_admin`` HTTP gating is already covered extensively
in ``tests/test_api.py`` -- most relevantly by
``test_admin_ai_settings_viewer_denied_and_db_override`` which exercises
the exact ``viewer PUT /api/settings/ai -> 403 'Admin access required'``
flow with full JSON-shape assertions, and by ``test_logout_user_creation``
which exercises the full admin lifecycle. The dozens of other mutating
endpoint tests cover every other ``require_admin`` reach site indirectly.

The contract this file defends is the Pool A back-compat identity (rebinds
are not AST-walkable by existing invariants -- ``from X import Y as Y``
looks syntactically ordinary), plus the bin-tested edge cases of
``_request_ip`` that aren't easily exercised through a uvicorn thread.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Top-level lazy-ordered preloads to break the Phase-16 circular-import gate.
#
# `app/auth_gates.py` at module top does ``import app.main as main``. If THIS
# test file is the first thing pytest collects and triggers ``import
# app.auth_gates`` FIRST, Python's fresh-load chain is:
#
#   pytest collects tests/test_auth_gates.py
#     -> module top hits ``import app.auth_gates as auth_gates``
#     -> Python starts fresh load of app/auth_gates.py
#     -> auth_gates top: ``import app.main as main`` -- app.main NOT in
#        sys.modules yet, so Python starts fresh load of app/main.py
#     -> app/main.py runs top-to-bottom, reaches its bottom-of-file rebind:
#            from app.auth_gates import (require_admin as require_admin, ...)
#     -> app.auth_gates IS in sys.modules (mid-load from above) but its
#        4 FunctionDefs HAVEN'T RUN YET -- ImportError.
#
# Forced-order fix: explicitly populate `sys.modules['app.main']` with the
# fully-loaded module BEFORE invoking ``import app.auth_gates`` below.
# This means Python returns the cached module from ``import app.main as
# main`` and the 4 functions get defined normally.
#
# This mirrors the ``app_modules`` fixture in
# tests/test_web_auth_router_integration.py -- whose docstring (which is
# 50 lines) explains the same circular-import contract for Phase-13's
# web_router / auth_router.
import app.main  # noqa: E402  -- must precede the import below
import app.auth_gates  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Pool A back-compat identity -- ``main.<name> is app.auth_gates.<name>``.
# ---------------------------------------------------------------------------


def test_main_require_admin_is_auth_gates_require_admin():
    """``app/main.py`` must expose ``require_admin`` as the SAME function
    object as ``app.auth_gates.require_admin``.

    Pool A from-import rebind at the bottom of ``main.py``. If this rebind
    is ever dropped or aliased to a wrapper, every mutating admin-only
    route silently breaks (routers call ``main.require_admin(request)``
    and Python ``NameError``s or routes through a different callable).
    """
    main = sys.modules["app.main"]
    assert hasattr(main, "require_admin"), (
        "main.require_admin back-compat alias missing -- Pool A from-import "
        "rebind at the bottom of app/main.py was dropped or never written"
    )
    import app.auth_gates as auth_gates
    assert main.require_admin is auth_gates.require_admin, (
        "main.require_admin is NOT the same function object as "
        "app.auth_gates.require_admin -- the Pool A rebind was wired wrong "
        "(e.g., to a wrapper, lambda, or shim). Routers will break at "
        "request time."
    )


def test_main_require_user_is_auth_gates_require_user():
    """Same identity check for ``require_user``.

    Used by ``app/api/users_router.py`` to gate ``GET /api/users/me`` and
    ``PATCH /api/users/{id}`` when the caller is allowed to operate on
    themselves.
    """
    main = sys.modules["app.main"]
    assert hasattr(main, "require_user"), (
        "main.require_user back-compat alias missing"
    )
    import app.auth_gates as auth_gates
    assert main.require_user is auth_gates.require_user, (
        "main.require_user is NOT the same function object as "
        "app.auth_gates.require_user"
    )


def test_main_require_session_is_auth_gates_require_session():
    """Same identity check for ``require_session``.

    Used by ``app/api/admin_router.py`` and ``app/api/auth_router.py``
    for session-shaped reads (csrf_token + expires_at + user).
    """
    main = sys.modules["app.main"]
    assert hasattr(main, "require_session"), (
        "main.require_session back-compat alias missing"
    )
    import app.auth_gates as auth_gates
    assert main.require_session is auth_gates.require_session, (
        "main.require_session is NOT the same function object as "
        "app.auth_gates.require_session"
    )


def test_main_request_ip_is_auth_gates_request_ip():
    """Same identity check for ``_request_ip``.

    Used by ``app/api/auth_router.py`` to capture source IP for the
    login-attempts audit table. The underscore prefix is preserved --
    the routers reach it as ``main._request_ip`` rather than
    ``main.request_ip``.
    """
    main = sys.modules["app.main"]
    assert hasattr(main, "_request_ip"), (
        "main._request_ip back-compat alias missing"
    )
    import app.auth_gates as auth_gates
    assert main._request_ip is auth_gates._request_ip, (
        "main._request_ip is NOT the same function object as "
        "app.auth_gates._request_ip"
    )


# ---------------------------------------------------------------------------
# 2. ``_request_ip`` behavior -- loopback honors X-Forwarded-For, else direct.
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, host: str | None) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, host: str | None, headers: dict[str, str]) -> None:
        self.client = _FakeClient(host) if host is not None else None
        self.headers = headers


def test_request_ip_returns_xff_when_loopback_and_header_present():
    """Loopback connection + ``X-Forwarded-For: 1.2.3.4`` -> returns ``1.2.3.4``.

    The intent: behind a reverse proxy, the direct connection is loopback
    (proxy -> app) but the REAL client IP is in XFF. ``_request_ip`` must
    strip and pick the first hop.
    """
    import app.auth_gates as auth_gates
    request = _FakeRequest("127.0.0.1", {"x-forwarded-for": "1.2.3.4, 10.0.0.1"})
    assert auth_gates._request_ip(request) == "1.2.3.4"


def test_request_ip_returns_loopback_direct_when_no_xff_header():
    """Loopback connection WITHOUT XFF -> returns the direct loopback.

    Operator's curl from localhost: returns ``127.0.0.1`` (no proxy).
    """
    import app.auth_gates as auth_gates
    request = _FakeRequest("127.0.0.1", {})
    assert auth_gates._request_ip(request) == "127.0.0.1"


def test_request_ip_honors_ipv6_loopback_xff():
    """IPv6 loopback (``::1``) + XFF must return the first XFF hop, same as IPv4.

    ``main._LOOPBACK`` includes ``'::1'``; verify the set membership works
    for both loopback families.
    """
    import app.auth_gates as auth_gates
    request = _FakeRequest("::1", {"x-forwarded-for": "203.0.113.5"})
    assert auth_gates._request_ip(request) == "203.0.113.5"


def test_request_ip_ignores_xff_when_direct_is_non_loopback():
    """Non-loopback direct + spoofed XFF -> returns the DIRECT, NOT XFF.

    Security boundary: a hostile external client cannot spoof their IP via
    XFF if the connection is direct to the app. Only proxy-fronted loopback
    connections trust XFF (because that's where the trusted proxy inserts it).
    """
    import app.auth_gates as auth_gates
    request = _FakeRequest(
        "198.51.100.7",
        {"x-forwarded-for": "1.2.3.4"},
    )
    assert auth_gates._request_ip(request) == "198.51.100.7", (
        "_request_ip must trust XFF ONLY when the direct connection is loopback; "
        "non-loopback direct peers can't insert a trusted XFF hop"
    )


def test_request_ip_returns_unknown_when_client_info_missing():
    """A request with ``request.client = None`` (edge case in some ASGI
    transports) -> returns ``'unknown'`` rather than raising AttributeError."""
    import app.auth_gates as auth_gates
    request = _FakeRequest(None, {})
    assert auth_gates._request_ip(request) == "unknown"
