"""Database backup and recording-purge helpers extracted from ``app/main.py`` (Phase-J).

Cluster membership:
- ``DATABASE_RESTORE_REQUIRED_TABLES`` - set of table names required for a valid restore
- ``DATABASE_RESTORE_LOCK`` - process-wide lock serialising database overwrites
- ``backup_directory()`` - compute (and mkdir) the data/backups directory
- ``safe_backup_timestamp()`` - UTC timestamp string for backup filenames
- ``create_database_backup(prefix)`` - SQLite online-backup to a timestamped file
- ``validate_restore_database(path)`` - integrity-check an uploaded database file
- ``overwrite_database_from_file(restore_source)`` - hot-swap the live database
- ``refresh_runtime_after_database_restore()`` - re-init singletons after restore
- ``purge_recordings_by_policy(*, force)`` - age-out old recordings + disk files
- ``purge_camera_diagnostics_by_policy()`` - age-out old camera-log rows

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
from app.database import AUDIT_LOG_IMMUTABLE_TRIGGERS
from app.recording_extension import delete_recording_files
from app.media_utils import safe_storage_path
from app.utils import normalize_bool_setting

logger = logging.getLogger('daygle.ai')

DATABASE_RESTORE_REQUIRED_TABLES: set[str] = {'events', 'detections', 'app_settings', 'users'}
DATABASE_RESTORE_LOCK: threading.Lock = threading.Lock()


def _normalize_ddl(sql: str | None) -> str:
    """Normalise a CREATE statement for allowlist comparison.

    Collapses runs of whitespace, drops the ``IF NOT EXISTS`` clause and the
    trailing statement terminator (both of which ``sqlite_master.sql`` strips)
    so a trigger stored in an uploaded backup compares equal to the canonical
    DDL regardless of formatting. Comparison is case-insensitive.
    """
    collapsed = ' '.join((sql or '').split())
    collapsed = collapsed.replace('CREATE TRIGGER IF NOT EXISTS', 'CREATE TRIGGER')
    return collapsed.rstrip(' ;').lower()


# Allowlist of the application's own triggers, keyed by name -> normalised
# body. A trigger in an uploaded backup is accepted only if BOTH its name and
# its normalised body match an entry here; every other trigger and all views
# are rejected by ``overwrite_database_from_file``.
_ALLOWED_TRIGGER_DDL: dict[str, str] = {
    name: _normalize_ddl(sql) for name, sql in AUDIT_LOG_IMMUTABLE_TRIGGERS.items()
}


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
        # N1 (round-5): verify backup integrity BEFORE declaring success so
        # torn writes, power losses, or filesystem mid-cycle faults don't get
        # silently accepted as valid backups. ``source.backup()`` performs
        # an internal page-by-page scan and copy, so the resulting
        # destination is by-construction physically consistent on disk;
        # ``PRAGMA integrity_check`` confirms the page tree + free lists
        # on the new file before we hand the path back to the caller.
        # Without this, corruption was only caught at the next
        # ``validate_restore_database`` upload (round-trip time may be
        # weeks or months).
        verify = sqlite3.connect(backup_path)
        try:
            integrity = verify.execute('PRAGMA integrity_check').fetchone()
            if not integrity or str(integrity[0]).lower() != 'ok':
                offending = '<none>' if not integrity else str(integrity[0])
                raise HTTPException(
                    status_code=500,
                    detail=f'Backup failed integrity check ({offending})',
                )
        finally:
            verify.close()
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def _snapshot_database_file(destination: Path) -> None:
    """Copy the live database to *destination* via SQLite's online backup API.

    ``source.backup()`` produces a by-construction consistent page-by-page copy
    even while the live connection is mid-write (WAL frames are folded in), so
    the archived database never captures a torn state -- the same mechanism the
    database-only backup relies on.
    """
    source = sqlite3.connect(str(_state.database.database_path))
    try:
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    # Mirror create_database_backup's verification: the online-backup API is
    # by-construction consistent, but confirming the page tree on the copy
    # catches torn storage before an archive is served as "valid".
    verify = sqlite3.connect(str(destination))
    try:
        integrity = verify.execute('PRAGMA integrity_check').fetchone()
        if not integrity or str(integrity[0]).lower() != 'ok':
            offending = '<none>' if not integrity else str(integrity[0])
            raise RuntimeError(f'Full backup database snapshot failed integrity check ({offending})')
    finally:
        verify.close()


def _archive_directory(
    archive,
    source_root: Path,
    arc_prefix: str,
    *,
    excluded_abs: set[str],
    excluded_names: frozenset[str],
) -> dict[str, int]:
    """Zip *source_root* into *archive* under ``arc_prefix/``.

    Symlinks at any depth are skipped rather than followed, so a planted link
    cannot pull external files into the archive. Directories in
    ``excluded_names`` (rolling ingest state, the backups dir) and any path in
    ``excluded_abs`` (the live database, the archive itself) are pruned. Media
    is already compressed, so files >= 1 MiB are stored verbatim instead of
    re-deflating gigabytes of video.
    """
    import os
    import zipfile

    manifest_section = {'files': 0, 'bytes': 0}
    if not source_root.exists():
        return manifest_section
    for dirpath, dirnames, filenames in os.walk(source_root, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            child = current / name
            if (
                name in excluded_names
                or child.is_symlink()
                or str(child.resolve(strict=False)) in excluded_abs
            ):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            child = current / name
            if child.is_symlink():
                continue
            try:
                rel = child.relative_to(source_root)
            except ValueError:
                continue
            arcname = f'{arc_prefix}/{rel.as_posix()}'
            try:
                size = child.stat().st_size
            except OSError:
                continue
            if size < 1024 * 1024:
                archive.write(child, arcname, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
            else:
                archive.write(child, arcname, compress_type=zipfile.ZIP_STORED)
            manifest_section['files'] += 1
            manifest_section['bytes'] += size
    return manifest_section


def create_full_backup(prefix: str = 'daygle-full') -> Path:
    """Create a downloadable zip containing the database, recordings, and snapshots.

    The archive is intentionally a zip (openable on Windows and Linux without
    extra tooling) and contains:

    * ``database/<name>.sqlite3`` - a consistent online-backup snapshot of the
      live database (metadata, settings, users, recording rows).
    * ``recordings/`` - final event + continuous clips (the rolling
      ``.prebuffer`` / ``.frames`` / ``.audio`` ingest state is EXCLUDED - it
      is regenerated continuously and would only bloat the archive).
    * ``snapshots/`` - saved event snapshots.
    * ``manifest.json`` - format marker, timestamp, and per-section file
      counts/sizes for restore-time verification.

    Returns the archive path; the caller is responsible for deleting it once
    served. The temporary database snapshot is always cleaned up here.
    """
    import json
    import zipfile

    storage_config = effective_storage_config()

    def resolve_root(key: str) -> Path:
        raw = storage_config.get(key)
        root = Path(str(raw)).expanduser() if raw else Path.cwd()
        if not root.is_absolute():
            root = Path.cwd() / root
        return root.resolve(strict=False)

    recordings_root = resolve_root('recordings_dir')
    snapshots_root = resolve_root('snapshots_dir')
    archive_path = backup_directory() / f'{prefix}-{safe_backup_timestamp()}-{secrets.token_hex(4)}.zip'
    db_filename = Path(str(_state.database.database_path)).name
    db_snapshot = Path(str(_state.database.database_path)).parent / f'.full-backup-{secrets.token_hex(4)}.sqlite3'
    excluded_abs = {
        str(Path(str(_state.database.database_path)).resolve(strict=False)),
        str(archive_path.resolve(strict=False)),
        str(db_snapshot.resolve(strict=False)),
    }
    excluded_names = frozenset({'.prebuffer', '.frames', '.audio', 'backups'})
    try:
        _snapshot_database_file(db_snapshot)
        included: dict[str, Any] = {}
        with zipfile.ZipFile(archive_path, 'w', allowZip64=True) as archive:
            try:
                db_size = db_snapshot.stat().st_size
            except OSError:
                db_size = 0
            archive.write(db_snapshot, f'database/{db_filename}', compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
            included['database'] = {'files': 1, 'bytes': db_size}
            included['recordings'] = _archive_directory(
                archive, recordings_root, 'recordings',
                excluded_abs=excluded_abs, excluded_names=excluded_names,
            )
            included['snapshots'] = _archive_directory(
                archive, snapshots_root, 'snapshots',
                excluded_abs=excluded_abs, excluded_names=excluded_names,
            )
            manifest = {
                'format': 'daygle-full-backup',
                'version': 1,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'database_filename': db_filename,
                'storage': {
                    'data_dir': str(storage_config.get('data_dir') or ''),
                    # Resolved absolute roots: the database's ``file_path``
                    # values are absolute, so record the same form to let a
                    # future restore remap them accurately.
                    'recordings_dir': str(recordings_root),
                    'snapshots_dir': str(snapshots_root),
                    'database': str(Path(str(_state.database.database_path)).resolve(strict=False)),
                },
                'included': included,
            }
            archive.writestr(
                'manifest.json',
                json.dumps(manifest, indent=2, sort_keys=True),
                compress_type=zipfile.ZIP_DEFLATED,
            )
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise
    finally:
        db_snapshot.unlink(missing_ok=True)
    return archive_path


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
    # Flush pending WAL frames into the main database file before overwriting so
    # that frames written by the live connection cannot be replayed on top of the
    # restored data after the backup completes.
    live_flush = sqlite3.connect(str(_state.database.database_path))
    try:
        live_flush.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        live_flush.close()

    # C2 fix: harden the SQLite restore path against malicious backup files.
    # The pre-restore validation in ``validate_restore_database`` already
    # checks integrity + minimum tables + admin presence, but it does NOT
    # inspect stored SQL or control extension loading. Two additional
    # defences are applied HERE, on the connection that backs the upcoming
    # ``source.backup(...)``:
    #
    # 1. ``enable_load_extension(False)`` is called explicitly. Python's
    #    standard library disables extension loading by default on 3.12+ but
    #    some distros / SQLite compile flags flip the default; the explicit
    #    call is a no-op when already disabled and a belt-and-braces fix
    #    when it isn't. Without this, a stored trigger / view whose body
    #    calls ``SELECT load_extension('/tmp/evil.so')`` would autoload
    #    attacker code on the first connection-using query after restore.
    #
    # 2. ``sqlite_master`` is queried for VIEWs and TRIGGERs. The application
    #    ships exactly TWO triggers of its own -- the immutable audit-log
    #    guards (``app.database.AUDIT_LOG_IMMUTABLE_TRIGGERS``) -- and no
    #    views. Those two triggers are allowlisted by their exact (normalised)
    #    body, so an attacker cannot smuggle a ``load_extension`` / ``readfile``
    #    / ``writefile`` payload under a trusted trigger NAME. Any other
    #    trigger, and ANY view, is rejected with HTTP 400 before any row-level
    #    SQL is run. This blocks the wrapper vectors that can ride inside a
    #    view or trigger body even when the live application never queries
    #    them explicitly, while still permitting a legitimate backup produced
    #    by this application (which always carries the audit-log triggers).
    #
    # Both defences are evaluated on the OPEN ``source`` connection; the
    # subsequent ``source.backup(...)`` reuses the same connection after the
    # checks pass, so the destination receives the still-validated schema.
    source = sqlite3.connect(str(restore_source))
    try:
        try:
            source.enable_load_extension(False)
        except (AttributeError, sqlite3.NotSupportedError):
            # Python <3.12 on some SQLite builds exposes no
            # enable_load_extension; those builds default to False already,
            # so the no-op is safe.
            pass
        rows = source.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('view', 'trigger') "
            "AND sql IS NOT NULL AND sql <> ''"
        ).fetchall()
        offending = [
            (row[0], row[1])
            for row in rows
            if not (
                row[0] == 'trigger'
                and _ALLOWED_TRIGGER_DDL.get(row[1]) == _normalize_ddl(row[2])
            )
        ]
        if offending:
            sample = ', '.join(f'{typ}:{name}' for typ, name in offending[:5])
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Restored database contains unexpected views or triggers ({sample}"
                    f"{'...' if len(offending) > 5 else ''}); restore rejected. "
                    f"Only plain-table backups (plus this application's own "
                    f"immutable audit-log triggers) are accepted."
                ),
            )
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
    _state.database.init()
    _state.auth.init()
    # Bug 7 fix: apply the storage / recording swap BEFORE applying the
    # camera settings. ``apply_cameras_settings`` finishes with a call to
    # ``apply_sound_settings`` which (when not suppressed) invokes
    # ``_state.recording_service.prime_rtsp_prebuffer`` on every enabled
    # camera. If the OLD recording service is still published at that
    # point, every prime spawns a fresh ``prebuffer-<key>`` worker on the
    # OLD service -- ``apply_storage_and_recording_settings`` then
    # immediately drains and replaces the OLD service, tearing those
    # fresh workers down without ever serving a recorded frame. The OLD
    # service's per-camera locks synthesize enough disk / CPU / network
    # churn during the redundant ingest that the user can observe
    # several seconds of ffmpeg restart-rate spikes on every restore.
    #
    # Running ``apply_storage_and_recording_settings`` first publishes
    # the NEW (``RecordingService(...)``) onto ``_state.recording_service``
    # before any prime call lands, so the primes land on the NEW one's
    # empty worker dicts -- workers that survive the swap rather than
    # being immediately torn down. The locking discipline from Bug 6
    # (``_apply_settings_lock`` already serialises the two apply_*
    # functions; the swap-order simply removes the wasted work; the lock
    # only needs to be held once because the apply_* functions no longer
    # have a meaningful interleave).
    _state.apply_storage_and_recording_settings()
    _state.apply_cameras_settings(effective_cameras_config())
    _state.auth.apply_config(effective_auth_config())


def purge_recordings_by_policy(*, force: bool = False) -> dict[str, Any]:
    recording_settings = effective_recording_config()
    if not force and (not normalize_bool_setting(recording_settings.get('auto_purge_enabled', True), True)):
        return {'purged': 0, 'files_deleted': 0, 'bytes_deleted': 0, 'recordings': []}
    retention_days = int(recording_settings.get('retention_days', 14))
    max_storage_gb = int(recording_settings.get('max_storage_gb', 20))
    # ``older_than`` is already in canonical UTC ``+00:00`` form from
    # ``datetime.now(timezone.utc).isoformat()`` but we run it through the
    # normaliser so it matches the bound SQL receives + the
    # UTC-``+00:00`` form rows get stored in once ``add_recording``'s
    # normaliser runs (defense in depth on every layer where a TZ
    # mismatch could sort a cutoff before the row it should be comparing
    # against).
    from app.utils import _normalize_iso_to_utc
    older_than = _normalize_iso_to_utc(
        (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    )
    max_storage_bytes = max_storage_gb * 1024 * 1024 * 1024
    purged = _state.database.purge_recordings(older_than=older_than, max_storage_bytes=max_storage_bytes)
    bytes_deleted = 0
    files_deleted = 0
    for recording in purged:
        file_path = safe_storage_path(recording.get('file_path'), roots=('recordings_dir',))
        if file_path is not None and file_path.exists() and file_path.is_file():
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
