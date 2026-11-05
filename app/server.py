"""Production Uvicorn launcher with tunnel-safe binding and proxy-header trust."""
from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import uvicorn
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.cloudflare_tunnel import CloudflareTunnelSecretStore, resolve_cloudflare_tunnel_settings
from app.settings import load_settings

# Loopback is always trusted for forwarded headers: the bundled cloudflared
# connector and a same-host reverse proxy both connect from here, and Uvicorn's
# own default is the same posture. A remote proxy is added on top of this set
# from ``auth.trusted_proxies`` (see ``forwarded_allow_ips``).
_LOOPBACK_FORWARDED_HOSTS = ("127.0.0.1", "::1")
_DEFAULT_DATABASE_PATH = "data/daygle_ai_camera.sqlite3"


def _persisted_setting(database_path: str | Path, key: str) -> dict[str, Any]:
    """Read one JSON ``app_settings`` row before FastAPI has initialised its DB."""
    try:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        value = json.loads(row[0]) if row else {}
        return value if isinstance(value, dict) else {}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # A first boot has no app_settings table yet. Startup must remain
        # available even when an optional setting cannot be inspected.
        return {}


def _persisted_tunnel_settings(database_path: str | Path) -> dict[str, Any]:
    """Read only the tunnel setting before FastAPI has initialised its DB."""
    return _persisted_setting(database_path, "cloudflare_tunnel")


def _config_entries(value: Any) -> list[str]:
    """Split a comma-separated string or list setting into trimmed entries."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _valid_proxy_entries(entries: list[str]) -> list[str]:
    """Keep only parseable IPs/CIDRs, de-duplicated in input order.

    The settings validator already rejects invalid entries, but the YAML file
    is operator-edited and this runs before that validation. Dropping malformed
    values (and therefore ``*``) keeps Uvicorn's trust set identical to the
    app's own ``is_trusted_proxy`` check instead of silently trusting everyone.
    """
    valid: list[str] = []
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if entry not in valid:
            valid.append(entry)
    return valid


def forwarded_allow_ips(config: dict[str, Any] | None = None) -> str:
    """Return Uvicorn's comma-separated ``forwarded_allow_ips`` value.

    Uvicorn's ``ProxyHeadersMiddleware`` applies ``X-Forwarded-Proto`` (and
    ``X-Forwarded-For``) only for peers listed here. Deriving the list from
    ``auth.trusted_proxies`` -- the same setting the app uses for its origin
    and client-IP checks -- keeps the external request URL, HSTS, audit
    addresses, and login rate limiting consistent for a reverse proxy on
    another host instead of leaving ``request.url`` on the internal
    ``http://`` address.

    The UI-saved auth row overrides the YAML block, matching
    ``effective_auth_config``. Loopback is always appended so the tunnel
    connector and a same-host proxy keep working. The value is read once at
    process start, so a trusted-proxy change applies on the next restart.
    """
    config = config if config is not None else load_settings()
    config = config if isinstance(config, dict) else {}
    auth_config = config.get("auth", {})
    if not isinstance(auth_config, dict):
        auth_config = {}
    storage = config.get("storage", {})
    storage = storage if isinstance(storage, dict) else {}
    database_path = storage.get("database", _DEFAULT_DATABASE_PATH)
    persisted_auth = _persisted_setting(database_path, "auth")
    trusted = persisted_auth.get("trusted_proxies", auth_config.get("trusted_proxies"))
    entries = _valid_proxy_entries(_config_entries(trusted))
    for entry in _LOOPBACK_FORWARDED_HOSTS:
        if entry not in entries:
            entries.append(entry)
    return ",".join(entries)


def server_host(config: dict[str, Any] | None = None) -> str:
    """Return the Uvicorn bind address for this run.

    By default the app keeps serving the configured ``server.host``
    (typically ``0.0.0.0``) even when a Cloudflare Tunnel token is
    configured, so LAN clients and the connector coexist -- the natural
    posture for a fresh install and for split-DNS deployments (e.g. OPNsense
    HAProxy for LAN clients plus the tunnel for external clients). Operators
    who want the tunnel to be the ONLY ingress set
    ``server.tunnel_loopback_only: true`` to force loopback (``127.0.0.1``).

    Environment configuration is available before Uvicorn starts; the DB
    lookup covers a previously saved UI token for systemd/service launches.
    """
    config = config if config is not None else load_settings()
    server_config = config.get("server", {}) if isinstance(config, dict) else {}
    storage = config.get("storage", {}) if isinstance(config, dict) else {}
    persisted = _persisted_tunnel_settings(storage.get("database", _DEFAULT_DATABASE_PATH))
    token_store = CloudflareTunnelSecretStore(storage.get("database", _DEFAULT_DATABASE_PATH))
    tunnel = resolve_cloudflare_tunnel_settings(config, persisted, persisted_token=token_store.read())
    # A UI-saved ``tunnel_loopback_only`` in app_settings (the LAN & Proxy
    # Access settings card) overrides the YAML bootstrap default.
    loopback_only = bool(server_config.get("tunnel_loopback_only", False))
    if isinstance(persisted, dict) and "tunnel_loopback_only" in persisted:
        loopback_only = bool(persisted.get("tunnel_loopback_only"))
    if tunnel.token and loopback_only:
        return "127.0.0.1"
    return str(server_config.get("host", "0.0.0.0"))


# A bare hostname / IPv4 / bracketed IPv6, optionally with a port. Anything
# else (spaces, paths, userinfo, several values) is ignored rather than used.
_FORWARDED_HOST_RE = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::[0-9]{1,5})?$")

# Set by ``main()`` before Uvicorn calls ``create_asgi_app``; the factory
# re-derives it when it is built some other way (e.g. a test).
_forwarded_allow_ips: str | None = None


class ForwardedHostMiddleware:
    """Apply ``X-Forwarded-Host`` from a trusted proxy to the ``Host`` header.

    Uvicorn's ``ProxyHeadersMiddleware`` applies ``X-Forwarded-Proto`` and
    ``X-Forwarded-For`` but not ``X-Forwarded-Host``, and it replaces the
    client address with the real client's. After that, the app can no longer
    see that the request came through a trusted proxy, so its own
    ``X-Forwarded-Host`` handling (same-origin check) never runs. A proxy that
    rewrites ``Host`` (nginx's default ``$proxy_host``) would then fail the
    origin check on every mutating request.

    This middleware runs OUTSIDE ``ProxyHeadersMiddleware``, while the direct
    peer is still visible, and only for peers in the same trusted list. It
    takes the first (original) entry and replaces ``Host`` with it, so
    ``request.url`` carries the external host.
    """

    def __init__(self, app: Any, trusted_hosts: str) -> None:
        self.app = app
        self._networks = []
        for entry in _config_entries(trusted_hosts):
            try:
                self._networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                continue

    def _is_trusted(self, peer: Any) -> bool:
        try:
            address = ipaddress.ip_address(str(peer).split("%", 1)[0])
        except ValueError:
            return False
        return any(address in network for network in self._networks)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            client = scope.get("client")
            if client and self._is_trusted(client[0]):
                forwarded = None
                for name, value in scope.get("headers", []):
                    if name == b"x-forwarded-host":
                        forwarded = value
                        break
                if forwarded is not None:
                    host = forwarded.decode("latin-1").split(",")[0].strip()
                    if _FORWARDED_HOST_RE.match(host):
                        headers = [(n, v) for n, v in scope["headers"] if n != b"host"]
                        headers.append((b"host", host.encode("latin-1")))
                        scope = dict(scope, headers=headers)
        await self.app(scope, receive, send)


def create_asgi_app() -> Any:
    """Uvicorn factory: the FastAPI app behind the forwarded-header middleware.

    Order matters: ``ForwardedHostMiddleware`` must be outermost so it sees the
    proxy's address before ``ProxyHeadersMiddleware`` replaces it with the
    client's. Uvicorn's own ``proxy_headers`` option is therefore turned off in
    ``main()`` and the proxy middleware is applied here instead, with the same
    trusted list.
    """
    from app.main import app as fastapi_app

    trusted = _forwarded_allow_ips if _forwarded_allow_ips is not None else forwarded_allow_ips()
    return ForwardedHostMiddleware(ProxyHeadersMiddleware(fastapi_app, trusted_hosts=trusted), trusted)


def main() -> None:
    global _forwarded_allow_ips
    config = load_settings()
    server_config = config.get("server", {})
    _forwarded_allow_ips = forwarded_allow_ips(config)
    uvicorn.run(
        "app.server:create_asgi_app",
        factory=True,
        host=server_host(config),
        port=int(server_config.get("port", 8080)),
        reload=False,
        # Applied by ``create_asgi_app`` (with X-Forwarded-Host support), so
        # Uvicorn must not wrap the app in a second copy.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
