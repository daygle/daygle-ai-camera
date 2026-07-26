"""Regression tests for two undefined-name crash bugs found by static analysis.

Both were latent ``NameError`` bugs on error-handling paths that pyflakes
flags but the normal happy-path test suite never exercised:

1. ``app.auth.AuthService.authenticate`` -- the best-effort ``login_attempts``
   audit-write ``except`` handler referenced ``_logging``, a name only
   imported *locally* inside ``cleanup_expired_sessions``. If the audit INSERT
   ever failed (disk full, locked DB, dropped table) the handler raised
   ``NameError`` instead of logging, converting a best-effort breadcrumb into
   a full login-path 500 -- the exact outcome the handler's comment says it
   must avoid.

2. ``app.api.auth_router.setup`` -- the setup-endpoint rate-limit branch
   raised ``HTTPException`` without importing it, so a throttled setup POST
   raised ``NameError`` instead of returning ``429 Retry-After``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_authenticate_survives_audit_write_failure(tmp_path, monkeypatch):
    """A failing ``login_attempts`` write must not turn a bad-password login
    into a ``NameError`` -- the caller should still see ``AuthError``."""
    import contextlib

    from app.auth import AuthService, AuthError

    db_path = str(tmp_path / "auth.sqlite3")
    auth = AuthService(db_path, {})
    auth.create_user("alice", "Str0ng-Pass!", role="admin")

    import sqlite3

    real_connect = auth.connect

    class _ConnProxy:
        """Delegates everything to the real connection but fails ONLY the
        best-effort audit INSERT (in the ``finally``), leaving the SELECT /
        UPDATE in the ``try`` intact so control reaches the ``except``
        handler that held the bug."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if "INSERT INTO login_attempts" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextlib.contextmanager
    def failing_audit_connect():
        with real_connect() as db:
            yield _ConnProxy(db)

    monkeypatch.setattr(auth, "connect", failing_audit_connect)

    with pytest.raises(AuthError):
        auth.authenticate("alice", "wrong-password", "127.0.0.1")


def test_setup_rate_limited_raises_http_429(monkeypatch, tmp_path):
    """A rate-limited setup POST must raise ``HTTPException(429)`` -- not
    ``NameError`` from an unimported symbol."""
    from fastapi import HTTPException
    from starlette.requests import Request

    import app.api.auth_router as auth_router
    from app.auth import AuthService

    auth = AuthService(str(tmp_path / "auth.sqlite3"), {})

    # Force the throttle so the guarded branch runs.
    monkeypatch.setattr(
        auth_router.setup_limiter, "is_rate_limited", lambda key: True
    )

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/setup",
        "headers": [],
        "client": ("203.0.113.7", 12345),
        "query_string": b"",
    }
    request = Request(scope)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(auth_router.setup(request, auth=auth, auth_enabled=True))
    assert excinfo.value.status_code == 429


def test_setup_rate_limited_writes_audit_row_without_spurious_warning(
    monkeypatch, tmp_path, caplog
):
    """The rate-limited setup path must persist its audit row and NOT log a
    'Setup audit-log write failed' warning.

    Regression for the ``EventDatabase.close()`` bug: ``EventDatabase`` has no
    ``close()`` method, so the old ``finally: db_for_audit.close()`` raised
    ``AttributeError`` on every hit, which the outer handler mislogged as an
    audit-write failure even though the row was written."""
    import logging

    from fastapi import HTTPException
    from starlette.requests import Request

    import app.api.auth_router as auth_router
    from app.auth import AuthService
    from app.database import EventDatabase

    db_path = str(tmp_path / "auth.sqlite3")
    auth = AuthService(db_path, {})
    monkeypatch.setattr(
        auth_router.setup_limiter, "is_rate_limited", lambda key: True
    )

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/setup",
        "headers": [],
        "client": ("203.0.113.7", 12345),
        "query_string": b"",
    }
    request = Request(scope)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException):
            asyncio.run(auth_router.setup(request, auth=auth, auth_enabled=True))

    assert "Setup audit-log write failed" not in caplog.text
    rows = EventDatabase(db_path).list_audit_logs(limit=10)
    assert any(r["action"] == "setup_rate_limited" for r in rows)
