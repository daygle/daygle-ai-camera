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


def _embedding_model_installed(onnx_name: str) -> bool:
    try:
        return _safe_within_models_dir(onnx_name).exists()
    except Exception:
        return False


def _embedding_models_response(db, *, reload_succeeded=None, reload_error=None) -> dict:
    """Build the combined status + catalog payload the models UI consumes.

    Every mutating action (download / use / update / delete) returns this so the
    page can re-render both the recognition status header and the model cards
    (installed/active flags) from a single round-trip.
    """
    config = effective_face_recognition_config()
    active_path = str(config.get('model_path') or '')

    def _active(onnx_name: str) -> bool:
        try:
            return bool(active_path) and _relative_model_path(_safe_within_models_dir(onnx_name)) == active_path
        except Exception:
            return False

    from app.face_recognition_service import get_face_recognition_service as _get_service

    response = face_recognition_status(config, _get_service(), db)
    response['models'] = embedding_model_catalog(_embedding_model_installed, _active)
    if reload_succeeded is not None:
        response['reload_succeeded'] = reload_succeeded
        response['reload_error'] = reload_error
    return response


def _activate_embedding_model(info: dict, db, reload_service) -> tuple[str, bool, str | None]:
    """Point the persisted recognition settings at ``info``'s model and reload.

    Returns ``(relative_model_path, reload_succeeded, reload_error)``. Recognition
    stays disabled until an admin explicitly enables it -- selecting a model only
    sets which model would be used.
    """
    destination = _safe_within_models_dir(info['onnx'])
    rel_path = _relative_model_path(destination)
    new_settings = validate_face_recognition_settings({
        **effective_face_recognition_config(),
        'model_path': rel_path,
        'model_id': info['model_id'],
    })
    db.set_setting('face_recognition', new_settings, utc_now())
    available, reason = reload_service(new_settings)
    return rel_path, available, reason


@router.get('/api/settings/face-recognition/embedding-models')
def list_embedding_models(request: Request, db=Depends(get_database)):
    require_admin(request)
    return _embedding_models_response(db)


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
    rel_path, available, reason = _activate_embedding_model(info, db, reload_service)
    write_audit_log(request, db, 'download', 'settings.face_recognition.embedding_model', details={
        'catalog_id': catalog_id,
        'model_path': rel_path,
        'model_id': info['model_id'],
    })
    return _embedding_models_response(db, reload_succeeded=available, reload_error=reason)


@router.post('/api/settings/face-recognition/embedding-models/{catalog_id}/select')
def select_embedding_model(
    catalog_id: str,
    request: Request,
    db=Depends(get_database),
    reload_service=Depends(get_reload_face_recognition),
):
    """Point recognition at an already-installed embedding model ("Use").

    No download: the model must already be on disk. Recognition stays disabled
    until an admin enables it -- this only switches which model is used.
    """
    require_admin(request)
    info = EMBEDDING_MODELS.get(catalog_id)
    if info is None:
        raise HTTPException(status_code=404, detail='Unknown embedding model.')
    if not _embedding_model_installed(info['onnx']):
        raise HTTPException(status_code=400, detail='Model is not installed. Download it first.')
    rel_path, available, reason = _activate_embedding_model(info, db, reload_service)
    write_audit_log(request, db, 'update', 'settings.face_recognition.embedding_model', details={
        'catalog_id': catalog_id,
        'action': 'select',
        'model_path': rel_path,
        'model_id': info['model_id'],
    })
    return _embedding_models_response(db, reload_succeeded=available, reload_error=reason)


@router.post('/api/settings/face-recognition/embedding-models/{catalog_id}/update')
async def update_embedding_model(
    catalog_id: str,
    request: Request,
    db=Depends(get_database),
    reload_service=Depends(get_reload_face_recognition),
):
    """Re-download an installed embedding model's file (repair / refresh).

    These are fixed pre-built files with no version feed, so "update" re-fetches
    the trusted catalog URL over the existing file. If the refreshed model is the
    active one, the service is reloaded so the new bytes take effect; otherwise
    the active selection is left unchanged.
    """
    require_admin(request)
    info = EMBEDDING_MODELS.get(catalog_id)
    if info is None:
        raise HTTPException(status_code=404, detail='Unknown embedding model.')
    if not _embedding_model_installed(info['onnx']):
        raise HTTPException(status_code=400, detail='Model is not installed. Download it first.')
    destination = _safe_within_models_dir(info['onnx'])
    try:
        await run_in_threadpool(_download_weights, info['url'], destination)
    except RuntimeError as exc:
        logger.warning('Embedding model update failed for %s: %s', catalog_id, exc)
        raise HTTPException(status_code=502, detail='Embedding model update failed.') from exc
    available = reason = None
    if _relative_model_path(destination) == str(effective_face_recognition_config().get('model_path') or ''):
        # Refreshed the active model -> reload so the new bytes are used.
        available, reason = reload_service(None)
    write_audit_log(request, db, 'update', 'settings.face_recognition.embedding_model', details={
        'catalog_id': catalog_id,
        'action': 'update',
    })
    return _embedding_models_response(db, reload_succeeded=available, reload_error=reason)


@router.delete('/api/settings/face-recognition/embedding-models/{catalog_id}')
def delete_embedding_model(
    catalog_id: str,
    request: Request,
    db=Depends(get_database),
):
    """Delete an installed embedding model's file.

    Refuses to delete the model recognition is currently pointed at -- switch to
    another model (or clear the selection) first -- so recognition never ends up
    referencing a missing file.
    """
    require_admin(request)
    info = EMBEDDING_MODELS.get(catalog_id)
    if info is None:
        raise HTTPException(status_code=404, detail='Unknown embedding model.')
    destination = _safe_within_models_dir(info['onnx'])
    if not destination.exists():
        raise HTTPException(status_code=404, detail='Model is not installed.')
    if _relative_model_path(destination) == str(effective_face_recognition_config().get('model_path') or ''):
        raise HTTPException(
            status_code=400,
            detail='This model is in use. Select a different model before deleting it.',
        )
    destination.unlink()
    write_audit_log(request, db, 'delete', 'settings.face_recognition.embedding_model', details={
        'catalog_id': catalog_id,
        'onnx': info['onnx'],
    })
    return _embedding_models_response(db)
