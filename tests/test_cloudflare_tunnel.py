from __future__ import annotations

import logging
import subprocess
import sys
import time

from app.cloudflare_tunnel import (
    NO_AUTOUPDATE_FLAG,
    TUNNEL_HEALTHY_UPTIME_SECONDS,
    TUNNEL_RESTART_BACKOFF_MIN_SECONDS,
    CloudflareTunnelManager,
    CloudflareTunnelSecretStore,
    CloudflareTunnelSettings,
    SubprocessCloudflared,
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
    assert settings.token == "env-token"
    assert settings.source == "environment"
    assert settings.autostart is True


def test_local_virtualenv_binary_fallback(monkeypatch, tmp_path) -> None:
    local_binary = tmp_path / "bin" / "cloudflared"
    local_binary.parent.mkdir()
    local_binary.write_bytes(b"binary")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr("app.cloudflare_tunnel.shutil.which", lambda _name: None)
    settings = resolve_cloudflare_tunnel_settings({}, {}, {"PATH": ""})
    assert settings.binary == str(local_binary)


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
    manager.stop()


def test_spawn_passes_no_autoupdate_and_captures_output(monkeypatch) -> None:
    """cloudflared must not rewrite its own binary underneath the supervisor.

    The self-update replaces the binary and exits, which is what produced a
    dead tunnel (error 1033) on the host this was diagnosed from.
    """
    recorded: dict[str, object] = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            recorded["args"] = args
            recorded["kwargs"] = kwargs
            self.stdout = None
            self.returncode = 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    connector = SubprocessCloudflared("cloudflared")
    connector.start("secret-token")

    # --no-autoupdate belongs to ``tunnel``: after ``run`` cloudflared rejects
    # it ("flag provided but not defined") and exits before connecting.
    assert recorded["args"] == ["cloudflared", "tunnel", NO_AUTOUPDATE_FLAG, "run"]
    kwargs = recorded["kwargs"]
    # Output must be piped, never discarded to DEVNULL.
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["env"]["TUNNEL_TOKEN"] == "secret-token"


def test_connector_output_is_logged_with_token_redacted(caplog) -> None:
    """Connector output reaches app.log, but never the tunnel token."""
    import io

    connector = SubprocessCloudflared("cloudflared")
    stream = io.BytesIO(b"Registered tunnel connection\nleaked secret-token here\n")
    stream.close = lambda: None  # type: ignore[method-assign]

    class Stub:
        stdout = stream

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    connector._process = Stub()  # type: ignore[assignment]
    with caplog.at_level(logging.INFO):
        connector._drain_output("secret-token")

    assert "Registered tunnel connection" in caplog.text
    assert "secret-token" not in caplog.text
    assert "<redacted>" in caplog.text


def test_supervisor_restarts_a_dead_connector() -> None:
    """A connector that exits on its own is respawned without operator action."""
    created: list[FakeProcess] = []

    def factory() -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("secret-token", "environment", True, "cloudflared"),
        process_factory=factory,
        supervisor_interval=0.01,
    )
    try:
        manager.start()
        assert len(created) == 1
        first = created[0]

        # Simulate the self-update exit that caused the original outage, which
        # comes after the connector has been up for a while.
        manager._started_at = time.monotonic() - TUNNEL_HEALTHY_UPTIME_SECONDS
        first.running = False
        manager._supervise_once()

        assert len(created) == 2, "a dead connector must be respawned"
        assert created[1].running is True
        assert manager.status()["running"] is True
    finally:
        manager.stop()


def test_supervisor_does_not_resurrect_after_explicit_stop() -> None:
    """An operator stop must stick; the watchdog cannot undo it."""
    created: list[FakeProcess] = []

    def factory() -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("secret-token", "environment", True, "cloudflared"),
        process_factory=factory,
        supervisor_interval=0.01,
    )
    try:
        manager.start()
        manager.stop()
        count_after_stop = len(created)
        manager._supervise_once()
        assert len(created) == count_after_stop
        assert manager.status()["running"] is False
    finally:
        manager.stop()


def test_clearing_the_token_stops_supervision() -> None:
    created: list[FakeProcess] = []

    def factory() -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("secret-token", "database", True, "cloudflared"),
        process_factory=factory,
        supervisor_interval=0.01,
    )
    try:
        manager.start()
        manager.configure(None, source="database", autostart=False)
        count = len(created)
        manager._supervise_once()
        assert len(created) == count
        assert manager.status()["running"] is False
    finally:
        manager.stop()


def test_repeated_restart_failures_back_off() -> None:
    """A permanently broken setup must not spin the respawn loop forever."""
    attempts = {"count": 0}

    class AlwaysFailing(FakeProcess):
        def start(self, token: str) -> None:
            attempts["count"] += 1
            raise OSError("binary unavailable")

    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("secret-token", "environment", True, "cloudflared"),
        process_factory=AlwaysFailing,
        supervisor_interval=0.01,
    )
    try:
        manager._desired_running = True
        first = attempts["count"]
        manager._supervise_once()
        assert attempts["count"] == first + 1
        assert manager._restart_backoff == TUNNEL_RESTART_BACKOFF_MIN_SECONDS * 2

        # A backoff window must suppress the next attempt entirely.
        manager._supervise_once()
        assert attempts["count"] == first + 1
    finally:
        manager.stop()


def test_supervisor_thread_exits_promptly_on_stop() -> None:
    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("secret-token", "environment", True, "cloudflared"),
        process_factory=FakeProcess,
        supervisor_interval=30.0,
    )
    manager.start()
    thread = manager._supervisor_thread
    assert thread is not None and thread.is_alive()
    manager.stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "supervisor must not linger after stop()"
    # Allow the supervisor thread's own wait to be interruptible.
    assert time.monotonic() >= 0


def test_connector_that_dies_at_once_backs_off() -> None:
    """Spawning succeeds but the connector exits straight away (bad token,
    bad arguments): that must back off, not respawn on every tick."""
    created: list[FakeProcess] = []

    def factory() -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("secret-token", "environment", True, "cloudflared"),
        process_factory=factory,
        supervisor_interval=30.0,
    )
    try:
        manager.start()
        created[-1].running = False
        manager._supervise_once()
        assert len(created) == 1, "an early exit waits out the backoff"
        assert manager._restart_backoff == TUNNEL_RESTART_BACKOFF_MIN_SECONDS * 2
        assert "exiting shortly after it starts" in manager.status()["error"]

        manager._next_restart_at = 0.0  # backoff elapsed
        manager._supervise_once()
        assert len(created) == 2
        created[-1].running = False
        manager._supervise_once()
        assert len(created) == 2
        assert manager._restart_backoff == TUNNEL_RESTART_BACKOFF_MIN_SECONDS * 4

        # A connector that stays up resets the backoff.
        manager._next_restart_at = 0.0
        manager._supervise_once()
        manager._started_at = time.monotonic() - TUNNEL_HEALTHY_UPTIME_SECONDS
        manager._supervise_once()
        assert manager._restart_backoff == TUNNEL_RESTART_BACKOFF_MIN_SECONDS
    finally:
        manager.stop()


def test_failed_first_start_is_still_supervised() -> None:
    attempts = {"count": 0}

    class FailsOnce(FakeProcess):
        def start(self, token: str) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("binary briefly missing")
            super().start(token)

    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("secret-token", "environment", True, "cloudflared"),
        process_factory=FailsOnce,
        supervisor_interval=30.0,
    )
    try:
        assert manager.start()["running"] is False
        assert manager._supervisor_thread is not None and manager._supervisor_thread.is_alive()
        manager._next_restart_at = 0.0
        manager._supervise_once()
        assert manager.status()["running"] is True
    finally:
        manager.stop()


def test_restart_leaves_one_supervisor_thread() -> None:
    manager = CloudflareTunnelManager(
        CloudflareTunnelSettings("secret-token", "environment", True, "cloudflared"),
        process_factory=FakeProcess,
        supervisor_interval=30.0,
    )
    manager.start()
    old = manager._supervisor_thread
    with manager._lock:
        # The old thread may be blocked on the lock mid-check while the
        # restart runs; its own stop event must still end it afterwards.
        manager.restart()
    assert old is not None
    old.join(timeout=2.0)
    assert not old.is_alive()
    assert manager._supervisor_thread is not None and manager._supervisor_thread.is_alive()
    manager.stop()
