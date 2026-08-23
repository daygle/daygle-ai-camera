"""Database backup and recording-purge helpers extracted from ``app/main.py`` (Phase-J).

Cluster membership:
- ``DATABASE_RESTORE_REQUIRED_TABLES`` - set of table names required for a valid restore
- ``DATABASE_RESTORE_LOCK`` - process-wide lock serialising database overwrites
- ``backup_directory()`` - compute (and mkdir) the data/backups directory
- ``safe_backup_timestamp()`` - UTC timestamp string for backup filenames
- ``create_database_backup(prefix)`` - SQLite online-backup to a timestamped file
- ``validate_restore_database(path)`` - integrity-check an uploaded database file
- ``validate_full_backup(path)`` - validate a full-backup ZIP manifest and members
- ``restore_full_backup(path)`` - restore database, media, and model artifacts
- ``overwrite_database_from_file(restore_source)`` - hot-swap the live database
- ``refresh_runtime_after_database_restore()`` - re-init singletons after restore
- ``purge_recordings_by_policy(*, force)`` - age-out old recordings + disk files
- ``purge_camera_diagnostics_by_policy()`` - age-out old camera-log rows

Pool-C reach (resolved lazily via lazy imports inside function bodies):
- ``app.main.apply_cameras_settings`` (``refresh_runtime_after_database_restore``)
- ``app.main.apply_storage_and_recording_settings`` (``refresh_runtime_after_database_restore``)
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.config_facades import (
    effective_auth_config,
    effective_cameras_config,
    effective_face_recognition_config,
    effective_recording_config,
    effective_storage_config,
)
from app.database import AUDIT_LOG_IMMUTABLE_TRIGGERS
from app.label_groups import refresh_label_groups
from app.recording_files import delete_recording_files
from app.media_utils import safe_storage_path
from app.utils import normalize_bool_setting

logger = logging.getLogger('daygle.ai')

DATABASE_RESTORE_REQUIRED_TABLES: set[str] = {'events', 'detections', 'app_settings', 'users'}
DATABASE_RESTORE_LOCK: threading.Lock = threading.Lock()
FULL_BACKUP_FORMAT = 'daygle-full-backup'
FULL_BACKUP_VERSION = 2
SUPPORTED_FULL_BACKUP_VERSIONS = {1, FULL_BACKUP_VERSION}
FULL_BACKUP_MAX_MEMBERS = 200_000
FULL_BACKUP_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024 * 1024


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


def _online_backup_snapshot(source_path: str | Path, destination_path: str | Path, *, failure_detail: str) -> None:
    """Copy *source_path* to *destination_path* via SQLite's online-backup API.

    ``source.backup()`` performs a page-by-page scan-and-copy that folds in WAL
    frames, so the copy is by-construction physically consistent even while the
    source connection is mid-write. ``PRAGMA integrity_check`` then confirms the
    page tree + free lists on the new file BEFORE callers declare success (N1):
    torn writes, power loss, or mid-cycle filesystem faults are rejected here
    instead of surfacing weeks later at restore-restore validation time.
    """
    source = sqlite3.connect(str(source_path))
    try:
        destination = sqlite3.connect(str(destination_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    verify = sqlite3.connect(str(destination_path))
    try:
        integrity = verify.execute('PRAGMA integrity_check').fetchone()
        if not integrity or str(integrity[0]).lower() != 'ok':
            offending = '<none>' if not integrity else str(integrity[0])
            raise HTTPException(status_code=500, detail=f'{failure_detail} ({offending})')
    finally:
        verify.close()


def create_database_backup(prefix: str = 'daygle-database') -> Path:
    backup_path = backup_directory() / f'{prefix}-{safe_backup_timestamp()}-{secrets.token_hex(4)}.sqlite3'
    try:
        _online_backup_snapshot(
            _state.database.database_path,
            backup_path,
            failure_detail='Backup failed integrity check',
        )
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def _snapshot_database_file(destination: Path) -> None:
    """Snapshot the live database for a full-backup archive."""
    _online_backup_snapshot(
        _state.database.database_path,
        destination,
        failure_detail='Full backup database snapshot failed integrity check',
    )


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
            # Skip capture/render intermediates: a mid-write temp clip, audio-
            # mux staging copy, or concat manifest would be archived torn and
            # unplayable, and each is deleted moments after render anyway.
            lower = name.lower()
            if (
                lower.endswith('.concat.txt')
                or lower.endswith('.audio.mp4')
                or '.prebuffer.tmp.' in lower
                or '.recording.tmp.' in lower
            ):
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


def _resolve_storage_root(storage_config: dict[str, Any], key: str) -> Path:
    raw = storage_config.get(key)
    root = Path(str(raw)).expanduser() if raw else Path.cwd()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve(strict=False)


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
    * ``events/`` - legacy event artifacts, when present.
    * ``models/`` - installed detection and sound-model assets.
    * ``manifest.json`` - format marker, timestamp, and per-section file
      counts/sizes for restore-time verification.

    Returns the archive path; the caller is responsible for deleting it once
    served. The temporary database snapshot is always cleaned up here.
    """
    storage_config = effective_storage_config()

    recordings_root = _resolve_storage_root(storage_config, 'recordings_dir')
    snapshots_root = _resolve_storage_root(storage_config, 'snapshots_dir')
    events_root = _resolve_storage_root(storage_config, 'events_dir')
    models_root = (Path(__file__).resolve().parent.parent / 'models').resolve(strict=False)
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
            included['events'] = _archive_directory(
                archive, events_root, 'events',
                excluded_abs=excluded_abs, excluded_names=excluded_names,
            )
            included['models'] = _archive_directory(
                archive, models_root, 'models',
                excluded_abs=excluded_abs, excluded_names=frozenset(),
            )
            manifest = {
                'format': FULL_BACKUP_FORMAT,
                'version': FULL_BACKUP_VERSION,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'database_filename': db_filename,
                'storage': {
                    'data_dir': str(storage_config.get('data_dir') or ''),
                    # Resolved absolute roots: the database's ``file_path``
                    # values are absolute, so record the same form to let a
                    # future restore remap them accurately.
                    'recordings_dir': str(recordings_root),
                    'snapshots_dir': str(snapshots_root),
                    'events_dir': str(events_root),
                    'database': str(Path(str(_state.database.database_path)).resolve(strict=False)),
                    'models_dir': str(models_root),
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


def _normalise_archive_member(name: str) -> str:
    if not name or '\x00' in name or '\\' in name:
        raise HTTPException(status_code=400, detail='Full backup contains an invalid archive path.')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in path.parts):
        raise HTTPException(status_code=400, detail='Full backup contains an unsafe archive path.')
    return path.as_posix()


def _validate_full_backup_archive(path: Path) -> tuple[dict[str, Any], str]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail='Uploaded full backup is not a valid ZIP archive.') from exc
    try:
        infos = archive.infolist()
        if len(infos) > FULL_BACKUP_MAX_MEMBERS:
            raise HTTPException(status_code=400, detail='Full backup contains too many files.')
        total_size = 0
        names: set[str] = set()
        allowed_roots = {'database', 'recordings', 'snapshots', 'events', 'models'}
        database_members: list[str] = []
        for info in infos:
            name = _normalise_archive_member(info.filename)
            if name in names:
                raise HTTPException(status_code=400, detail='Full backup contains duplicate archive paths.')
            names.add(name)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise HTTPException(status_code=400, detail='Full backup contains a symbolic link.')
            if name != 'manifest.json':
                root = name.split('/', 1)[0]
                if root not in allowed_roots:
                    raise HTTPException(status_code=400, detail='Full backup contains an unexpected archive section.')
                if root == 'database' and not info.is_dir():
                    database_members.append(name)
            total_size += max(0, int(info.file_size))
            if total_size > FULL_BACKUP_MAX_UNCOMPRESSED_BYTES:
                raise HTTPException(status_code=400, detail='Full backup is too large to restore safely.')
        if 'manifest.json' not in names:
            raise HTTPException(status_code=400, detail='Full backup is missing manifest.json.')
        try:
            manifest = json.loads(archive.read('manifest.json'))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail='Full backup manifest is invalid.') from exc
        if not isinstance(manifest, dict) or manifest.get('format') != FULL_BACKUP_FORMAT:
            raise HTTPException(status_code=400, detail='Uploaded ZIP is not a Daygle full backup.')
        try:
            version = int(manifest.get('version', 0))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='Full backup manifest version is invalid.') from exc
        if version not in SUPPORTED_FULL_BACKUP_VERSIONS:
            raise HTTPException(status_code=400, detail='This full backup version is not supported by the installed application.')
        if len(database_members) != 1 or not database_members[0].lower().endswith('.sqlite3'):
            raise HTTPException(status_code=400, detail='Full backup must contain exactly one SQLite database.')
        return manifest, database_members[0]
    finally:
        archive.close()


def validate_full_backup(path: Path) -> dict[str, Any]:
    """Validate a full-backup container without extracting or changing state."""
    manifest, _database_member = _validate_full_backup_archive(path)
    return manifest


def _extract_full_backup(path: Path, destination: Path) -> tuple[dict[str, Any], Path]:
    manifest, database_member = _validate_full_backup_archive(path)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = _normalise_archive_member(info.filename)
            if info.is_dir():
                continue
            target = (destination / name).resolve(strict=False)
            if not target.is_relative_to(destination.resolve(strict=False)):
                raise HTTPException(status_code=400, detail='Full backup extraction escaped its staging directory.')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, 'r') as source, target.open('wb') as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    return manifest, destination / database_member


def _portable_relative_path(raw_path: Any, source_root: Any) -> Path | None:
    raw = str(raw_path or '').strip().replace('\\', '/')
    source = str(source_root or '').strip().replace('\\', '/').rstrip('/')
    if not raw or not source:
        return None
    raw_cmp = raw.casefold()
    source_cmp = source.casefold()
    prefix = source_cmp + '/'
    if raw_cmp.startswith(prefix):
        relative = raw[len(source) + 1:]
    elif not raw.startswith('/') and not (len(raw) > 1 and raw[1] == ':'):
        relative = raw
    else:
        return None
    relative_path = PurePosixPath(relative)
    if not relative or relative_path.is_absolute() or any(part in {'', '.', '..'} for part in relative_path.parts):
        return None
    return Path(*relative_path.parts)


def _remap_restored_database(database_path: Path, manifest: dict[str, Any], target_storage: dict[str, Any]) -> None:
    source_storage = manifest.get('storage') if isinstance(manifest.get('storage'), dict) else {}
    mappings = (
        ('recordings_dir', 'recordings_dir', ('recordings', 'file_path')),
        ('snapshots_dir', 'snapshots_dir', ('events', 'snapshot_path')),
        ('snapshots_dir', 'snapshots_dir', ('events', 'thumbnail_path')),
        ('snapshots_dir', 'snapshots_dir', ('recordings', 'thumbnail_path')),
        ('events_dir', 'events_dir', ('events', 'snapshot_path')),
    )
    conn = sqlite3.connect(str(database_path))
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for source_key, target_key, (table, column) in mappings:
            if table not in tables:
                continue
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                continue
            source_root = source_storage.get(source_key)
            target_root = _resolve_storage_root(target_storage, target_key)
            rows = conn.execute(f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL').fetchall()
            updates: list[tuple[str, int]] = []
            for rowid, raw_value in rows:
                relative = _portable_relative_path(raw_value, source_root)
                if relative is not None:
                    updates.append((str(target_root / relative), rowid))
            # One executemany per table/column instead of a statement per row --
            # a large library can carry hundreds of thousands of media paths.
            if updates:
                conn.executemany(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                    updates,
                )
        if 'app_settings' in tables:
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'storage'").fetchone()
            if row:
                try:
                    storage_override = json.loads(row[0])
                except (TypeError, ValueError):
                    storage_override = None
                if isinstance(storage_override, dict):
                    for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir'):
                        if target_storage.get(key):
                            storage_override[key] = str(_resolve_storage_root(target_storage, key))
                    storage_override['database'] = str(Path(str(_state.database.database_path)).resolve(strict=False))
                    conn.execute(
                        "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = 'storage'",
                        (json.dumps(storage_override), datetime.now(timezone.utc).isoformat()),
                    )
        conn.commit()
    finally:
        conn.close()


def _copy_restored_tree(source: Path, target: Path) -> int:
    if not source.exists():
        return 0
    if target.exists() and target.is_symlink():
        raise HTTPException(status_code=400, detail='Configured restore directory is a symbolic link.')
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            source_file = current / name
            if source_file.is_symlink():
                raise HTTPException(status_code=400, detail='Full backup contains a symbolic link.')
            relative = source_file.relative_to(source)
            current_target = target
            for part in relative.parts[:-1]:
                current_target = current_target / part
                if current_target.exists() and current_target.is_symlink():
                    raise HTTPException(status_code=400, detail='Configured restore directory contains a symbolic link.')
            raw_target_file = target / relative
            if raw_target_file.exists() and raw_target_file.is_symlink():
                raise HTTPException(status_code=400, detail='Configured restore directory contains a symbolic link.')
            target_file = raw_target_file.resolve(strict=False)
            if not target_file.is_relative_to(target.resolve(strict=False)):
                raise HTTPException(status_code=400, detail='Full backup media path is unsafe.')
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
            copied += 1
    return copied


def restore_full_backup(path: Path) -> dict[str, Any]:
    """Restore a full backup's database, media, and model artifacts.

    The archive is extracted into a private staging directory first. Database
    media paths and the persisted storage override are rewritten from the
    source machine's roots to the current installation's roots before the
    validated database is installed. Secrets from environment variables and
    the protected Cloudflare token file are intentionally not imported.
    """
    target_storage = effective_storage_config()
    database_parent = Path(str(_state.database.database_path)).resolve(strict=False).parent
    with tempfile.TemporaryDirectory(prefix='.full-restore-', dir=str(database_parent)) as staging_name:
        staging = Path(staging_name)
        manifest, staged_database = _extract_full_backup(path, staging)
        validate_restore_database(staged_database)
        _remap_restored_database(staged_database, manifest, target_storage)
        validate_restore_database(staged_database)
        storage_targets = {
            'recordings': _resolve_storage_root(target_storage, 'recordings_dir'),
            'snapshots': _resolve_storage_root(target_storage, 'snapshots_dir'),
            'events': _resolve_storage_root(target_storage, 'events_dir'),
            'models': (Path(__file__).resolve().parent.parent / 'models').resolve(strict=False),
        }
        # Copy media BEFORE overwriting the live database. The tree copies are
        # additive and idempotent, so failing halfway leaves extra files that a
        # later retry simply re-copies; overwriting the DB first instead meant
        # a mid-copy failure restored rows pointing at media that was never
        # written -- the worst of both states.
        copied = {
            section: _copy_restored_tree(staging / section, target)
            for section, target in storage_targets.items()
        }
        overwrite_database_from_file(staged_database)
    return {'version': manifest.get('version'), 'copied': copied}


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
    # The object-label groups cache may have been primed from the OLD database;
    # re-read so zone/alert matching reflects the restored group map.
    refresh_label_groups()


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


def purge_face_identities_by_policy() -> int:
    """Age out recognised-identity data on events per the recognition policy.

    The face-recognition ``retention_days`` setting governs how long recognised
    identities are kept on event metadata; ``0`` (the default) means keep
    indefinitely, so this is a no-op then. Otherwise events older than the window
    have their ``face_identities`` block stripped (the event itself is kept).
    """
    try:
        retention_days = int(effective_face_recognition_config().get('retention_days', 0) or 0)
        if retention_days <= 0:
            return 0
        older_than = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        return _state.database.purge_face_identities(older_than=older_than)
    except Exception as exc:
        logger.debug('Face identity purge failed: %s', exc)
        return 0
