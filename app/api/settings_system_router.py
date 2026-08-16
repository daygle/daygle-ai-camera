"""System Settings APIRouter.
"""

from __future__ import annotations
import secrets
import sqlite3
import threading
import zipfile
from pathlib import Path
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.auth import utc_now
from app.cloudflare_tunnel import MAX_TUNNEL_TOKEN_LENGTH, CloudflareTunnelSecretStore
from app.auth_gates import require_admin
from app.camera_lifecycle import apply_storage_and_recording_settings
from app.config_facades import (
    effective_auth_config,
    effective_cameras_config,
    effective_live_config,
    effective_recording_config,
    effective_storage_config,
    get_camera_config,
)
from app.deps import (
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
    create_full_backup,
    overwrite_database_from_file,
    refresh_runtime_after_database_restore,
    restore_full_backup,
    validate_full_backup,
    validate_restore_database,
)
from app.auth import SESSION_COOKIE
from app.utils import _current_version
import app.state as _state

router = APIRouter()


@router.get('/api/settings/system')
def get_system_settings(request: Request, db=Depends(get_database), auth_enabled: bool = Depends(get_auth_enabled)):
    # M1 fix: handler-level admin gate. Middleware already enforces the
    # admin role via the /api/settings/system branch-5 match, but the
    # explicit gate here is the second line if a future refactor moves
    # the path out of the middleware's admin match list.
    require_admin(request)
    return {'version': _current_version(), 'camera': get_camera_config(None), 'cameras': effective_cameras_config(), 'live': effective_live_config(), 'recording': effective_recording_config(), 'storage': effective_storage_config(), 'cloudflare_tunnel': _tunnel_status(db),        'auth': {
            'session_timeout_hours': effective_auth_config().get('session_timeout_hours'),
            'max_login_attempts': effective_auth_config().get('max_login_attempts'),
            'lockout_minutes': effective_auth_config().get('lockout_minutes'),
            'rate_limit_max_attempts': effective_auth_config().get('rate_limit_max_attempts'),
            'rate_limit_window_seconds': effective_auth_config().get('rate_limit_window_seconds'),
            'rate_limit_base_delay': effective_auth_config().get('rate_limit_base_delay'),
            'rate_limit_max_delay': effective_auth_config().get('rate_limit_max_delay'),
            'trusted_proxies': effective_auth_config().get('trusted_proxies'),
        }, 'bootstrap': {'database': _state.config.get('storage', {}).get('database'), 'auth_enabled': auth_enabled, 'cookie_name': str(effective_auth_config().get('cookie_name', SESSION_COOKIE)), 'server': _state.config.get('server', {})}}


@router.get('/api/settings/system/database/backup')
def backup_database(request: Request, db=Depends(get_database)):
    require_admin(request)
    backup_path = create_database_backup()
    write_audit_log(request, db, 'backup', 'database', details={'filename': backup_path.name})
    return FileResponse(backup_path, media_type='application/vnd.sqlite3', filename=backup_path.name, headers={'Cache-Control': 'no-store'}, background=BackgroundTask(backup_path.unlink, missing_ok=True))


@router.get('/api/settings/system/database/backup/full')
def backup_database_full(request: Request, db=Depends(get_database)):
    require_admin(request)
    backup_path = create_full_backup()
    write_audit_log(request, db, 'backup', 'database.full', details={'filename': backup_path.name})
    return FileResponse(
        backup_path,
        media_type='application/zip',
        filename=backup_path.name,
        headers={'Cache-Control': 'no-store'},
        background=BackgroundTask(backup_path.unlink, missing_ok=True),
    )


@router.post('/api/settings/system/database/restore')
async def restore_database(request: Request, file: UploadFile=File(...), db=Depends(get_database)):
    require_admin(request)
    filename = Path(file.filename or '').name
    if not filename:
        raise HTTPException(status_code=400, detail='Choose a SQLite database or full ZIP backup file to restore.')
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
            raise HTTPException(status_code=400, detail='Uploaded backup is empty.')
        is_full_backup = zipfile.is_zipfile(restore_temp)
        if is_full_backup:
            await run_in_threadpool(validate_full_backup, restore_temp)
            safety_backup = await run_in_threadpool(create_full_backup, 'pre-restore-daygle-full')
        else:
            await run_in_threadpool(validate_restore_database, restore_temp)
            safety_backup = await run_in_threadpool(create_database_backup, 'pre-restore-daygle-database')
        try:
            if is_full_backup:
                restore_result = await run_in_threadpool(restore_full_backup, restore_temp)
            else:
                await run_in_threadpool(overwrite_database_from_file, restore_temp)
                restore_result = None
        except sqlite3.Error as exc:
            raise HTTPException(status_code=500, detail=f'Backup restore failed: {exc}') from exc
        await run_in_threadpool(refresh_runtime_after_database_restore)
        resource = 'database.full' if is_full_backup else 'database'
        write_audit_log(request, db, 'restore', resource, details={'source_filename': filename, 'safety_backup': str(safety_backup)})
        message = 'Full backup restored successfully (database, media, and models).' if is_full_backup else 'Database restored successfully.'
        return {'ok': True, 'message': message, 'source_filename': filename, 'safety_backup': str(safety_backup), 'restore': restore_result}
    finally:
        DATABASE_RESTORE_LOCK.release()
        restore_temp.unlink(missing_ok=True)
        for sidecar_suffix in ('-wal', '-shm'):
            Path(f'{restore_temp}{sidecar_suffix}').unlink(missing_ok=True)
        await file.close()


def _tunnel_status(db=None):
    """Tunnel manager status plus the effective ``tunnel_loopback_only`` value.

    The bind-mode toggle is persisted in ``app_settings`` (UI) and falls back
    to the ``server.tunnel_loopback_only`` YAML bootstrap value (default true)
    when it has never been saved from the dashboard.
    """
    manager = _state.cloudflare_tunnel_manager
    if manager is None:
        status = {'configured': False, 'running': False, 'source': 'none', 'autostart': False, 'pid': None, 'binary': 'cloudflared', 'error': None}
    else:
        status = manager.status()
    default_loopback = bool(_state.config.get('server', {}).get('tunnel_loopback_only', True))
    persisted = db.get_setting('cloudflare_tunnel') if db is not None else None
    persisted = persisted if isinstance(persisted, dict) else {}
    status['tunnel_loopback_only'] = bool(persisted.get('tunnel_loopback_only', default_loopback))
    return status


@router.get('/api/settings/system/cloudflare-tunnel')
def get_cloudflare_tunnel_settings(request: Request, db=Depends(get_database)):
    require_admin(request)
    return _tunnel_status(db)


@router.put('/api/settings/system/cloudflare-tunnel')
async def update_cloudflare_tunnel_settings(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Cloudflare Tunnel settings must be an object.')
    raw_token = payload.get('token')
    token = str(raw_token or '').strip() if raw_token is not None else None
    if token is not None and len(token) > MAX_TUNNEL_TOKEN_LENGTH:
        raise HTTPException(status_code=400, detail='Cloudflare Tunnel token is too long.')
    manager = _state.cloudflare_tunnel_manager
    if manager is None:
        raise HTTPException(status_code=503, detail='Cloudflare Tunnel manager is not ready.')
    token_store = CloudflareTunnelSecretStore(db.database_path)
    if token is None or token == '':
        # An empty field means clear the persisted token, not an accidental
        # overwrite of a secret that the browser deliberately never receives.
        token = None
    current_persisted = db.get_setting('cloudflare_tunnel') or {}
    if not isinstance(current_persisted, dict):
        current_persisted = {}
    autostart = bool(payload.get('autostart', current_persisted.get('autostart', False)))
    tunnel_loopback_only = bool(payload.get('tunnel_loopback_only', current_persisted.get('tunnel_loopback_only', False)))
    if token:
        try:
            token_store.write(token)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f'Could not securely store Cloudflare Tunnel token: {type(exc).__name__}.') from exc
    else:
        token_store.clear()
    # SQLite stores only non-secret metadata; the secret lives in a 0600 file.
    persisted = {'configured': bool(token), 'autostart': autostart, 'tunnel_loopback_only': tunnel_loopback_only}
    db.set_setting('cloudflare_tunnel', persisted, utc_now())
    manager.configure(token, source='database', autostart=autostart)
    write_audit_log(request, db, 'update', 'settings.cloudflare_tunnel', details={'configured': bool(token), 'autostart': autostart})
    return _tunnel_status()


@router.post('/api/settings/system/cloudflare-tunnel/start')
def start_cloudflare_tunnel(request: Request):
    require_admin(request)
    manager = _state.cloudflare_tunnel_manager
    if manager is None:
        raise HTTPException(status_code=503, detail='Cloudflare Tunnel manager is not ready.')
    return manager.start()


@router.post('/api/settings/system/cloudflare-tunnel/stop')
def stop_cloudflare_tunnel(request: Request):
    require_admin(request)
    manager = _state.cloudflare_tunnel_manager
    if manager is None:
        raise HTTPException(status_code=503, detail='Cloudflare Tunnel manager is not ready.')
    return manager.stop()


@router.post('/api/settings/system/cloudflare-tunnel/restart')
def restart_cloudflare_tunnel(request: Request):
    require_admin(request)
    manager = _state.cloudflare_tunnel_manager
    if manager is None:
        raise HTTPException(status_code=503, detail='Cloudflare Tunnel manager is not ready.')
    return manager.restart()


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
):
    require_admin(request)
    settings = validate_recording_settings(await request.json())
    db.set_setting('recording', settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.recording')
    # Defer the expensive service restart to a background thread so the
    # HTTP response returns immediately. The internal
    # ``_apply_settings_lock`` serialises concurrent apply calls safely.
    threading.Thread(target=apply_storage_and_recording_settings, daemon=True).start()
    return settings


@router.put('/api/settings/system/storage')
async def update_storage_settings(
    request: Request,
    db=Depends(get_database),
):
    require_admin(request)
    settings = validate_storage_settings(await request.json())
    # Create directories synchronously so callers (including the test)
    # can rely on them existing immediately after the PUT returns.
    # The full service restart (stopping old ffmpegs, starting new ones)
    # is still deferred to the background thread below.
    for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir'):
        path = settings.get(key)
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
    db.set_setting('storage', settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.storage')
    # Defer the expensive service restart to a background thread so the
    # HTTP response returns immediately. The internal
    # ``_apply_settings_lock`` serialises concurrent apply calls safely.
    threading.Thread(target=apply_storage_and_recording_settings, daemon=True).start()
    return settings


@router.put('/api/settings/system/auth')
async def update_auth_settings(request: Request, db=Depends(get_database), auth=Depends(get_auth)):
    require_admin(request)
    settings = validate_auth_settings(await request.json())
    db.set_setting('auth', settings, utc_now())
    auth.apply_config(settings)
    write_audit_log(request, db, 'update', 'settings.auth')
    return settings
