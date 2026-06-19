"""Database backup and recording-purge helpers extracted from ``app/main.py`` (Phase-J).

Cluster membership:
- ``DATABASE_RESTORE_REQUIRED_TABLES`` — set of table names required for a valid restore
- ``DATABASE_RESTORE_LOCK`` — process-wide lock serialising database overwrites
- ``backup_directory()`` — compute (and mkdir) the data/backups directory
- ``safe_backup_timestamp()`` — UTC timestamp string for backup filenames
- ``create_database_backup(prefix)`` — SQLite online-backup to a timestamped file
- ``validate_restore_database(path)`` — integrity-check an uploaded database file
- ``overwrite_database_from_file(restore_source)`` — hot-swap the live database
- ``refresh_runtime_after_database_restore()`` — re-init singletons after restore
- ``purge_recordings_by_policy(*, force)`` — age-out old recordings + disk files
- ``purge_camera_diagnostics_by_policy()`` — age-out old camera-log rows

Pool-C reach (resolved lazily via lazy imports inside function bodies):
- ``app.main.apply_cameras_settings`` (``refresh_runtime_after_database_restore``)
- ``app.main.apply_storage_and_recording_settings`` (``refresh_runtime_after_database_restore``)
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.config_facades import (
    effective_auth_config,
    effective_cameras_config,
    effective_recording_config,
    effective_storage_config,
)
from app.recording_extension import delete_recording_files
from app.utils import normalize_bool_setting

logger = logging.getLogger('daygle.ai')

DATABASE_RESTORE_REQUIRED_TABLES: set[str] = {'events', 'detections', 'app_settings', 'users'}
DATABASE_RESTORE_LOCK: threading.Lock = threading.Lock()


def backup_directory() -> Path:
    backups_dir = Path(str(effective_storage_config().get('data_dir') or 'data')) / 'backups'
    backups_dir.mkdir(parents=True, exist_ok=True)
    return backups_dir


def safe_backup_timestamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def create_database_backup(prefix: str = 'daygle-database') -> Path:
    backup_path = backup_directory() / f'{prefix}-{safe_backup_timestamp()}-{secrets.token_hex(4)}.sqlite3'
    try:
        source = sqlite3.connect(_state.database.database_path)
        try:
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def validate_restore_database(path: Path) -> None:
    try:
        db = sqlite3.connect(path)
        try:
            integrity = db.execute('PRAGMA integrity_check').fetchone()
            if not integrity or str(integrity[0]).lower() != 'ok':
                raise HTTPException(status_code=400, detail='Uploaded database failed SQLite integrity check.')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
            missing = sorted(DATABASE_RESTORE_REQUIRED_TABLES - tables)
            if missing:
                raise HTTPException(status_code=400, detail=f"Uploaded database is missing required table(s): {', '.join(missing)}.")
            admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()[0]
            if int(admin_count) < 1:
                raise HTTPException(status_code=400, detail='Uploaded database must include at least one active administrator account.')
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        raise HTTPException(status_code=400, detail='Uploaded file is not a valid SQLite database.') from exc


def overwrite_database_from_file(restore_source: Path) -> None:
    source = sqlite3.connect(str(restore_source))
    try:
        destination = sqlite3.connect(str(_state.database.database_path))
        try:
            source.backup(destination)
            checkpoint = destination.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint and checkpoint[0] != 0:
                logger.warning('Database restore WAL checkpoint returned error code %s', checkpoint[0])
        finally:
            destination.close()
    finally:
        source.close()


def refresh_runtime_after_database_restore() -> None:
    from app.main import apply_cameras_settings, apply_storage_and_recording_settings
    _state.database.init()
    _state.auth.init()
    apply_cameras_settings(effective_cameras_config())
    apply_storage_and_recording_settings()
    _state.auth.apply_config(effective_auth_config())


def purge_recordings_by_policy(*, force: bool = False) -> dict[str, Any]:
    recording_settings = effective_recording_config()
    if not force and (not normalize_bool_setting(recording_settings.get('auto_purge_enabled', True), True)):
        return {'purged': 0, 'files_deleted': 0, 'bytes_deleted': 0, 'recordings': []}
    retention_days = int(recording_settings.get('retention_days', 14))
    max_storage_gb = int(recording_settings.get('max_storage_gb', 20))
    older_than = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    max_storage_bytes = max_storage_gb * 1024 * 1024 * 1024
    purged = _state.database.purge_recordings(older_than=older_than, max_storage_bytes=max_storage_bytes)
    bytes_deleted = 0
    files_deleted = 0
    for recording in purged:
        file_path = Path(str(recording.get('file_path') or ''))
        if file_path.exists() and file_path.is_file():
            bytes_deleted += file_path.stat().st_size
            files_deleted += 1
    delete_recording_files(purged)
    return {'purged': len(purged), 'files_deleted': files_deleted, 'bytes_deleted': bytes_deleted, 'recordings': purged}


def purge_camera_diagnostics_by_policy() -> int:
    """Age out old camera diagnostic events.

    Two bounds keep the log from growing without limit: a hard row cap enforced
    on every insert (see EventDatabase.add_camera_diagnostic) and this
    time-based purge. Retention follows the same recording retention window
    (``retention_days``) so diagnostics age out alongside the recordings they
    explain.
    """
    try:
        retention_days = max(1, int(effective_recording_config().get('retention_days', 14)))
        older_than = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        return _state.database.purge_camera_diagnostics_older_than(older_than)
    except Exception as exc:
        logger.debug('Camera diagnostics purge failed: %s', exc)
        return 0
