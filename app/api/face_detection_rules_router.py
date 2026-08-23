"""Face detection rules APIRouter.

CRUD for per-person face detection rules with email/push alert toggles.
Rules are stored under the ``face_detection_rules`` key in the settings
database alongside the other face-recognition settings.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.auth import utc_now
from app.auth_gates import require_admin
from app.deps import get_database
from app.face_detection_rules import effective_face_detection_rules, validate_face_detection_rules
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/settings/face-detection-rules')
def get_face_detection_rules(request: Request):
    require_admin(request)
    return effective_face_detection_rules()


@router.put('/api/settings/face-detection-rules')
async def update_face_detection_rules(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await request.json()
    new_rules = validate_face_detection_rules(payload)
    db.set_setting('face_detection_rules', new_rules, utc_now())
    write_audit_log(request, db, 'update', 'settings.face_detection_rules')
    return new_rules
