"""Object-label groups APIRouter.

Create / edit / remove the umbrella object groups used by zone allow-lists and
object rules (e.g. ``animal`` -> cat/dog/...). Stores the whole group map under
the ``label_groups`` database setting and refreshes the process-wide matching
cache after every write.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.ai_settings import detector_status
from app.auth import utc_now
from app.auth_gates import require_admin
from app.config_facades import effective_ai_config
from app.deps import get_database
from app.label_groups import (
    _MAX_GROUPS,
    _MAX_MEMBERS_PER_GROUP,
    _RESERVED_GROUP_NAMES,
    cached_label_groups,
    normalize_label_groups,
    refresh_label_groups,
)
from app.request_helpers import write_audit_log

logger = logging.getLogger('daygle.ai')

router = APIRouter()


def _available_labels() -> list[str]:
    return detector_status(effective_ai_config()).get('available_labels') or []


def _serialized_groups() -> dict[str, list[str]]:
    return {
        name: sorted(members)
        for name, members in cached_label_groups().items()
    }


@router.get('/api/settings/label_groups')
def get_label_groups(request: Request):
    require_admin(request)
    return {
        'groups': _serialized_groups(),
        'available_labels': _available_labels(),
    }


@router.put('/api/settings/label_groups')
async def update_label_groups(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Payload must be a JSON object.')

    raw_groups = payload.get('groups', {})
    if raw_groups is None:
        raw_groups = {}
    if not isinstance(raw_groups, dict):
        raise HTTPException(
            status_code=400,
            detail='groups must be an object mapping a group name to its member labels.',
        )

    groups: dict[str, list[str]] = {}
    member_labels: set[str] = set()
    for raw_name, raw_members in raw_groups.items():
        name = str(raw_name or '').strip().lower()
        if not name:
            raise HTTPException(status_code=400, detail='Group names must not be empty.')
        if ' ' in name or ',' in name:
            raise HTTPException(
                status_code=400,
                detail=f"Group name '{raw_name}' must be a single word (no spaces or commas).",
            )
        if name in _RESERVED_GROUP_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"'{name}' is reserved and cannot be used as a group name.",
            )
        if len(groups) >= _MAX_GROUPS:
            raise HTTPException(
                status_code=400,
                detail=f'At most {_MAX_GROUPS} groups are allowed.',
            )
        members = _validate_members(name, raw_members)
        groups[name] = members
        member_labels.update(members)

    # A group name that is also some group's member would silently broaden a
    # concrete-label rule (a group named "cat" would make a "cat" rule match
    # the group's other members), so reject the collision.
    for name in groups:
        if name in member_labels:
            raise HTTPException(
                status_code=400,
                detail=f"Group name '{name}' collides with a member label; choose a different name.",
            )

    normalized = normalize_label_groups(groups)
    # Re-validate after normalization so a group that canonicalizes away (e.g.
    # only empty/reserved members) is reported rather than persisted empty.
    for name in groups:
        if name not in normalized:
            raise HTTPException(
                status_code=400,
                detail=f"Group '{name}' has no valid member labels.",
            )

    db.set_setting('label_groups', normalized, utc_now())
    refresh_label_groups()
    write_audit_log(request, db, 'update', 'settings.label_groups', details={
        'groups': sorted(normalized),
    })
    return {
        'groups': {name: sorted(members) for name, members in normalized.items()},
        'available_labels': _available_labels(),
    }


def _validate_members(group_name: str, raw_members: Any) -> list[str]:
    """Validate one group's member list and return the canonical sorted list."""
    if isinstance(raw_members, str):
        raw_members = raw_members.split(',')
    if not isinstance(raw_members, (list, tuple, set, frozenset)):
        raise HTTPException(
            status_code=400,
            detail=f"Group '{group_name}' members must be a list of labels.",
        )
    members: list[str] = []
    seen: set[str] = set()
    for raw in raw_members:
        member = str(raw or '').strip().lower()
        if not member:
            continue
        if member in _RESERVED_GROUP_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"'{member}' is reserved and cannot be a group member.",
            )
        if member == group_name:
            raise HTTPException(
                status_code=400,
                detail=f"Group '{group_name}' cannot contain itself as a member.",
            )
        if member in seen:
            continue
        if len(members) >= _MAX_MEMBERS_PER_GROUP:
            raise HTTPException(
                status_code=400,
                detail=f"Group '{group_name}' has more than {_MAX_MEMBERS_PER_GROUP} members.",
            )
        members.append(member)
        seen.add(member)
    if not members:
        raise HTTPException(
            status_code=400,
            detail=f"Group '{group_name}' has no valid member labels.",
        )
    return sorted(members)
