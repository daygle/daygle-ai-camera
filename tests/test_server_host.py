"""Unit tests for the Uvicorn bind-address decision (``app/server.py``).

``server_host`` must default to the configured host (LAN serving) even when a
Cloudflare Tunnel token is configured -- a fresh install keeps LAN access --
and must honour ``server.tunnel_loopback_only: true`` so operators can force
loopback (the tunnel is the only ingress), including a UI-saved toggle.
"""

from __future__ import annotations

from typing import Any

from app.server import forwarded_allow_ips, server_host

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


def test_tunnel_token_defaults_to_lan_serving() -> None:
    # A token alone does NOT cut off LAN access: a fresh install keeps the
    # configured host by default (tunnel_loopback_only defaults to false).
    assert server_host(_config(host="0.0.0.0", tunnel_token="tok")) == "0.0.0.0"
    assert server_host(_config(host="192.168.20.145", tunnel_token="tok")) == "192.168.20.145"


def test_tunnel_token_with_loopback_only_enabled_binds_loopback() -> None:
    # tunnel_loopback_only: true forces the tunnel-only ingress.
    assert (
        server_host(_config(host="0.0.0.0", tunnel_loopback_only=True, tunnel_token="tok"))
        == "127.0.0.1"
    )


def test_tunnel_token_with_loopback_only_false_keeps_host() -> None:
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


def test_persisted_tunnel_loopback_only_overrides_yaml(tmp_path) -> None:
    """A UI-saved value in app_settings wins over the YAML bootstrap default."""
    import json
    import sqlite3

    db_path = tmp_path / "data" / "daygle_ai_camera.sqlite3"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )
        connection.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('cloudflare_tunnel', ?, '')",
            (json.dumps({"tunnel_loopback_only": False}),),
        )

    # YAML says loopback-only, but the UI toggle says serve the LAN.
    cfg = _config(
        host="0.0.0.0",
        tunnel_loopback_only=True,
        tunnel_token="tok",
    )
    cfg["storage"] = {"database": str(db_path)}
    assert server_host(cfg) == "0.0.0.0"

    # The reverse: UI locked to loopback overrides a YAML opt-out.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE app_settings SET value = ? WHERE key = 'cloudflare_tunnel'",
            (json.dumps({"tunnel_loopback_only": True}),),
        )
    cfg = _config(host="0.0.0.0", tunnel_loopback_only=False, tunnel_token="tok")
    cfg["storage"] = {"database": str(db_path)}
    assert server_host(cfg) == "127.0.0.1"


# ---------------------------------------------------------------------------
# Forwarded-header trust (``forwarded_allow_ips``)
#
# Uvicorn's ProxyHeadersMiddleware only applies X-Forwarded-Proto/-For from the
# listed peers. The launcher derives that list from ``auth.trusted_proxies``
# (the setting the app's own origin/client checks use) plus loopback, so a
# remote reverse proxy produces an external request URL and HSTS works.
# ---------------------------------------------------------------------------


def test_forwarded_allow_ips_defaults_to_loopback() -> None:
    assert forwarded_allow_ips(_config()) == "127.0.0.1,::1"


def test_forwarded_allow_ips_includes_configured_reverse_proxy() -> None:
    cfg = _config()
    cfg["auth"] = {"trusted_proxies": ["10.0.0.5", "192.168.1.0/24"]}
    assert forwarded_allow_ips(cfg) == "10.0.0.5,192.168.1.0/24,127.0.0.1,::1"


def test_forwarded_allow_ips_accepts_comma_separated_string() -> None:
    cfg = _config()
    cfg["auth"] = {"trusted_proxies": " 10.0.0.5 , 192.168.1.0/24 ,"}
    assert forwarded_allow_ips(cfg) == "10.0.0.5,192.168.1.0/24,127.0.0.1,::1"


def test_forwarded_allow_ips_drops_invalid_and_wildcard_entries() -> None:
    # A YAML typo must not widen trust, and "*" is deliberately unsupported:
    # the app's own is_trusted_proxy() would never honour it.
    cfg = _config()
    cfg["auth"] = {"trusted_proxies": ["not-an-ip", "*", "10.0.0.5", "10.0.0.5"]}
    assert forwarded_allow_ips(cfg) == "10.0.0.5,127.0.0.1,::1"


def test_forwarded_allow_ips_does_not_duplicate_loopback() -> None:
    cfg = _config()
    cfg["auth"] = {"trusted_proxies": ["127.0.0.1", "::1", "10.0.0.5"]}
    assert forwarded_allow_ips(cfg) == "127.0.0.1,::1,10.0.0.5"


def test_forwarded_allow_ips_uses_ui_saved_trusted_proxies(tmp_path) -> None:
    """A DB row saved from the LAN & Proxy Access card wins over YAML."""
    import json
    import sqlite3

    db_path = tmp_path / "data" / "daygle_ai_camera.sqlite3"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )
        connection.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('auth', ?, '')",
            (json.dumps({"trusted_proxies": ["10.9.8.7"]}),),
        )

    cfg = _config()
    cfg["auth"] = {"trusted_proxies": ["10.0.0.1"]}
    cfg["storage"] = {"database": str(db_path)}
    assert forwarded_allow_ips(cfg) == "10.9.8.7,127.0.0.1,::1"


def test_main_wires_the_derived_forwarded_allow_ips(monkeypatch) -> None:
    """``main()`` must pass the derived list (and keep proxy headers on)."""
    import app.server as server

    captured: dict = {}
    cfg = _config()
    cfg["auth"] = {"trusted_proxies": ["10.0.0.5"]}
    monkeypatch.setattr(server, "load_settings", lambda: cfg)
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: captured.update(kwargs))

    server.main()

    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "10.0.0.5,127.0.0.1,::1"
