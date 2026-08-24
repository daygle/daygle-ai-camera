"""People / face-enrolment APIRouter (Stage 2c).

Admin-only CRUD for enrolled people and their face embeddings. Enrolling a face
embeds an uploaded image (optionally cropped to a face box) with the active
recognition model and stores the vector; the live matcher is refreshed on every
change so recognition picks up edits immediately.

Managing biometric data is admin-only and audited. Deleting a person removes
all of their embeddings.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.auth_gates import require_admin
from app.deps import get_database, get_face_recognition_service
from app.face_recognition import (
    crop_face_region,
    decode_bgr_image,
    embedding_to_bytes,
    encode_face_thumbnail,
)
from app.face_recognition_service import refresh_face_recognition_matcher
from app.request_helpers import write_audit_log, _read_uploaded_image

router = APIRouter()

_MAX_NAME_LEN = 200
_MAX_NOTES_LEN = 2000


def _clean_name(value: object) -> str:
    name = str(value or '').strip()
    if not name:
        raise HTTPException(status_code=400, detail='A person name is required.')
    if len(name) > _MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail=f'Name must be at most {_MAX_NAME_LEN} characters.')
    return name


def _clean_notes(value: object) -> str | None:
    if value is None:
        return None
    notes = str(value).strip()
    if len(notes) > _MAX_NOTES_LEN:
        raise HTTPException(status_code=400, detail=f'Notes must be at most {_MAX_NOTES_LEN} characters.')
    return notes


@router.get('/api/persons')
def list_persons(request: Request, db=Depends(get_database)):
    require_admin(request)
    return {'persons': db.list_persons()}


@router.post('/api/persons')
async def create_person(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await request.json()
    name = _clean_name(payload.get('name'))
    notes = _clean_notes(payload.get('notes'))
    person_id = db.add_person(name, notes=notes)
    write_audit_log(request, db, 'create', 'person', resource_id=str(person_id), details={'name': name})
    return db.get_person(person_id)


@router.get('/api/persons/{person_id}')
def get_person(person_id: int, request: Request, db=Depends(get_database)):
    require_admin(request)
    person = db.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail='Person not found.')
    person['faces'] = db.list_person_faces(person_id)
    return person


@router.patch('/api/persons/{person_id}')
async def update_person(person_id: int, request: Request, db=Depends(get_database)):
    require_admin(request)
    if db.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail='Person not found.')
    payload = await request.json()
    name = _clean_name(payload.get('name')) if 'name' in payload else None
    notes = _clean_notes(payload.get('notes')) if 'notes' in payload else None
    db.update_person(person_id, name=name, notes=notes)
    write_audit_log(request, db, 'update', 'person', resource_id=str(person_id))
    return db.get_person(person_id)


@router.delete('/api/persons/{person_id}')
def delete_person(person_id: int, request: Request, db=Depends(get_database)):
    require_admin(request)
    if not db.delete_person(person_id):
        raise HTTPException(status_code=404, detail='Person not found.')
    refresh_face_recognition_matcher()
    write_audit_log(request, db, 'delete', 'person', resource_id=str(person_id))
    return {'ok': True}


@router.post('/api/persons/{person_id}/faces')
async def enroll_face(
    person_id: int,
    request: Request,
    db=Depends(get_database),
    service=Depends(get_face_recognition_service),
    x: int | None = Query(default=None),
    y: int | None = Query(default=None),
    width: int | None = Query(default=None),
    height: int | None = Query(default=None),
):
    """Enrol a face for a person from an uploaded image (optionally cropped).

    Requires recognition to be enabled with a loaded model -- the image is
    embedded with that model and the vector stored, tagged with the model id so
    it is only ever matched against the same model.
    """
    require_admin(request)
    if db.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail='Person not found.')
    if not service.available:
        raise HTTPException(
            status_code=400,
            detail='Face recognition is not ready. Enable it and select an embedding model first.',
        )
    image_bytes, _filename, _content_type = await _read_uploaded_image(request)
    try:
        image = decode_bgr_image(image_bytes)
        box = None
        if None not in (x, y, width, height):
            box = {'x': x, 'y': y, 'width': width, 'height': height}
        crop = crop_face_region(image, box)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    vector = service.embed_face(crop)
    if vector is None:
        raise HTTPException(
            status_code=400,
            detail='Could not embed the face (image too small or model unavailable).',
        )
    dim = int(vector.shape[0])
    # Keep a small JPEG of the enrolled crop so the People page can show what
    # was enrolled. Encoding never raises (returns None on failure), so a
    # thumbnail hiccup never blocks storing the embedding.
    thumbnail = encode_face_thumbnail(crop)
    face_id = db.add_person_face(
        person_id,
        embedding=embedding_to_bytes(vector),
        dim=dim,
        model=service.model_id,
        thumbnail=thumbnail,
    )
    refresh_face_recognition_matcher()
    write_audit_log(request, db, 'enroll', 'person.face', resource_id=str(person_id), details={'face_id': face_id})
    return {'ok': True, 'face_id': face_id, 'faces': db.list_person_faces(person_id)}


@router.get('/api/persons/{person_id}/faces/{face_id}/thumbnail')
def get_face_thumbnail(person_id: int, face_id: int, request: Request, db=Depends(get_database)):
    """Serve the stored JPEG thumbnail for an enrolled face (admin-only).

    Biometric imagery, so this is gated the same as the rest of the People
    API. Returns 404 for an unknown face, a face belonging to a different
    person, or a face enrolled before thumbnails were captured.
    """
    require_admin(request)
    faces = db.list_person_faces(person_id)
    if not any(int(face['id']) == face_id for face in faces):
        raise HTTPException(status_code=404, detail='Face not found.')
    image_bytes = db.get_person_face_thumbnail(face_id)
    if not image_bytes:
        raise HTTPException(status_code=404, detail='No thumbnail for this face.')
    return Response(
        content=image_bytes,
        media_type='image/jpeg',
        headers={'Cache-Control': 'private, max-age=300'},
    )


@router.delete('/api/persons/{person_id}/faces/{face_id}')
def delete_face(person_id: int, face_id: int, request: Request, db=Depends(get_database)):
    require_admin(request)
    # Confirm the face belongs to THIS person before deleting. ``delete_person_face``
    # removes by face id alone, so without this check a mismatched URL
    # (``/persons/<A>/faces/<face-of-B>``) would delete B's face and audit it
    # under A. Mirrors the ownership check on the thumbnail endpoint above.
    if not any(int(face['id']) == face_id for face in db.list_person_faces(person_id)):
        raise HTTPException(status_code=404, detail='Face not found.')
    if not db.delete_person_face(face_id):
        raise HTTPException(status_code=404, detail='Face not found.')
    refresh_face_recognition_matcher()
    write_audit_log(request, db, 'delete', 'person.face', resource_id=str(person_id), details={'face_id': face_id})
    return {'ok': True}
