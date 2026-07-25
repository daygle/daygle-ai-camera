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


def test_request_ip_picks_first_xff_hop_for_multi_hop_chain():
    """Per RFC 7239 / XFF convention, the leftmost XFF entry is the original
    client and subsequent entries are intermediate proxy hops.

    Reverse proxies (Nginx, Traefik, Cloudflare, AWS ALB) typically APPEND
    their IP to the right, so ``[-1]`` is the most recent proxy hop, not
    the client. Bug pre-fix: ``_request_ip`` returned ``[-1].strip()``,
    giving the rate-limiter / audit log the proxy IP rather than the actual
    attacker IP -- which both poisoned the rate-limit source attribution
    AND gave attackers a single IP (the trusted proxy) to DoS while
    rotating their real IPs in the upstream hops.
    """
    import app.auth_gates as auth_gates
    # Caller is the trusted proxy; the XFF chain represents client -> proxyA
    # -> proxyB -> trusted-proxy -> app. The original client is the
    # leftmost entry.
    request = _FakeRequest("127.0.0.1", {"x-forwarded-for": "203.0.113.5, 10.0.0.1, 10.0.0.2, 127.0.0.1"})
    assert auth_gates._request_ip(request) == "203.0.113.5"


def test_request_ip_tolerates_whitespace_around_first_xff_hop():
    """Some proxies insert a space after the comma (``"1.2.3.4, 10.0.0.1"``);
    the first hop must still be extracted correctly.
    """
    import app.auth_gates as auth_gates
    request = _FakeRequest("127.0.0.1", {"x-forwarded-for": " 1.2.3.4 , 10.0.0.1"})
    assert auth_gates._request_ip(request) == "1.2.3.4"


def test_request_ip_falls_back_to_direct_when_first_xff_hop_is_empty():
    """A malformed XFF header whose first hop is empty (``", 10.0.0.1"``)
    falls back to the direct connection (the trusted-proxy IP), rather than
    returning the empty string. Defensive against header-parser quirks.
    """
    import app.auth_gates as auth_gates
    request = _FakeRequest("10.0.0.5", {"x-forwarded-for": ", 10.0.0.1"})
    assert auth_gates._request_ip(request) == "10.0.0.5"


# ---------------------------------------------------------------------------
# ``trusted_proxies`` config -- explicit trust list beyond loopback.
# ---------------------------------------------------------------------------


def test_request_ip_honors_trusted_proxies_config_for_non_loopback():
    """When ``trusted_proxies`` is configured to include a non-loopback peer,
    X-Forwarded-For from that peer IS trusted; non-listed peers fall back to
    the direct connection.

    Guards the Bug-A fix: previously ``_request_ip`` trusted XFF whenever the
    direct peer was in ``_LOOPBACK`` (``{'127.0.0.1', '::1', 'localhost'}``),
    which exposed the app to client-side IP spoofing when bound to a
    non-loopback interface (Docker bridge, LAN). The fix delegates trust to
    ``auth.trusted_proxies`` so an admin/operator must explicitly whitelist
    the upstream proxy IP.
    """
    import app.auth_gates as auth_gates
    import app.state as _state
    saved_auth_config = dict(_state.auth_config)
    _state.auth_config["trusted_proxies"] = ["10.0.0.5"]
    try:
        # 10.0.0.5 IS in trusted_proxies -- XFF honoured.
        request = _FakeRequest("10.0.0.5", {"x-forwarded-for": "192.0.2.42"})
        assert auth_gates._request_ip(request) == "192.0.2.42"

        # 127.0.0.1 NOT in trusted_proxies -- XFF ignored, direct returned.
        request = _FakeRequest("127.0.0.1", {"x-forwarded-for": "192.0.2.42"})
        assert auth_gates._request_ip(request) == "127.0.0.1"
    finally:
        _state.auth_config.clear()
        _state.auth_config.update(saved_auth_config)


def test_request_ip_accepts_trusted_proxies_as_comma_separated_string():
    """Operators may write ``trusted_proxies`` as a comma-separated string in YAML.

    Defends the convenience path: some operators prefer keeping proxy lists
    in YAML config strings; the helper must parse this form identically to
    the list form.
    """
    import app.auth_gates as auth_gates
    import app.state as _state
    saved_auth_config = dict(_state.auth_config)
    _state.auth_config["trusted_proxies"] = "10.0.0.5, 192.168.1.1 , ::1"
    try:
        request = _FakeRequest("192.168.1.1", {"x-forwarded-for": "203.0.113.7"})
        assert auth_gates._request_ip(request) == "203.0.113.7"
    finally:
        _state.auth_config.clear()
        _state.auth_config.update(saved_auth_config)


def test_request_ip_falls_back_to_loopback_when_trusted_proxies_missing():
    """When ``trusted_proxies`` is absent from both DB and YAML overrides,
    the helper falls back to ``{'127.0.0.1', '::1'}`` -- matching the legacy
    behaviour unit-tested by the five tests above.

    Critical: unit tests must not regress into AttributeError when
    ``_state.database`` is None and the auth config has no proxy key.
    """
    import app.auth_gates as auth_gates
    import app.state as _state
    saved_auth_config = dict(_state.auth_config)
    _state.auth_config.pop("trusted_proxies", None)
    try:
        request = _FakeRequest("127.0.0.1", {"x-forwarded-for": "203.0.113.7"})
        assert auth_gates._request_ip(request) == "203.0.113.7"

        # Non-loopback without explicit trust -> direct, no XFF.
        request = _FakeRequest("198.51.100.7", {"x-forwarded-for": "1.2.3.4"})
        assert auth_gates._request_ip(request) == "198.51.100.7"
    finally:
        _state.auth_config.clear()
        _state.auth_config.update(saved_auth_config)
