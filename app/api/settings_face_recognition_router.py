"""Settings / Face-recognition APIRouter (Stage 2b).

Admin-only endpoints to configure the recognition backend: read/write the
settings, reload the service, and download an embedding model. Recognition is
off by default and does nothing until an admin enables it and selects a model.

Enrolling people and matching faces on the live stream are later slices; this
router only manages the capability's configuration.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.auth import utc_now
from app.auth_gates import require_admin
from app.config_facades import effective_face_recognition_config
from app.deps import get_database, get_face_recognition_service, get_reload_face_recognition
from app.face_recognition_settings import face_recognition_status, validate_face_recognition_settings
from app.model_management import (
    _download_weights,
    _relative_model_path,
    _safe_within_models_dir,
)
from app.request_helpers import write_audit_log

logger = logging.getLogger('daygle.ai')

router = APIRouter()


@router.get('/api/settings/face-recognition')
def get_face_recognition_settings(
    request: Request,
    db=Depends(get_database),
    service=Depends(get_face_recognition_service),
):
    require_admin(request)
    return face_recognition_status(effective_face_recognition_config(), service, db)


@router.put('/api/settings/face-recognition')
async def update_face_recognition_settings(
    request: Request,
    db=Depends(get_database),
    reload_service=Depends(get_reload_face_recognition),
):
    require_admin(request)
    payload = await request.json()
    new_settings = validate_face_recognition_settings(payload)
    db.set_setting('face_recognition', new_settings, utc_now())
    available, reason = reload_service(new_settings)
    from app.face_recognition_service import get_face_recognition_service as _get_service

    response = face_recognition_status(new_settings, _get_service(), db)
    response['reload_succeeded'] = available
    response['reload_error'] = reason
    write_audit_log(request, db, 'update', 'settings.face_recognition', details={
        'enabled': new_settings.get('enabled'),
        'model_path': new_settings.get('model_path'),
        'model_id': new_settings.get('model_id'),
    })
    return response


@router.post('/api/settings/face-recognition/reload')
def reload_face_recognition(
    request: Request,
    db=Depends(get_database),
    reload_service=Depends(get_reload_face_recognition),
):
    require_admin(request)
    available, reason = reload_service(None)
    from app.face_recognition_service import get_face_recognition_service as _get_service

    response = face_recognition_status(effective_face_recognition_config(), _get_service(), db)
    response['reload_succeeded'] = available
    response['reload_error'] = reason
    return response


@router.post('/api/settings/face-recognition/download-model')
async def download_face_recognition_model(
    request: Request,
    db=Depends(get_database),
):
    """Download an embedding model (ONNX) from an explicit https URL.

    The application does not bundle a face-embedding model -- ArcFace/
    InsightFace weights carry their own (typically non-commercial) licenses, so
    the operator supplies the source. The file is written into ``models/`` under
    a validated basename; the caller then points the settings at it.
    """
    require_admin(request)
    payload = await request.json()
    url = str(payload.get('url') or '').strip()
    filename = str(payload.get('filename') or '').strip()
    if not url:
        raise HTTPException(status_code=400, detail='A model download url is required.')
    if not filename.endswith('.onnx'):
        raise HTTPException(status_code=400, detail='Model filename must end in .onnx.')
    try:
        destination = _safe_within_models_dir(filename)
    except Exception as exc:
        raise HTTPException(status_code=400, detail='Invalid model filename.') from exc
    try:
        await run_in_threadpool(_download_weights, url, destination)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f'Model download failed: {exc}') from exc
    rel_path = _relative_model_path(destination)
    write_audit_log(request, db, 'download', 'settings.face_recognition.model', details={
        'model_path': rel_path,
    })
    return {'ok': True, 'model_path': rel_path}
