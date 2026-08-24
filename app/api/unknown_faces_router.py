"""Unknown faces review APIRouter.

Endpoints for reviewing, assigning, and dismissing unknown face captures
that were detected but not matched to any enrolled person.

Admin-only — managing biometric data is an admin operation.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth_gates import require_admin
from app.db.unknown_faces import UnknownFaceAssignmentError
from app.deps import get_database
from app.face_recognition_service import refresh_face_recognition_matcher
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/unknown-faces')
def list_unknown_faces(
    request: Request,
    status: str = 'pending',
    limit: int = 50,
    offset: int = 0,
    db=Depends(get_database),
):
    """List unknown face captures, newest first."""
    require_admin(request)
    if status not in ('pending', 'assigned', 'dismissed'):
        raise HTTPException(status_code=400, detail='Invalid status filter.')
    limit = max(1, min(100, limit))
    offset = max(0, offset)
    faces = db.list_unknown_faces(status=status, limit=limit, offset=offset)
    total = db.count_unknown_faces(status=status)
    return {'faces': faces, 'total': total, 'limit': limit, 'offset': offset}


@router.get('/api/unknown-faces/{face_id}/thumbnail')
def get_thumbnail(face_id: int, request: Request, db=Depends(get_database)):
    """Return the JPEG thumbnail for an unknown face capture."""
    require_admin(request)
    thumb = db.get_unknown_face_thumbnail(face_id)
    if thumb is None:
        raise HTTPException(status_code=404, detail='Thumbnail not found.')
    return Response(content=thumb, media_type='image/jpeg')


@router.post('/api/unknown-faces/{face_id}/assign')
async def assign_unknown_face(
    face_id: int,
    request: Request,
    db=Depends(get_database),
):
    """Assign an unknown face to an enrolled person (or create a new person).

    Body:
      {"person_id": 123}            — assign to existing person
      {"name": "New Person"}        — create new person + assign
    """
    require_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Request body must be valid JSON.') from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Request body must be a JSON object.')

    raw_person_id = payload.get('person_id')
    new_name = payload.get('name')
    person_id: int | None = None
    if raw_person_id not in (None, ''):
        if isinstance(raw_person_id, bool):
            raise HTTPException(status_code=400, detail='person_id must be a positive integer.')
        try:
            person_id = int(raw_person_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(status_code=400, detail='person_id must be a positive integer.') from exc
        if person_id <= 0:
            raise HTTPException(status_code=400, detail='person_id must be a positive integer.')
    elif new_name is not None:
        name = str(new_name).strip()
        if not name:
            raise HTTPException(status_code=400, detail='A person name is required.')
        # The atomic DB operation creates this person inside the same
        # transaction as the face enrolment and status transition.
        new_name = name
    else:
        raise HTTPException(status_code=400, detail='Provide person_id or name.')

    try:
        result = db.assign_unknown_face_with_embedding(
            face_id, person_id=person_id, person_name=new_name,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnknownFaceAssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    resolved_id = int(result['person_id'])
    refresh_face_recognition_matcher()
    if result['created_person']:
        write_audit_log(
            request, db, 'create', 'person', resource_id=str(resolved_id),
            details={'name': result['person_name']},
        )
    write_audit_log(
        request, db, 'assign', 'unknown_face',
        resource_id=str(face_id),
        details={'person_id': resolved_id, 'person_name': result['person_name']},
    )
    return {'ok': True, 'person_id': resolved_id, 'person_name': result['person_name']}


@router.post('/api/unknown-faces/{face_id}/dismiss')
def dismiss_unknown_face(face_id: int, request: Request, db=Depends(get_database)):
    """Mark an unknown face as dismissed (reviewed, no action taken)."""
    require_admin(request)
    face = db.get_unknown_face(face_id)
    if face is None:
        raise HTTPException(status_code=404, detail='Unknown face not found.')
    if face['status'] != 'pending':
        raise HTTPException(status_code=400, detail='This face has already been reviewed.')
    db.dismiss_unknown_face(face_id)
    write_audit_log(request, db, 'dismiss', 'unknown_face', resource_id=str(face_id))
    return {'ok': True}


@router.delete('/api/unknown-faces/{face_id}')
def delete_unknown_face(face_id: int, request: Request, db=Depends(get_database)):
    """Permanently remove an unknown face capture."""
    require_admin(request)
    if not db.delete_unknown_face(face_id):
        raise HTTPException(status_code=404, detail='Unknown face not found.')
    write_audit_log(request, db, 'delete', 'unknown_face', resource_id=str(face_id))
    return {'ok': True}
