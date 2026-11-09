"""Utility APIRouter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

import app.state as _state
from app.auth_gates import require_admin
from app.config_facades import (
    effective_ai_config,
    effective_live_config,
    effective_recording_config,
    effective_storage_config,
    effective_system_config,
)
from app.deps import get_cameras_config, get_database
from app.sound_detector import SOUND_CLASSES
from app.storage_metrics import storage_status
from app.system_metrics import GPU_TEMP_CRITICAL_C, GPU_TEMP_WARN_C, system_resources

router = APIRouter()


@router.get('/api/stats')
def stats(request: Request, since: str | None = Query(None), cameras_config=Depends(get_cameras_config), db=Depends(get_database)):
    require_admin(request)
    result = db.stats(since=since)
    result['total_cameras'] = len(cameras_config)
    return result


def _int_setting(value: Any, fallback: int) -> int:
    """Coerce a stored numeric setting, falling back on anything unusable.

    The settings validator bounds the GPU thermal thresholds, but
    ``app_settings`` can also be restored from an uploaded backup (validated
    for schema, not value types) or hand-edited. A non-numeric value must not
    turn every System page load into an HTTP 500.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


@router.get('/api/system/resources')
def system_resources_status(request: Request):
    """Host CPU / load-average / RAM / GPU snapshot for the dashboard cards."""
    require_admin(request)
    # GPU thermal thresholds are admin-tunable (Settings -> Maintenance -> GPU
    # Health); fall back to the module defaults when unset.
    system = effective_system_config()
    result = system_resources(
        gpu_warn_c=_int_setting(system.get('gpu_temp_warn_c'), GPU_TEMP_WARN_C),
        gpu_critical_c=_int_setting(system.get('gpu_temp_critical_c'), GPU_TEMP_CRITICAL_C),
    )
    result['video_decode'] = video_decode_status()
    return result


@router.get('/api/system/storage')
def system_storage_status(request: Request):
    """Disk space and media usage for the System page's Storage card (admin).

    Reads the folders from the live storage and recording services, so it
    reports exactly where media is being written. A sync handler: FastAPI
    runs it in the threadpool, so a cold directory walk never blocks the
    event loop.
    """
    require_admin(request)
    storage_config = effective_storage_config()
    storage = getattr(_state, 'storage', None)
    recorder = getattr(_state, 'recording_service', None)
    data_dir = Path(getattr(storage, 'data_dir', None) or storage_config.get('data_dir') or 'data')
    locations = {
        'recordings': Path(getattr(recorder, 'recordings_dir', None) or storage_config.get('recordings_dir') or data_dir / 'recordings'),
        'snapshots': Path(getattr(storage, 'snapshots_dir', None) or storage_config.get('snapshots_dir') or data_dir / 'snapshots'),
        'events': Path(getattr(storage, 'events_dir', None) or storage_config.get('events_dir') or data_dir / 'events'),
    }
    database = storage_config.get('database')
    return storage_status(
        locations,
        Path(database) if database else None,
        retention=effective_recording_config(),
    )


def video_decode_status() -> dict:
    """Which decoder each camera's ingest uses, for the System > Health card."""
    from app.video_decode import normalize_video_decode, probe_gpu_decode

    names = {str(camera.get('id') or ''): str(camera.get('name') or camera.get('id') or '') for camera in _state.cameras_config}
    service = getattr(_state, 'recording_service', None)
    decoders = service.ingest_decode_status() if service and hasattr(service, 'ingest_decode_status') else {}
    return {
        'setting': normalize_video_decode(effective_live_config().get('video_decode')),
        **probe_gpu_decode(),
        'cameras': [
            {'camera_id': entry.get('camera_id'), 'name': names.get(str(entry.get('camera_id')), entry.get('camera_id')),
             'decode': entry.get('decode'), 'gpu_fallback': bool(entry.get('gpu_fallback'))}
            for entry in decoders.values()
        ],
    }


@router.get('/api/labels')
def available_labels():
    """Return available labels for the recordings filter dropdown."""
    object_labels: list[str] = []
    ai_config = effective_ai_config()
    labels_path = ai_config.get('labels_path', 'models/coco.names')
    try:
        p = Path(labels_path)
        if p.exists():
            object_labels = [line.strip() for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    except Exception:
        pass
    sound_labels = [
        {'id': class_id, 'label': meta['label'], 'description': meta.get('description', '')}
        for class_id, meta in SOUND_CLASSES.items()
    ]
    return {'objects': object_labels, 'sounds': sound_labels}


@router.delete('/api/objects')
def delete_all_objects(request: Request, db=Depends(get_database)):
    require_admin(request)
    deleted = db.delete_all_objects()
    return {'ok': True, 'deleted': deleted}
