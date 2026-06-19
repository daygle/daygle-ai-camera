"""System Settings APIRouter.

Extracted from ``app/main.py`` (Phase-9 of the hybrid-pattern router split).
Same template as ``app/api/settings_ai_router.py`` (Phase-2) and
``app/api/status_router.py`` (Phase-8): ``import app.main as main`` at
module level, every global / helper read through ``main.<name>`` *inside*
handler bodies.

Handlers moved (7):

- GET   /api/settings/system
- GET   /api/settings/system/database/backup
- POST  /api/settings/system/database/restore
- PUT   /api/settings/system/live
- PUT   /api/settings/system/recording
- PUT   /api/settings/system/storage
- PUT   /api/settings/system/auth

BODY-REWRITE NOTE
The status_router.py Phase-8 BODY-REWRITE NOTE applies here too:
handlers in this file originally referenced module-level state in
main.py via bare names (``config``, ``auth_enabled``,
``SESSION_COOKIE_NAME``, ``BASE_DIR``, ``database``,
``effective_cameras_config``, ``effective_live_config``, etc.). After
extraction to this router, those bare names resolve to ZERO attributes
in our namespace -- handlers would NameError at request time. Per
hybrid-pattern uniformity (rule 5 of ``app/api/__init__.py``), each
bare call is rewritten as ``main.<bare>`` here. Pure syntactic change
with zero behavioral impact.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.BASE_DIR`` - module-level Path to the repo root.
- ``main.config`` - the loaded config dict (re-export).
- ``main.auth_enabled`` - bool flag for whether auth is enabled.
- ``main.SESSION_COOKIE_NAME`` - the resolved session cookie name.
- ``main.database`` - the EventDatabase instance (with .set_setting,
  .add_event etc.; the backup handler reads .database_path).
- ``main.auth`` - the AuthService instance (the auth PUT calls
  apply_config).
- ``main.require_admin``, ``main.write_audit_log``, ``main.utc_now`` -
  shared request helpers used across the settings PUT flow.
- ``main.get_camera_config``, ``main.effective_cameras_config``,
  ``main.effective_live_config``, ``main.effective_recording_config``,
  ``main.effective_storage_config``, ``main.effective_auth_config`` -
  reading setters built at module-load.
- ``main.create_database_backup``, ``main.validate_restore_database``,
  ``main.overwrite_database_from_file``,
  ``main.refresh_runtime_after_database_restore`` - the database
  backup/restore pipeline the POST handler composes.
- ``main.validate_live_settings``, ``main.validate_recording_settings``,
  ``main.validate_storage_settings``, ``main.validate_auth_settings`` -
  per-subdomain validator functions called before the PUT handler
  persists.
- ``main.apply_storage_and_recording_settings`` - hot-reload the
  running services after storage/recording settings change.
- ``main.DATABASE_RESTORE_LOCK`` - module-level threading.Lock that
  serialises concurrent restore POSTs.

Tests go through ``LocalClient.request`` rather than calling
``main.<attr>`` directly for these endpoints, so no back-compat alias
on ``app.main`` is needed. The Phase-7.1 invariant
``tests/test_api_router_split_invariants.py::test_app_api_imports_in_main_are_consumed``
will catch any orphan-import regression if a future refactor drops the
``from app.api.settings_system_router import router as settings_system_router``
rebind line in ``app/main.py``.
"""

from __future__ import annotations
import secrets
import sqlite3
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

import app.main as main

router = APIRouter()


@router.get('/api/settings/system')
def get_system_settings():
    version_file = main.BASE_DIR / 'VERSION'
    current_version = version_file.read_text(encoding='utf-8').strip() if version_file.exists() else 'unknown'
    return {'version': current_version, 'camera': main.get_camera_config(None), 'cameras': main.effective_cameras_config(), 'live': main.effective_live_config(), 'recording': main.effective_recording_config(), 'storage': main.effective_storage_config(), 'auth': {'session_timeout_hours': main.effective_auth_config().get('session_timeout_hours'), 'max_login_attempts': main.effective_auth_config().get('max_login_attempts'), 'lockout_minutes': main.effective_auth_config().get('lockout_minutes')}, 'bootstrap': {'database': main.config.get('storage', {}).get('database'), 'auth_enabled': main.auth_enabled, 'cookie_name': main.SESSION_COOKIE_NAME, 'server': main.config.get('server', {})}}


@router.get('/api/settings/system/database/backup')
def backup_database(request: Request):
    main.require_admin(request)
    backup_path = main.create_database_backup()
    main.write_audit_log(request, 'backup', 'database', details={'filename': backup_path.name})
    return main.FileResponse(backup_path, media_type='application/vnd.sqlite3', filename=backup_path.name, headers={'Cache-Control': 'no-store'}, background=BackgroundTask(backup_path.unlink, missing_ok=True))


@router.post('/api/settings/system/database/restore')
async def restore_database(request: Request, file: UploadFile=File(...)):
    main.require_admin(request)
    filename = Path(file.filename or '').name
    if not filename:
        raise HTTPException(status_code=400, detail='Choose a SQLite database backup file to restore.')
    if not main.DATABASE_RESTORE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='Another database restore is already in progress.')
    restore_temp = main.database.database_path.parent / f'.restore-{secrets.token_hex(8)}.sqlite3'
    try:
        with restore_temp.open('wb') as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        if restore_temp.stat().st_size == 0:
            raise HTTPException(status_code=400, detail='Uploaded database backup is empty.')
        await run_in_threadpool(main.validate_restore_database, restore_temp)
        safety_backup = await run_in_threadpool(main.create_database_backup, 'pre-restore-daygle-database')
        try:
            await run_in_threadpool(main.overwrite_database_from_file, restore_temp)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=500, detail=f'Database restore failed: {exc}') from exc
        await run_in_threadpool(main.refresh_runtime_after_database_restore)
        main.write_audit_log(request, 'restore', 'database', details={'source_filename': filename, 'safety_backup': str(safety_backup)})
        return {'ok': True, 'message': 'Database restored successfully.', 'source_filename': filename, 'safety_backup': str(safety_backup)}
    finally:
        main.DATABASE_RESTORE_LOCK.release()
        restore_temp.unlink(missing_ok=True)
        for sidecar_suffix in ('-wal', '-shm'):
            Path(f'{restore_temp}{sidecar_suffix}').unlink(missing_ok=True)
        await file.close()


@router.put('/api/settings/system/live')
async def update_live_settings(request: Request):
    main.require_admin(request)
    settings = main.validate_live_settings(await request.json())
    main.database.set_setting('live', settings, main.utc_now())
    main.write_audit_log(request, 'update', 'settings.live')
    return settings


@router.put('/api/settings/system/recording')
async def update_recording_settings(request: Request):
    main.require_admin(request)
    settings = main.validate_recording_settings(await request.json())
    main.database.set_setting('recording', settings, main.utc_now())
    main.apply_storage_and_recording_settings()
    main.write_audit_log(request, 'update', 'settings.recording')
    return settings


@router.put('/api/settings/system/storage')
async def update_storage_settings(request: Request):
    main.require_admin(request)
    settings = main.validate_storage_settings(await request.json())
    main.database.set_setting('storage', settings, main.utc_now())
    main.apply_storage_and_recording_settings()
    main.write_audit_log(request, 'update', 'settings.storage')
    return settings


@router.put('/api/settings/system/auth')
async def update_auth_settings(request: Request):
    main.require_admin(request)
    settings = main.validate_auth_settings(await request.json())
    main.database.set_setting('auth', settings, main.utc_now())
    main.auth.apply_config(settings)
    main.write_audit_log(request, 'update', 'settings.auth')
    return settings
