"""Utility APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request

from app.auth_gates import require_admin
from app.config_facades import effective_ai_config
from app.deps import get_cameras_config, get_database
from app.sound_detector import SOUND_CLASSES
from app.system_metrics import system_resources

router = APIRouter()


@router.get('/api/stats')
def stats(request: Request, since: str | None = Query(None), cameras_config=Depends(get_cameras_config), db=Depends(get_database)):
    require_admin(request)
    result = db.stats(since=since)
    result['total_cameras'] = len(cameras_config)
    return result


@router.get('/api/system/resources')
def system_resources_status(request: Request):
    """Host CPU / load-average / RAM snapshot for the dashboard cards."""
    require_admin(request)
    return system_resources()


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
