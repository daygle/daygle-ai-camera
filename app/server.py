"""Production Uvicorn launcher with Cloudflare Tunnel-safe binding."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import uvicorn

from app.cloudflare_tunnel import CloudflareTunnelSecretStore, resolve_cloudflare_tunnel_settings
from app.settings import load_settings


def _persisted_tunnel_settings(database_path: str | Path) -> dict[str, Any]:
    """Read only the tunnel setting before FastAPI has initialised its DB."""
    try:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'cloudflare_tunnel'"
            ).fetchone()
        value = json.loads(row[0]) if row else {}
        return value if isinstance(value, dict) else {}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # A first boot has no app_settings table yet. LAN startup must remain
        # available even when the optional tunnel setting cannot be inspected.
        return {}


def server_host(config: dict[str, Any] | None = None) -> str:
    """Return the Uvicorn bind address for this run.

    By default, a configured Cloudflare Tunnel token forces loopback
    (``127.0.0.1``) so the connector is the only ingress. Operators who front
    the same origin with a LAN reverse proxy / split-DNS setup (e.g. OPNsense
    HAProxy for LAN clients plus the tunnel for external clients) can opt out
    with ``server.tunnel_loopback_only: false`` to keep the configured
    ``server.host`` (typically ``0.0.0.0``) while the tunnel is active.

    Environment configuration is available before Uvicorn starts; the DB
    lookup covers a previously saved UI token for systemd/service launches.
    """
    config = config if config is not None else load_settings()
    server_config = config.get("server", {}) if isinstance(config, dict) else {}
    storage = config.get("storage", {}) if isinstance(config, dict) else {}
    persisted = _persisted_tunnel_settings(storage.get("database", "data/daygle_ai_camera.sqlite3"))
    token_store = CloudflareTunnelSecretStore(storage.get("database", "data/daygle_ai_camera.sqlite3"))
    tunnel = resolve_cloudflare_tunnel_settings(config, persisted, persisted_token=token_store.read())
    # A UI-saved ``tunnel_loopback_only`` in app_settings (the Cloudflare
    # Tunnel settings card) overrides the YAML bootstrap default.
    loopback_only = bool(server_config.get("tunnel_loopback_only", True))
    if isinstance(persisted, dict) and "tunnel_loopback_only" in persisted:
        loopback_only = bool(persisted.get("tunnel_loopback_only"))
    if tunnel.token and loopback_only:
        return "127.0.0.1"
    return str(server_config.get("host", "0.0.0.0"))


def main() -> None:
    config = load_settings()
    server_config = config.get("server", {})
    uvicorn.run(
        "app.main:app",
        host=server_host(config),
        port=int(server_config.get("port", 8080)),
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
