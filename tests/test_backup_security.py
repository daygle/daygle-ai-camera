"""C2 hardening tests for ``app/backup.py::overwrite_database_from_file``.

The C2 fix in ``app/backup.py`` adds two defences to the global SQLite
restore path:

1. ``source.enable_load_extension(False)`` is called explicitly on the
   source connection BEFORE the schema scan. This is belt-and-braces on
   Python 3.12+ whose default is already ``False``; on older builds
   where extension loading is on by default this is the only thing
   preventing a stored trigger / view from autoloading native code.

2. ``sqlite_master`` is queried for VIEWs and TRIGGERs. Neither is used
   by the application under ``app/``; if either is present in the
   uploaded backup the restore is REJECTED with HTTP 400 before any
   backup-write to the live DB.

These tests exercise the ``overwrite_database_from_file`` function
directly with a tmp_path staging area, monkeypatching ``_state.database``
with a fake ``database_path`` so the hot-swap target is well-defined.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Preload app.main so ``app.backup``'s reach via ``app.state`` resolves
# (the same preload pattern used by the rest of the test suite).
import app.main  # noqa: E402  -- intentional preload  # lgtm[py/unused-import]
import app.state as _state  # noqa: E402
import app.backup as backup_module  # noqa: E402


def _make_app_shaped_db(path: Path) -> None:
    """Create a minimal Daygle-shaped SQLite DB at ``path``.

    Includes the four tables ``validate_restore_database`` requires
    (``users``, ``events``, ``detections``, ``app_settings``) plus the
    one row of admin that the same validator checks. We add them here
    so the C2 hardening path can be exercised end-to-end.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE users(
                id INTEGER PRIMARY KEY,
                username TEXT,
                password_hash TEXT,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO users (id, username, password_hash, role, is_active)
                VALUES (1, 'admin', 'fakehash', 'admin', 1);
            CREATE TABLE events(id INTEGER PRIMARY KEY, label TEXT);
            CREATE TABLE detections(id INTEGER PRIMARY KEY, label TEXT);
            CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _add_view(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        # A view whose body is harmless SELECT; the schema scan should
        # still reject because the application contract forbids views.
        conn.execute('CREATE VIEW v_admin AS SELECT id, username FROM users')
        conn.commit()
    finally:
        conn.close()


def _add_trigger(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        # Trigger body is a no-op SELECT; schema scan rejects because
        # the application contract forbids triggers.
        conn.execute(
            'CREATE TRIGGER t_admin AFTER INSERT ON events '
            "BEGIN SELECT 1; END"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def live_db_path(tmp_path: Path) -> Path:
    live = tmp_path / 'live.sqlite3'
    _make_app_shaped_db(live)
    return live


def test_overwrite_database_from_file_passes_clean_db(tmp_path, monkeypatch, live_db_path):
    """A backup with only the four required tables must restore cleanly.

    Asserts the post-restore live DB still contains the admin row, so
    the backup() call actually wrote successfully.
    """
    monkeypatch.setattr(_state, 'database', SimpleNamespace(database_path=live_db_path))
    source = tmp_path / 'source.sqlite3'
    _make_app_shaped_db(source)
    backup_module.overwrite_database_from_file(source)
    conn = sqlite3.connect(str(live_db_path))
    try:
        rows = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        assert rows == 1
        admin = conn.execute(
            "SELECT role FROM users WHERE role = 'admin' AND is_active = 1"
        ).fetchall()
        assert admin, 'admin row should survive the restore'
    finally:
        conn.close()


def test_overwrite_database_from_file_rejects_view(tmp_path, monkeypatch, live_db_path):
    """A backup with a VIEW must fail restore BEFORE the schema is swapped.

    Guard against the stored-SQL-in-view vector (e.g., a view body that
    wraps ``SELECT load_extension('/tmp/evil.so')``).
    """
    monkeypatch.setattr(_state, 'database', SimpleNamespace(database_path=live_db_path))
    source = tmp_path / 'source_view.sqlite3'
    _make_app_shaped_db(source)
    _add_view(source)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        backup_module.overwrite_database_from_file(source)
    assert exc_info.value.status_code == 400
    assert 'view' in exc_info.value.detail.lower(), exc_info.value.detail

    # The live DB should NOT contain the view (sanity-check the rejection
    # ran before the backup was attempted).
    conn = sqlite3.connect(str(live_db_path))
    try:
        view_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view'"
        ).fetchall()
        assert view_rows == [], (
            f'Rejected restore still wrote views into the live DB: {view_rows}'
        )
    finally:
        conn.close()


def test_overwrite_database_from_file_rejects_trigger(tmp_path, monkeypatch, live_db_path):
    """A backup with a TRIGGER must fail restore BEFORE the schema is swapped."""
    monkeypatch.setattr(_state, 'database', SimpleNamespace(database_path=live_db_path))
    source = tmp_path / 'source_trigger.sqlite3'
    _make_app_shaped_db(source)
    _add_trigger(source)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        backup_module.overwrite_database_from_file(source)
    assert exc_info.value.status_code == 400
    assert 'trigger' in exc_info.value.detail.lower(), exc_info.value.detail

    conn = sqlite3.connect(str(live_db_path))
    try:
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        assert triggers == [], (
            f'Rejected restore still wrote triggers into the live DB: {triggers}'
        )
    finally:
        conn.close()


def _add_app_audit_triggers(path: Path) -> None:
    """Add the application's OWN immutable audit-log triggers, exactly as
    ``EventDatabase.init`` creates them (every real backup carries these)."""
    from app.database import AUDIT_LOG_IMMUTABLE_TRIGGERS
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_log("
            "id INTEGER PRIMARY KEY, immutable INTEGER NOT NULL DEFAULT 1)"
        )
        conn.executescript('\n'.join(AUDIT_LOG_IMMUTABLE_TRIGGERS.values()))
        conn.commit()
    finally:
        conn.close()


def test_overwrite_database_from_file_accepts_own_audit_triggers(
    tmp_path, monkeypatch, live_db_path,
):
    """A legitimate backup carries the application's own immutable audit-log
    triggers; the restore validator must ALLOWLIST them (otherwise no backup
    this app produces could ever be restored)."""
    monkeypatch.setattr(_state, 'database', SimpleNamespace(database_path=live_db_path))
    source = tmp_path / 'source_own_triggers.sqlite3'
    _make_app_shaped_db(source)
    _add_app_audit_triggers(source)

    # Must NOT raise -- the own triggers are allowlisted.
    backup_module.overwrite_database_from_file(source)
    conn = sqlite3.connect(str(live_db_path))
    try:
        assert conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1
    finally:
        conn.close()


def test_overwrite_database_from_file_rejects_name_spoofed_audit_trigger(
    tmp_path, monkeypatch, live_db_path,
):
    """An attacker cannot smuggle a payload under a trusted trigger NAME:
    a trigger named like an allowlisted one but with a different (malicious)
    body must still be rejected, since the allowlist matches on body."""
    monkeypatch.setattr(_state, 'database', SimpleNamespace(database_path=live_db_path))
    source = tmp_path / 'source_spoofed.sqlite3'
    _make_app_shaped_db(source)
    conn = sqlite3.connect(str(source))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_log("
            "id INTEGER PRIMARY KEY, immutable INTEGER NOT NULL DEFAULT 1)"
        )
        # Trusted NAME, hostile BODY (would autoload native code on restore).
        conn.execute(
            "CREATE TRIGGER trg_audit_log_immutable_delete "
            "AFTER INSERT ON audit_log "
            "BEGIN SELECT load_extension('/tmp/evil.so'); END"
        )
        conn.commit()
    finally:
        conn.close()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        backup_module.overwrite_database_from_file(source)
    assert exc_info.value.status_code == 400
    assert 'trigger' in exc_info.value.detail.lower(), exc_info.value.detail


def test_overwrite_database_from_file_calls_enable_load_extension_false(
    tmp_path, monkeypatch, live_db_path,
):
    """Defence-in-depth: ``source.enable_load_extension(False)`` is called
    on the connection used for backup. Even if the view/trigger scan is
    later bypassed (e.g., a future regression), any ``SELECT load_extension(...)``
    in stored SQL on the same connection will fail because the connection
    itself is locked down.
    """
    monkeypatch.setattr(_state, 'database', SimpleNamespace(database_path=live_db_path))
    source = tmp_path / 'source.sqlite3'
    _make_app_shaped_db(source)

    captured: list[bool] = []

    # ``sqlite3.Connection`` is an immutable C type on modern CPython, so its
    # ``enable_load_extension`` method cannot be monkeypatched in place. Spy via
    # a Connection SUBCLASS injected through ``sqlite3.connect(factory=...)``
    # instead -- this works across Python versions and still records every
    # disable call made on the source connection.
    class _SpyConnection(sqlite3.Connection):
        def enable_load_extension(self, value):  # noqa: ANN001
            if value is False:  # only record the disable call (it must always be present)
                captured.append(value)
            return super().enable_load_extension(value)

    real_connect = sqlite3.connect

    def _spy_connect(database, *args, **kwargs):  # noqa: ANN001
        kwargs.setdefault('factory', _SpyConnection)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(backup_module.sqlite3, 'connect', _spy_connect)
    backup_module.overwrite_database_from_file(source)

    assert captured, (
        'overwrite_database_from_file must explicitly call '
        'sqlite3.Connection.enable_load_extension(False) on the source '
        'connection before the schema scan / backup write'
    )
    assert captured[0] is False
