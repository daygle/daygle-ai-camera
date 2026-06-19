"""Request-level helpers extracted from app/main.py.

These helpers were previously defined in app/main.py alongside the FastAPI
application. Moving them here lets router files import them directly without
going through the ``import app.main as main`` hybrid pattern.

The functions do NOT import app.main — they use direct imports only.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs

from fastapi import Request

from app.auth import utc_now
from app.auth_gates import _request_ip
from app.database import EventDatabase

logger = logging.getLogger('daygle.ai')


async def form_data(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded (or plain text) body into a dict."""
    body = (await request.body()).decode('utf-8')
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}


def write_audit_log(
    request: Request,
    database: EventDatabase,
    action: str,
    resource: str,
    resource_id: Any = None,
    details: dict[str, Any] | None = None,
    status: str = 'success',
) -> None:
    """Write a single audit-log row.

    Unlike the original app/main.py version, *database* is an explicit parameter
    so callers do not need to reach through the module-level singleton.
    """
    user: dict[str, Any] | None = getattr(request.state, 'user', None)
    user_id: int | None = int(user['id']) if user else None
    username: str = str(user['username']) if user else 'anonymous'
    try:
        database.add_audit_log(
            created_at=utc_now(),
            user_id=user_id,
            username=username,
            action=action,
            resource=resource,
            resource_id=str(resource_id) if resource_id is not None else None,
            details=details,
            ip_address=_request_ip(request),
            status=status,
        )
    except Exception as exc:
        logger.warning('Failed to write audit log: %s', exc)
