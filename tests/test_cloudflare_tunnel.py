from __future__ import annotations

import logging

from app.cloudflare_tunnel import (
    CloudflareTunnelManager,
    CloudflareTunnelSecretStore,
    CloudflareTunnelSettings,
    resolve_cloudflare_tunnel_settings,
)


class FakeProcess:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.started_tokens: list[str] = []
        self.stop_count = 0

    def start(self, token: str) -> None:
        self.started_tokens.append(token)
        self.running = True

    def stop(self) -> None:
        self.stop_count += 1
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def pid(self) -> int | None:
        return 4242 if self.running else None


def test_environment_token_wins_and_auto_starts() -> None:
    settings = resolve_cloudflare_tunnel_settings(
        {"cloudflare_tunnel": {"autostart": False}},
        {"token": "db-token", "autostart": False},
        {"DAYGLE_CLOUDFLARED_TOKEN": " env-token ", "PATH": ""},
    )
    assert settings == CloudflareTunnelSettings("env-token", "environment", True, "cloudflared")


def test_persisted_and_config_fallbacks() -> None:
    persisted = resolve_cloudflare_tunnel_settings({}, {"token": "db-token", "autostart": True}, {})
    assert persisted.token == "db-token"
    assert persisted.source == "database"
    assert persisted.autostart is True

    configured = resolve_cloudflare_tunnel_settings(
        {"cloudflare_tunnel": {"token": "yaml-token", "autostart": True}}, {}, {}
    )
    assert configured.token == "yaml-token"
    assert configured.source == "config"


def test_secret_store_uses_private_file(tmp_path) -> None:
    store = CloudflareTunnelSecretStore(tmp_path / "data" / "daygle.sqlite3")
    store.write("secret-token")
    assert store.read() == "secret-token"
    if not hasattr(tmp_path, 'drive') or str(tmp_path).startswith('/'):
        assert (store.path.stat().st_mode & 0o777) == 0o600
    store.clear()
    assert store.read() is None


def test_lifecycle_status_never_contains_token(caplog) -> None:
    process = FakeProcess()
    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("super-secret-token", "database", False, "cloudflared"),
        process=process,
    )
    status = manager.start()
    assert status["running"] is True
    assert status["pid"] == 4242
    assert "super-secret-token" not in repr(status)
    assert process.started_tokens == ["super-secret-token"]

    process.running = False
    with caplog.at_level(logging.WARNING):
        status = manager.status()
    assert status["running"] is False
    assert "super-secret-token" not in caplog.text

    stopped = manager.stop()
    assert stopped["running"] is False


def test_failed_process_start_is_nonfatal() -> None:
    class FailingProcess(FakeProcess):
        def start(self, token: str) -> None:
            raise OSError(f"binary unavailable for {token}")

    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("secret-token", "environment", True, "cloudflared"),
        process_factory=FailingProcess,
    )
    status = manager.start()
    assert status["running"] is False
    assert status["configured"] is True
    assert "secret-token" not in str(status)
