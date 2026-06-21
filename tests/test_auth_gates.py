"""Tests for ``app/auth_gates.py``.

``require_admin``, ``require_user``, ``require_session``, and ``_request_ip``
live in ``app/auth_gates.py``; all routers import them directly from there.

These tests pin ``_request_ip`` behavior: loopback caller with
``X-Forwarded-For`` returns the first hop; loopback caller without the header
returns the loopback direct; non-loopback caller ignores XFF (so a hostile
client can't spoof its IP).

End-to-end ``require_admin`` HTTP gating is covered in ``tests/test_api.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load app.main first to avoid the circular-import gate where auth_gates.py
# does ``import app.main as main`` at module top.
import app.main  # noqa: E402  -- must precede the import below
import app.auth_gates  # noqa: E402


# ---------------------------------------------------------------------------
# ``_request_ip`` behavior -- loopback honors X-Forwarded-For, else direct.
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

    Behind a reverse proxy the direct connection is loopback (proxy -> app)
    but the real client IP is in XFF. ``_request_ip`` must strip and pick
    the first hop.
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
    """IPv6 loopback (``::1``) + XFF must return the first XFF hop, same as IPv4."""
    import app.auth_gates as auth_gates
    request = _FakeRequest("::1", {"x-forwarded-for": "203.0.113.5"})
    assert auth_gates._request_ip(request) == "203.0.113.5"


def test_request_ip_ignores_xff_when_direct_is_non_loopback():
    """Non-loopback direct + spoofed XFF -> returns the DIRECT, NOT XFF.

    A hostile external client cannot spoof their IP via XFF if the connection
    is direct to the app. Only proxy-fronted loopback connections trust XFF.
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
    """A request with ``request.client = None`` -> returns ``'unknown'``."""
    import app.auth_gates as auth_gates
    request = _FakeRequest(None, {})
    assert auth_gates._request_ip(request) == "unknown"
