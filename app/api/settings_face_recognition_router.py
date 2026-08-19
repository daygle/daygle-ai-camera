"""Settings / Face-recognition APIRouter (Stage 2b).

Admin-only endpoints to configure the recognition backend: read/write the
settings and reload the service. Recognition is off by default and does nothing
until an admin enables it and selects a model.

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
from app.embedding_models import EMBEDDING_MODELS, embedding_model_catalog
from app.face_recognition_settings import face_recognition_status, validate_face_recognition_settings
from app.model_management import _download_weights, _relative_model_path, _safe_within_models_dir
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


@router.get('/api/settings/face-recognition/embedding-models')
def list_embedding_models(request: Request):
    require_admin(request)

    def _installed(onnx_name: str) -> bool:
        try:
            return _safe_within_models_dir(onnx_name).exists()
        except Exception:
            return False

    return {'models': embedding_model_catalog(_installed)}


@router.post('/api/settings/face-recognition/embedding-models/{catalog_id}/download')
async def download_embedding_model(
    catalog_id: str,
    request: Request,
    db=Depends(get_database),
    reload_service=Depends(get_reload_face_recognition),
):
    """Download a catalog embedding model and point recognition at it.

    The download URL is taken from the fixed :data:`EMBEDDING_MODELS` catalog
    keyed by ``catalog_id`` -- it is never supplied by the caller, so there is no
    SSRF surface. On success the model becomes the active embedding model
    (``model_path`` + ``model_id``); recognition stays disabled until an admin
    explicitly enables it.
    """
    require_admin(request)
    info = EMBEDDING_MODELS.get(catalog_id)
    if info is None:
        raise HTTPException(status_code=404, detail='Unknown embedding model.')
    destination = _safe_within_models_dir(info['onnx'])
    try:
        await run_in_threadpool(_download_weights, info['url'], destination)
    except RuntimeError as exc:
        logger.warning('Embedding model download failed for %s: %s', catalog_id, exc)
        raise HTTPException(status_code=502, detail='Embedding model download failed.') from exc
    rel_path = _relative_model_path(destination)
    new_settings = validate_face_recognition_settings({
        **effective_face_recognition_config(),
        'model_path': rel_path,
        'model_id': info['model_id'],
    })
    db.set_setting('face_recognition', new_settings, utc_now())
    available, reason = reload_service(new_settings)
    from app.face_recognition_service import get_face_recognition_service as _get_service

    response = face_recognition_status(new_settings, _get_service(), db)
    response['reload_succeeded'] = available
    response['reload_error'] = reason
    write_audit_log(request, db, 'download', 'settings.face_recognition.embedding_model', details={
        'catalog_id': catalog_id,
        'model_path': rel_path,
        'model_id': info['model_id'],
    })
    return response
