"""Single Cloudflare Tunnel connector lifecycle.

The application owns one ``cloudflared tunnel run`` process. The token is
passed through the child environment rather than its command line so it does
not appear in ordinary process listings. Persisted tokens live in a dedicated
0600 file next to the SQLite database; ``app_settings`` stores only metadata.
This module deliberately contains no FastAPI imports so it remains unit-testable.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

CLOUDFLARED_TOKEN_ENV = "DAYGLE_CLOUDFLARED_TOKEN"
CLOUDFLARED_BINARY_ENV = "DAYGLE_CLOUDFLARED_BINARY"
DEFAULT_CLOUDFLARED_BINARY = "cloudflared"
MAX_TUNNEL_TOKEN_LENGTH = 4096
TOKEN_FILE_NAME = "cloudflare_tunnel.token"

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
        temporary.write_text(normalized + "\n", encoding="utf-8")
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


class CloudflaredProcess(Protocol):
    """Small process interface used by ``CloudflareTunnelManager`` tests."""

    def start(self, token: str) -> None: ...
    def stop(self) -> None: ...
    def is_running(self) -> bool: ...
    def pid(self) -> int | None: ...


class SubprocessCloudflared:
    """Production cloudflared process implementation."""

    def __init__(self, binary: str = DEFAULT_CLOUDFLARED_BINARY) -> None:
        self.binary = binary
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, token: str) -> None:
        child_env = os.environ.copy()
        child_env["TUNNEL_TOKEN"] = token
        self._process = subprocess.Popen(
            [self.binary, "tunnel", "run"],
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=(os.name != "nt"),
        )

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

    def configure(self, token: str | None, *, source: str = "database", autostart: bool = False) -> None:
        with self._lock:
            self._token = _normalise_token(token)
            self._source = source if self._token else "none"
            self._autostart = bool(autostart) if self._token else False
            if not self._token and self._process is not None:
                self._process.stop()
                self._process = None

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not self._token:
                self._last_error = "No Cloudflare Tunnel token is configured."
                return self.status()
            if self._process is not None and self._process.is_running():
                return self.status()
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
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None:
                try:
                    self._process.stop()
                except Exception as exc:
                    self._last_error = f"Unable to stop cloudflared ({type(exc).__name__})."
                    logger.warning("Cloudflare Tunnel stop failed: %s", self._last_error)
                finally:
                    self._process = None
            return self.status()

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
            }

    @property
    def autostart(self) -> bool:
        with self._lock:
            return self._autostart
