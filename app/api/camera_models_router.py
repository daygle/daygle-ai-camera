"""Camera model assignment APIRouter.

Per-camera YOLO model assignment: each camera can run its own object-detection
model (an installed ONNX file inside ``models/``) instead of the global
default. Assignment is stored in the camera's ``detection`` block
(``model_path`` / ``labels_path``; see ``app/camera_models.py``) and is picked
up by the live pipeline on the next detection cycle.

Routes:
- GET    /api/camera-models              -- current assignments + assignable models
- PUT    /api/camera-models/{camera_id}  -- assign / switch a camera's model
- DELETE /api/camera-models/{camera_id}  -- unassign (back to the global default)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

import app.state as _state
from app.ai_settings import models_dir_file
from app.auth import utc_now
from app.auth_gates import require_admin
from app.camera_id import normalize_camera_id
from app.camera_models import (
    DEFAULT_LABELS_PATH,
    camera_model_row,
    list_assignable_models,
    normalize_camera_labels_path,
    normalize_camera_model_path,
)
from app.config_facades import effective_ai_config, effective_cameras_config
from app.deps import get_apply_cameras_settings, get_database
from app.payload_validators import validate_camera_settings
from app.request_helpers import write_audit_log

router = APIRouter()


def _assignment_payload() -> dict:
    ai_config = effective_ai_config()
    default_model = str(ai_config.get('model_path') or '')
    return {
        'default_model': {
            'model_path': default_model or None,
            'labels_path': str(ai_config.get('labels_path') or DEFAULT_LABELS_PATH),
        },
        'cameras': [
            camera_model_row(camera, ai_config)
            for camera in effective_cameras_config()
        ],
        'models': list_assignable_models(),
    }


def _persist_camera_model(
    camera_id: str,
    model_path: str | None,
    labels_path: str | None,
    request: Request,
    db,
    apply_cameras_settings,
    action: str,
) -> dict:
    """Write one camera's model assignment (or clear it) under the shared
    cameras-config write lock, mirroring ``cameras_router.update_camera``."""
    with _state._cameras_config_write_lock:
        settings_list = list(effective_cameras_config())
        for index, current in enumerate(settings_list):
            if str(current.get('id') or '') != camera_id:
                continue
            detection = dict(current.get('detection') or {})
            if model_path is None:
                # Explicit empty values CLEAR the stored override: the
                # validator merges this detection dict over the stored one,
                # so simply omitting the keys would preserve the previous
                # assignment instead of unassigning.
                detection['model_path'] = ''
                detection['labels_path'] = ''
            else:
                detection['model_path'] = model_path
                detection['labels_path'] = labels_path or DEFAULT_LABELS_PATH
            settings_list[index] = validate_camera_settings(
                {'detection': detection},
                current=current,
                index=index + 1,
            )
            db.set_setting('cameras', settings_list, utc_now())
            apply_cameras_settings(settings_list)
            break
        else:
            raise HTTPException(status_code=404, detail='Camera not found')
    write_audit_log(
        request,
        db,
        'update',
        'settings.camera_model',
        camera_id,
        {
            'action': action,
            'model_path': model_path,
            'labels_path': labels_path,
            'camera_name': settings_list[index].get('name'),
        },
    )
    ai_config = effective_ai_config()
    return camera_model_row(settings_list[index], ai_config)


@router.get('/api/camera-models')
def get_camera_models():
    """Current per-camera assignments plus the installed models to assign."""
    return _assignment_payload()


@router.put('/api/camera-models/{camera_id}')
async def assign_camera_model(
    camera_id: str,
    request: Request,
    db=Depends(get_database),
    apply_cameras_settings=Depends(get_apply_cameras_settings),
):
    """Assign or switch a camera's object-detection model.

    Body: ``{"model_path": "models/yolo11s.onnx", "labels_path": "models/coco.names"}``
    (``labels_path`` optional, default ``models/coco.names``). The model must
    be an installed object-detection ONNX file inside ``models/``; face models
    are rejected because they run in the separate face-detection pass.
    """
    require_admin(request)
    normalized = normalize_camera_id(camera_id)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Expected a JSON object.')
    raw_model = payload.get('model_path')
    if not str(raw_model or '').strip():
        raise HTTPException(
            status_code=400,
            detail='model_path is required; use DELETE to unassign a camera model.',
        )
    model_path = normalize_camera_model_path(raw_model, strict=True)
    labels_path = normalize_camera_labels_path(payload.get('labels_path'), strict=True) or DEFAULT_LABELS_PATH
    # Existence is resolved through the models/ listing (app.ai_settings), not
    # by joining the submitted path onto the application root: the request
    # string only selects a name, the returned Path always comes from the
    # filesystem.
    if models_dir_file(model_path) is None:
        raise HTTPException(
            status_code=404,
            detail=f'Model file not found: {model_path}. Install it on /onnx first.',
        )
    if models_dir_file(labels_path) is None:
        raise HTTPException(status_code=404, detail=f'Labels file not found: {labels_path}.')
    row = _persist_camera_model(
        normalized, str(model_path), labels_path, request, db, apply_cameras_settings, 'assign',
    )
    return {'ok': True, 'camera': row}


@router.delete('/api/camera-models/{camera_id}')
def unassign_camera_model(
    camera_id: str,
    request: Request,
    db=Depends(get_database),
    apply_cameras_settings=Depends(get_apply_cameras_settings),
):
    """Remove a camera's model assignment so it follows the global default."""
    require_admin(request)
    normalized = normalize_camera_id(camera_id)
    row = _persist_camera_model(
        normalized, None, None, request, db, apply_cameras_settings, 'unassign',
    )
    return {'ok': True, 'camera': row}
