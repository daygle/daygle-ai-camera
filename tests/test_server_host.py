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
    """``main()`` runs the factory (which applies the proxy middleware itself)
    with the derived trust list, and turns Uvicorn's own copy off."""
    import app.server as server

    captured: dict = {}
    cfg = _config()
    cfg["auth"] = {"trusted_proxies": ["10.0.0.5"]}
    monkeypatch.setattr(server, "load_settings", lambda: cfg)
    monkeypatch.setattr(server, "_forwarded_allow_ips", None)
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: captured.update(kwargs, app=args[0]))

    server.main()

    assert captured["app"] == "app.server:create_asgi_app"
    assert captured["factory"] is True
    assert captured["proxy_headers"] is False
    assert server._forwarded_allow_ips == "10.0.0.5,127.0.0.1,::1"


# ---------------------------------------------------------------------------
# X-Forwarded-Host from a trusted proxy (``ForwardedHostMiddleware``)
#
# Uvicorn's ProxyHeadersMiddleware rewrites the client address but ignores
# X-Forwarded-Host, so after it runs the app can no longer tell the request
# came through a trusted proxy. A proxy that rewrites Host (nginx's default)
# then failed the same-origin check on every mutating request.
# ---------------------------------------------------------------------------


def _scope(client: str, headers: list[tuple[bytes, bytes]]) -> dict:
    return {
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/x", "raw_path": b"/api/x", "query_string": b"", "root_path": "",
        "headers": headers, "client": (client, 40000), "server": ("10.0.0.20", 8080),
    }


def _run_chain(scope: dict, trusted: str = "10.0.0.5,127.0.0.1,::1") -> dict:
    """Run *scope* through the same middleware stack ``create_asgi_app`` builds."""
    import asyncio

    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.server import ForwardedHostMiddleware

    seen: dict = {}

    async def inner(inner_scope, receive, send):
        seen["scope"] = inner_scope

    app = ForwardedHostMiddleware(ProxyHeadersMiddleware(inner, trusted_hosts=trusted), trusted)
    asyncio.run(app(scope, None, None))
    return seen["scope"]


_PROXY_HEADERS = [
    (b"host", b"10.0.0.20:8080"),
    (b"origin", b"https://cam.example.com"),
    (b"x-forwarded-for", b"192.168.1.50"),
    (b"x-forwarded-proto", b"https"),
    (b"x-forwarded-host", b"cam.example.com"),
]


def test_trusted_proxy_rewritten_host_passes_same_origin_check(monkeypatch) -> None:
    import app.middleware as middleware
    import app.state as state
    from starlette.requests import Request

    monkeypatch.setattr(state, "auth_config", {"trusted_proxies": ["10.0.0.5"]}, raising=False)
    monkeypatch.setattr(state, "database", None, raising=False)
    scope = _run_chain(_scope("10.0.0.5", list(_PROXY_HEADERS)))
    request = Request(scope)

    assert scope["client"][0] == "192.168.1.50"
    assert str(request.url) == "https://cam.example.com/api/x"
    assert middleware._check_same_origin(request) == (True, "")


def test_untrusted_peer_cannot_set_host_via_x_forwarded_host() -> None:
    scope = _run_chain(_scope("192.168.1.99", list(_PROXY_HEADERS)))
    assert dict(scope["headers"])[b"host"] == b"10.0.0.20:8080"
    assert scope["client"][0] == "192.168.1.99"


def test_forwarded_host_uses_first_entry_and_keeps_port() -> None:
    headers = [(b"host", b"10.0.0.20:8080"), (b"x-forwarded-host", b"cam.example.com:8443, inner.lan")]
    scope = _run_chain(_scope("10.0.0.5", headers))
    assert [v for n, v in scope["headers"] if n == b"host"] == [b"cam.example.com:8443"]


def test_malformed_forwarded_host_is_ignored() -> None:
    for bad in (b"evil.com/path", b"user@evil.com", b"a b", b""):
        headers = [(b"host", b"10.0.0.20:8080"), (b"x-forwarded-host", bad)]
        scope = _run_chain(_scope("10.0.0.5", headers))
        assert dict(scope["headers"])[b"host"] == b"10.0.0.20:8080", bad


def test_forwarded_host_trusts_cidr_and_ipv6_loopback() -> None:
    headers = [(b"host", b"internal"), (b"x-forwarded-host", b"[2001:db8::1]:443")]
    scope = _run_chain(_scope("::1", headers), trusted="192.168.1.0/24,127.0.0.1,::1")
    assert dict(scope["headers"])[b"host"] == b"[2001:db8::1]:443"
    scope = _run_chain(_scope("192.168.1.7", [(b"host", b"internal"), (b"x-forwarded-host", b"cam.lan")]),
                       trusted="192.168.1.0/24,127.0.0.1,::1")
    assert dict(scope["headers"])[b"host"] == b"cam.lan"


def test_create_asgi_app_wraps_fastapi_app_outermost_first(monkeypatch) -> None:
    import app.server as server
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setattr(server, "_forwarded_allow_ips", "10.0.0.5,127.0.0.1,::1")
    wrapped = server.create_asgi_app()
    assert isinstance(wrapped, server.ForwardedHostMiddleware)
    assert isinstance(wrapped.app, ProxyHeadersMiddleware)
