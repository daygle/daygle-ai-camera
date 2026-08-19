"""Settings / Face-recognition APIRouter (Stage 2b).

Admin-only endpoints to configure the recognition backend: read/write the
settings and reload the service. Recognition is off by default and does nothing
until an admin enables it and selects a model.

Enrolling people and matching faces on the live stream are later slices; this
router only manages the capability's configuration.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.auth import utc_now
from app.auth_gates import require_admin
from app.config_facades import effective_face_recognition_config
from app.deps import get_database, get_face_recognition_service, get_reload_face_recognition
from app.face_recognition_settings import face_recognition_status, validate_face_recognition_settings
from app.request_helpers import write_audit_log

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

# NOTE: an embedding model is supplied out of band (placed in ``models/`` and
# selected via ``model_path``), not fetched by this router. A server-side
# "download from a URL the caller provides" endpoint would be an SSRF surface;
# one-click download belongs with a specific bundled model + a fixed, trusted
# source URL (the same pattern used for the detection models), not an
# operator-typed URL. See docs for the manual model-setup steps.
