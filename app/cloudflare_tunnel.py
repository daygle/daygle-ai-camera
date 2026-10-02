"""Single Cloudflare Tunnel connector lifecycle.

The application owns one ``cloudflared tunnel run`` process. The token is
passed through the child environment rather than its command line so it does
not appear in ordinary process listings. Persisted tokens live in a dedicated
0600 file next to the SQLite database; ``app_settings`` stores only metadata.
This module deliberately contains no FastAPI imports so it remains unit-testable.

Two safeguards keep the tunnel alive. ``--no-autoupdate`` stops cloudflared
replacing its own binary and exiting, and a supervisor thread respawns the
connector after any other exit. Connector output is forwarded to the
application log (with the token redacted) so a tunnel failure is diagnosable
from the host.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


CLOUDFLARED_TOKEN_ENV = "DAYGLE_CLOUDFLARED_TOKEN"
CLOUDFLARED_BINARY_ENV = "DAYGLE_CLOUDFLARED_BINARY"
DEFAULT_CLOUDFLARED_BINARY = "cloudflared"
MAX_TUNNEL_TOKEN_LENGTH = 4096
TOKEN_FILE_NAME = "cloudflare_tunnel.token"

# cloudflared replaces its own binary on its periodic update tick and then
# EXITS, on the documented assumption that a service manager restarts it. This
# application spawns the connector as a plain child with no restart policy, so
# that exit used to leave a dead tunnel (Cloudflare error 1033) until a human
# noticed. ``--no-autoupdate`` stops cloudflared rewriting itself underneath a
# supervisor we control; version changes are handled by install_cloudflared.sh.
NO_AUTOUPDATE_FLAG = "--no-autoupdate"

# How often the supervisor checks a connector that should be alive, and the
# bounded backoff applied between restart attempts when a respawn keeps dying
# (a bad token, a missing binary, or a blocked network all look like this).
TUNNEL_SUPERVISOR_INTERVAL_SECONDS = 30.0
TUNNEL_RESTART_BACKOFF_MIN_SECONDS = 5.0
TUNNEL_RESTART_BACKOFF_MAX_SECONDS = 300.0

# Connectors that stay up this long are considered healthy, so the backoff
# resets and the next failure gets a fast first retry again.
TUNNEL_HEALTHY_UPTIME_SECONDS = 60.0

logger = logging.getLogger("daygle.ai")


@dataclass(frozen=True)
class CloudflareTunnelSettings:
    token: str | None
    source: str
    autostart: bool
    binary: str


def _normalise_token(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def tunnel_token_path(database_path: str | Path) -> Path:
    """Return the private token path associated with the application DB."""
    return Path(database_path).expanduser().resolve().parent / TOKEN_FILE_NAME


class CloudflareTunnelSecretStore:
    """Small 0600 token-file store; no token is returned by API status code."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = tunnel_token_path(database_path)

    def read(self) -> str | None:
        try:
            token = _normalise_token(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return None
        return token if token and len(token) <= MAX_TUNNEL_TOKEN_LENGTH else None

    def write(self, token: str) -> None:
        normalized = _normalise_token(token)
        if not normalized or len(normalized) > MAX_TUNNEL_TOKEN_LENGTH:
            raise ValueError("Invalid Cloudflare Tunnel token")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Use a replacement file so an existing token is never briefly exposed
        # with a permissive mode on umask configurations such as 000.
        temporary = self.path.with_suffix(".tmp")
        temporary.unlink(missing_ok=True)
        # Create the file 0600 from the first byte: ``write_text`` would create
        # it with the process umask (typically 0644, world-readable) and only
        # the later chmod would narrow it, briefly exposing the token.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(normalized + "\n")
        try:
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def resolve_cloudflare_tunnel_settings(
    config: Mapping[str, Any] | None = None,
    persisted: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    persisted_token: str | None = None,
) -> CloudflareTunnelSettings:
    """Resolve tunnel settings with environment variables taking precedence.

    ``persisted`` is metadata from ``app_settings``. ``persisted_token`` is
    read separately from the strict-permission secret file. The legacy
    ``persisted['token']`` shape remains accepted for one-way compatibility,
    but new writes never put a token in SQLite.
    """
    config = config or {}
    persisted = persisted or {}
    environ = environ or os.environ
    config_block = config.get("cloudflare_tunnel")
    if not isinstance(config_block, Mapping):
        config_block = {}

    env_token = _normalise_token(environ.get(CLOUDFLARED_TOKEN_ENV))
    stored_token = _normalise_token(persisted_token) or _normalise_token(persisted.get("token"))
    config_token = _normalise_token(config_block.get("token"))
    if env_token:
        token, source = env_token, "environment"
        autostart = True
    elif stored_token:
        token, source = stored_token, "database"
        autostart = bool(persisted.get("autostart", False))
    elif config_token:
        token, source = config_token, "config"
        autostart = bool(config_block.get("autostart", False))
    else:
        token, source, autostart = None, "none", False

    configured_binary = str(
        environ.get(CLOUDFLARED_BINARY_ENV)
        or config_block.get("binary")
        or ""
    ).strip()
    if configured_binary and configured_binary != DEFAULT_CLOUDFLARED_BINARY:
        binary = configured_binary
    elif shutil.which(DEFAULT_CLOUDFLARED_BINARY):
        binary = DEFAULT_CLOUDFLARED_BINARY
    else:
        candidates = (
            Path(sys.prefix) / "bin" / DEFAULT_CLOUDFLARED_BINARY,
            Path.home() / ".local" / "bin" / DEFAULT_CLOUDFLARED_BINARY,
            Path(__file__).resolve().parent.parent / "bin" / DEFAULT_CLOUDFLARED_BINARY,
        )
        binary = next(
            (str(candidate) for candidate in candidates if candidate.is_file()),
            DEFAULT_CLOUDFLARED_BINARY,
        )
    return CloudflareTunnelSettings(token, source, autostart, binary)


def _redact_token(text: str, token: str | None) -> str:
    """Strip the tunnel token from connector output before it reaches a log.

    cloudflared does not normally echo the token, but it is passed to the child
    through the environment and any crash dump or verbose line is untrusted
    input; a single leaked line in app.log would expose the connector secret.
    """
    if not token:
        return text
    return text.replace(token, "<redacted>")


class CloudflaredProcess(Protocol):
    """Small process interface used by ``CloudflareTunnelManager`` tests."""

    def start(self, token: str) -> None: ...
    def stop(self) -> None: ...
    def is_running(self) -> bool: ...
    def pid(self) -> int | None: ...


class SubprocessCloudflared:
    """Production cloudflared process implementation.

    Connector stdout/stderr are drained into the application log instead of
    ``DEVNULL``. Discarding them made every failure mode indistinguishable: a
    token rejection, a DNS failure, and the self-update exit all produced an
    identical empty journal, which is why a dead tunnel could not be diagnosed
    from the host at all.
    """

    def __init__(self, binary: str = DEFAULT_CLOUDFLARED_BINARY) -> None:
        self.binary = binary
        self._process: subprocess.Popen[bytes] | None = None
        self._output_thread: threading.Thread | None = None

    def start(self, token: str) -> None:
        child_env = os.environ.copy()
        child_env["TUNNEL_TOKEN"] = token
        self._process = subprocess.Popen(
            [self.binary, "tunnel", "run", NO_AUTOUPDATE_FLAG],
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=(os.name != "nt"),
        )
        self._output_thread = threading.Thread(
            target=self._drain_output,
            args=(token,),
            name="cloudflared-output",
            daemon=True,
        )
        self._output_thread.start()

    def _drain_output(self, token: str) -> None:
        """Forward connector output to the application log until it exits."""
        process = self._process
        stream = process.stdout if process is not None else None
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = _redact_token(raw.decode("utf-8", "replace").strip(), token)
                if line:
                    logger.info("cloudflared: %s", line)
        except (OSError, ValueError):
            # The pipe closes on terminate/kill; a read failure here is the
            # normal end of a connector shutdown, not an application error.
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()
        # EOF means the connector exited. Log the status: a deliberate
        # self-update exit reports a small non-zero code, while a signal death
        # reports a negative value, and the two need different responses.
        exit_code = process.poll() if process is not None else None
        if exit_code is not None:
            logger.warning("cloudflared exited with status %s.", exit_code)

    def stop(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None


class CloudflareTunnelManager:
    """Thread-safe lifecycle wrapper for one cloudflared connector."""

    def __init__(
        self,
        settings: CloudflareTunnelSettings | None = None,
        process: CloudflaredProcess | None = None,
        process_factory: Any | None = None,
        *,
        supervisor_interval: float = TUNNEL_SUPERVISOR_INTERVAL_SECONDS,
    ) -> None:
        self._lock = threading.RLock()
        self._token: str | None = settings.token if settings else None
        self._source = settings.source if settings else "none"
        self._autostart = settings.autostart if settings else False
        self._binary = settings.binary if settings else DEFAULT_CLOUDFLARED_BINARY
        self._process = process
        self._process_factory = process_factory or (
            (lambda process=process: process) if process is not None
            else (lambda: SubprocessCloudflared(self._binary))
        )
        self._last_error: str | None = None
        # ``_desired_running`` records operator intent, separately from whether
        # a connector is currently alive. The supervisor only respawns while
        # this is True, so an explicit stop() is never undone by the watchdog
        # and a deliberate shutdown does not fight a restart.
        self._desired_running = False
        self._supervisor_interval = supervisor_interval
        self._supervisor_stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        self._restart_backoff = TUNNEL_RESTART_BACKOFF_MIN_SECONDS
        self._restart_attempts = 0
        self._next_restart_at = 0.0
        self._started_at: float | None = None

    def configure(self, token: str | None, *, source: str = "database", autostart: bool = False) -> None:
        with self._lock:
            self._token = _normalise_token(token)
            self._source = source if self._token else "none"
            self._autostart = bool(autostart) if self._token else False
            if not self._token:
                # Clearing the token is an explicit request to take the tunnel
                # down, so drop supervisor intent before releasing the process.
                self._desired_running = False
                self._shutdown_supervisor_locked()
                if self._process is not None:
                    self._process.stop()
                    self._process = None

    def start(self) -> dict[str, Any]:
        with self._lock:
            self._desired_running = True
            # An explicit start is a fresh operator intent: clear any backoff
            # left over from earlier failures so it takes effect immediately.
            self._restart_backoff = TUNNEL_RESTART_BACKOFF_MIN_SECONDS
            self._restart_attempts = 0
            self._next_restart_at = 0.0
            started = self._start_locked()
            if started:
                self._ensure_supervisor_locked()
            return self.status()

    def _start_locked(self) -> bool:
        """Spawn the connector. Caller must hold ``self._lock``.

        Returns True when a connector is running afterwards. Failures stay
        non-fatal: the local service must keep serving even when the tunnel
        cannot start.
        """
        if not self._token:
            self._last_error = "No Cloudflare Tunnel token is configured."
            return False
        if self._process is not None and self._process.is_running():
            return True
        self._last_error = None
        try:
            self._process = self._process_factory()
            self._process.start(self._token)
        except Exception as exc:
            # Exception text can be supplied by a child-process wrapper;
            # never trust it to be free of the token.
            self._process = None
            self._last_error = f"Unable to start cloudflared ({type(exc).__name__})."
            logger.warning("Cloudflare Tunnel is unavailable: %s", self._last_error)
            return False
        self._started_at = time.monotonic()
        return True

    def _supervise(self) -> None:
        """Respawn the connector whenever it dies while it should be running.

        cloudflared exits on its own in several normal situations -- most
        notably its own self-update, which replaces the binary and exits
        expecting a supervisor. Without this loop every one of those exits left
        Cloudflare reporting error 1033 indefinitely.
        """
        while not self._supervisor_stop.wait(self._supervisor_interval):
            try:
                self._supervise_once()
            except Exception as exc:  # pragma: no cover - defensive
                # The watchdog must never die itself; a bug here would silently
                # disable tunnel supervision for the rest of the process life.
                logger.warning(
                    "Cloudflare Tunnel supervisor check failed (%s).", type(exc).__name__
                )

    def _supervise_once(self) -> None:
        with self._lock:
            if not self._desired_running or not self._token:
                return
            if self._process is not None and self._process.is_running():
                # Uptime-based backoff reset: a connector that stayed up long
                # enough is healthy, so a later failure retries promptly.
                if (
                    self._started_at is not None
                    and time.monotonic() - self._started_at >= TUNNEL_HEALTHY_UPTIME_SECONDS
                ):
                    self._restart_backoff = TUNNEL_RESTART_BACKOFF_MIN_SECONDS
                return
            now = time.monotonic()
            if now < self._next_restart_at:
                return
            if self._process is not None:
                # Drop the exited child so repeated restarts do not accumulate
                # unreaped zombies; the connector's own exit status has already
                # been logged by its output pump.
                self._process = None
            logger.warning(
                "Cloudflare Tunnel connector exited; restarting it (attempt %d).",
                self._restart_attempts + 1,
            )
            if self._start_locked():
                self._restart_attempts = 0
                self._restart_backoff = TUNNEL_RESTART_BACKOFF_MIN_SECONDS
                self._next_restart_at = 0.0
            else:
                # Still down: schedule the next attempt on a widening backoff
                # so a permanently broken setup (expired token, missing binary,
                # blocked egress) cannot spin the respawn loop.
                self._restart_attempts += 1
                self._restart_backoff = min(
                    self._restart_backoff * 2, TUNNEL_RESTART_BACKOFF_MAX_SECONDS
                )
                self._next_restart_at = time.monotonic() + self._restart_backoff
                logger.info(
                    "Cloudflare Tunnel still unavailable; next restart attempt in %.0fs.",
                    self._restart_backoff,
                )

    def _ensure_supervisor_locked(self) -> None:
        if self._supervisor_thread is not None and self._supervisor_thread.is_alive():
            return
        self._supervisor_stop.clear()
        self._supervisor_thread = threading.Thread(
            target=self._supervise, name="cloudflare-tunnel-supervisor", daemon=True
        )
        self._supervisor_thread.start()

    def _shutdown_supervisor_locked(self) -> None:
        # Never join here: this runs while holding ``self._lock``, which the
        # supervisor thread also acquires. The thread waits on an Event, so
        # setting the flag is enough for it to exit on its own.
        self._supervisor_stop.set()
        self._supervisor_thread = None

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._desired_running = False
            self._shutdown_supervisor_locked()
            if self._process is not None:
                try:
                    self._process.stop()
                except Exception as exc:
                    self._last_error = f"Unable to stop cloudflared ({type(exc).__name__})."
                    logger.warning("Cloudflare Tunnel stop failed: %s", self._last_error)
                finally:
                    self._process = None
                    self._started_at = None
            return self.status()

    def shutdown(self) -> None:
        """Stop the connector and the supervisor for application shutdown."""
        self.stop()

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start()

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._process is not None and self._process.is_running()
            if self._process is not None and not running and self._last_error is None:
                self._last_error = "cloudflared exited unexpectedly."
                logger.warning("Cloudflare Tunnel stopped unexpectedly.")
            return {
                "configured": self._token is not None,
                "source": self._source,
                "autostart": self._autostart,
                "running": running,
                "pid": self._process.pid() if running and self._process is not None else None,
                "binary": self._binary,
                "error": self._last_error,
                "supervised": self._desired_running,
            }

    @property
    def autostart(self) -> bool:
        with self._lock:
            return self._autostart
