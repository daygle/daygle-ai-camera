"""Unknown faces review APIRouter.

Endpoints for reviewing, assigning, and dismissing unknown face captures
that were detected but not matched to any enrolled person.

Admin-only — managing biometric data is an admin operation.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth_gates import require_admin
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
    face = db.get_unknown_face(face_id)
    if face is None:
        raise HTTPException(status_code=404, detail='Unknown face not found.')
    if face['status'] != 'pending':
        raise HTTPException(status_code=400, detail='This face has already been reviewed.')

    payload = await request.json()
    person_id = payload.get('person_id')
    new_name = payload.get('name')

    # Create a new person if name is provided instead of person_id.
    if not person_id and new_name:
        name = str(new_name).strip()
        if not name:
            raise HTTPException(status_code=400, detail='A person name is required.')
        person_id = db.add_person(name)
        write_audit_log(request, db, 'create', 'person', resource_id=str(person_id), details={'name': name})
    elif not person_id:
        raise HTTPException(status_code=400, detail='Provide person_id or name.')

    person = db.get_person(int(person_id))
    if person is None:
        raise HTTPException(status_code=404, detail='Person not found.')

    # Copy the embedding from the unknown face to the person's enrolment.
    emb_data = db.get_unknown_face_embedding(face_id)
    if emb_data:
        db.add_person_face(
            int(person_id),
            embedding=emb_data['embedding'],
            dim=emb_data['dim'],
            model=emb_data['model'],
            source_snapshot=f'unknown-face:{face_id}',
        )
        refresh_face_recognition_matcher()

    db.assign_unknown_face(face_id, int(person_id))
    write_audit_log(
        request, db, 'assign', 'unknown_face',
        resource_id=str(face_id),
        details={'person_id': int(person_id), 'person_name': person['name']},
    )
    return {'ok': True, 'person_id': int(person_id), 'person_name': person['name']}


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
