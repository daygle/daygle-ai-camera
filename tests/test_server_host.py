"""Unit tests for the Uvicorn bind-address decision (``app/server.py``).

``server_host`` must default to loopback whenever a Cloudflare Tunnel token is
configured (the tunnel is the only ingress), and must honour
``server.tunnel_loopback_only: false`` so a LAN reverse proxy / split-DNS
front-end can reach the same origin while the tunnel stays active.
"""

from __future__ import annotations

from typing import Any

from app.server import server_host

# A database path under a directory that cannot exist keeps the sqlite and
# secret-store probes from creating real files during the unit test.
_UNREACHABLE_DB = "no-such-dir-xyz/daygle_ai_camera.sqlite3"


def _config(
    host: str = "0.0.0.0",
    *,
    tunnel_loopback_only: bool | None = None,
    tunnel_token: str | None = None,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "server": {"host": host, "port": 8080},
        "storage": {"database": _UNREACHABLE_DB},
    }
    if tunnel_loopback_only is not None:
        cfg["server"]["tunnel_loopback_only"] = tunnel_loopback_only
    if tunnel_token is not None:
        cfg["cloudflare_tunnel"] = {"token": tunnel_token}
    return cfg


def test_no_tunnel_uses_configured_host() -> None:
    assert server_host(_config(host="0.0.0.0")) == "0.0.0.0"
    assert server_host(_config(host="127.0.0.1")) == "127.0.0.1"
    assert server_host(_config(host="192.168.20.145")) == "192.168.20.145"


def test_no_tunnel_defaults_to_all_interfaces() -> None:
    assert server_host(_config()) == "0.0.0.0"


def test_tunnel_token_defaults_to_loopback() -> None:
    assert server_host(_config(host="0.0.0.0", tunnel_token="tok")) == "127.0.0.1"


def test_tunnel_token_with_loopback_only_disabled_keeps_host() -> None:
    cfg = _config(host="0.0.0.0", tunnel_loopback_only=False, tunnel_token="tok")
    assert server_host(cfg) == "0.0.0.0"

    cfg = _config(host="192.168.20.145", tunnel_loopback_only=False, tunnel_token="tok")
    assert server_host(cfg) == "192.168.20.145"


def test_explicit_loopback_host_is_respected() -> None:
    assert (
        server_host(_config(host="127.0.0.1", tunnel_loopback_only=False))
        == "127.0.0.1"
    )
    # Even with the opt-out, an explicit loopback host stays loopback.
    assert (
        server_host(_config(host="127.0.0.1", tunnel_loopback_only=False, tunnel_token="tok"))
        == "127.0.0.1"
    )
