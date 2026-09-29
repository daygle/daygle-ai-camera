"""Utility APIRouter.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request

import app.state as _state
from app.auth_gates import require_admin
from app.config_facades import effective_ai_config, effective_live_config, effective_system_config
from app.deps import get_cameras_config, get_database
from app.sound_detector import SOUND_CLASSES
from app.system_metrics import GPU_TEMP_CRITICAL_C, GPU_TEMP_WARN_C, system_resources

router = APIRouter()


@router.get('/api/stats')
def stats(request: Request, since: str | None = Query(None), cameras_config=Depends(get_cameras_config), db=Depends(get_database)):
    require_admin(request)
    result = db.stats(since=since)
    result['total_cameras'] = len(cameras_config)
    return result


@router.get('/api/system/resources')
def system_resources_status(request: Request):
    """Host CPU / load-average / RAM / GPU snapshot for the dashboard cards."""
    require_admin(request)
    # GPU thermal thresholds are admin-tunable (Settings -> Maintenance -> GPU
    # Health); fall back to the module defaults when unset.
    system = effective_system_config()
    result = system_resources(
        gpu_warn_c=int(system.get('gpu_temp_warn_c') or GPU_TEMP_WARN_C),
        gpu_critical_c=int(system.get('gpu_temp_critical_c') or GPU_TEMP_CRITICAL_C),
    )
    result['video_decode'] = video_decode_status()
    return result


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
