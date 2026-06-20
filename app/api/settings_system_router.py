"""System Settings APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations
import secrets
import sqlite3
from pathlib import Path
from fastapi import APIRouter, Depends, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.auth import utc_now
from app.auth_gates import require_admin
from app.config_facades import (
    effective_auth_config,
    effective_cameras_config,
    effective_live_config,
    effective_recording_config,
    effective_storage_config,
    get_camera_config,
)
from app.deps import (
    get_apply_storage_and_recording_settings,
    get_auth,
    get_auth_enabled,
    get_database,
)
from app.payload_validators import (
    validate_auth_settings,
    validate_live_settings,
    validate_recording_settings,
    validate_storage_settings,
)
from app.request_helpers import write_audit_log
from app.backup import (
    DATABASE_RESTORE_LOCK,
    create_database_backup,
    overwrite_database_from_file,
    refresh_runtime_after_database_restore,
    validate_restore_database,
)
from app.main import (
    BASE_DIR,
    config,
    SESSION_COOKIE_NAME,
)

router = APIRouter()


@router.get('/api/settings/system')
def get_system_settings(db=Depends(get_database), auth_enabled: bool = Depends(get_auth_enabled)):
    version_file = BASE_DIR / 'VERSION'
    current_version = version_file.read_text(encoding='utf-8').strip() if version_file.exists() else 'unknown'
    return {'version': current_version, 'camera': get_camera_config(None), 'cameras': effective_cameras_config(), 'live': effective_live_config(), 'recording': effective_recording_config(), 'storage': effective_storage_config(), 'auth': {'session_timeout_hours': effective_auth_config().get('session_timeout_hours'), 'max_login_attempts': effective_auth_config().get('max_login_attempts'), 'lockout_minutes': effective_auth_config().get('lockout_minutes')}, 'bootstrap': {'database': config.get('storage', {}).get('database'), 'auth_enabled': auth_enabled, 'cookie_name': SESSION_COOKIE_NAME, 'server': config.get('server', {})}}


@router.get('/api/settings/system/database/backup')
def backup_database(request: Request, db=Depends(get_database)):
    require_admin(request)
    backup_path = create_database_backup()
    write_audit_log(request, db, 'backup', 'database', details={'filename': backup_path.name})
    return FileResponse(backup_path, media_type='application/vnd.sqlite3', filename=backup_path.name, headers={'Cache-Control': 'no-store'}, background=BackgroundTask(backup_path.unlink, missing_ok=True))


@router.post('/api/settings/system/database/restore')
async def restore_database(request: Request, file: UploadFile=File(...), db=Depends(get_database)):
    require_admin(request)
    filename = Path(file.filename or '').name
    if not filename:
        raise HTTPException(status_code=400, detail='Choose a SQLite database backup file to restore.')
    if not DATABASE_RESTORE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='Another database restore is already in progress.')
    restore_temp = db.database_path.parent / f'.restore-{secrets.token_hex(8)}.sqlite3'
    try:
        with restore_temp.open('wb') as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        if restore_temp.stat().st_size == 0:
            raise HTTPException(status_code=400, detail='Uploaded database backup is empty.')
        await run_in_threadpool(validate_restore_database, restore_temp)
        safety_backup = await run_in_threadpool(create_database_backup, 'pre-restore-daygle-database')
        try:
            await run_in_threadpool(overwrite_database_from_file, restore_temp)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=500, detail=f'Database restore failed: {exc}') from exc
        await run_in_threadpool(refresh_runtime_after_database_restore)
        write_audit_log(request, db, 'restore', 'database', details={'source_filename': filename, 'safety_backup': str(safety_backup)})
        return {'ok': True, 'message': 'Database restored successfully.', 'source_filename': filename, 'safety_backup': str(safety_backup)}
    finally:
        DATABASE_RESTORE_LOCK.release()
        restore_temp.unlink(missing_ok=True)
        for sidecar_suffix in ('-wal', '-shm'):
            Path(f'{restore_temp}{sidecar_suffix}').unlink(missing_ok=True)
        await file.close()


@router.put('/api/settings/system/live')
async def update_live_settings(request: Request, db=Depends(get_database)):
    require_admin(request)
    settings = validate_live_settings(await request.json())
    db.set_setting('live', settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.live')
    return settings


@router.put('/api/settings/system/recording')
async def update_recording_settings(
    request: Request,
    db=Depends(get_database),
    apply_storage_and_recording_settings=Depends(get_apply_storage_and_recording_settings),
):
    require_admin(request)
    settings = validate_recording_settings(await request.json())
    db.set_setting('recording', settings, utc_now())
    apply_storage_and_recording_settings()
    write_audit_log(request, db, 'update', 'settings.recording')
    return settings


@router.put('/api/settings/system/storage')
async def update_storage_settings(
    request: Request,
    db=Depends(get_database),
    apply_storage_and_recording_settings=Depends(get_apply_storage_and_recording_settings),
):
    require_admin(request)
    settings = validate_storage_settings(await request.json())
    db.set_setting('storage', settings, utc_now())
    apply_storage_and_recording_settings()
    write_audit_log(request, db, 'update', 'settings.storage')
    return settings


@router.put('/api/settings/system/auth')
async def update_auth_settings(request: Request, db=Depends(get_database), auth=Depends(get_auth)):
    require_admin(request)
    settings = validate_auth_settings(await request.json())
    db.set_setting('auth', settings, utc_now())
    auth.apply_config(settings)
    write_audit_log(request, db, 'update', 'settings.auth')
    return settings
