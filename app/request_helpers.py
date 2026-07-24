"""Request-level helpers extracted from app/main.py.

These helpers were previously defined in app/main.py alongside the FastAPI
application. Moving them here lets router files import them directly without
going through the ``import app.main as main`` hybrid pattern.

The functions do NOT import app.main - they use direct imports only.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs

from fastapi import HTTPException, Request

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


def _parse_header_value(header: str, key: str) -> str | None:
    for part in header.split(';'):
        part = part.strip()
        if part.startswith(f'{key}='):
            return part.split('=', 1)[1].strip('"')
    return None


async def _read_uploaded_image(request: Request) -> tuple[bytes, str | None, str | None]:
    content_type = request.headers.get('content-type', '')
    body = await request.body()
    if content_type.startswith('image/'):
        return (body, None, content_type)
    boundary = _parse_header_value(content_type, 'boundary')
    if not boundary:
        raise HTTPException(status_code=400, detail='Expected multipart image upload')
    delimiter = ('--' + boundary).encode('utf-8')
    for part in body.split(delimiter):
        if b'Content-Disposition' not in part or b'name="file"' not in part:
            continue
        header_blob, separator, payload = part.partition(b'\r\n\r\n')
        if not separator:
            continue
        headers = header_blob.decode('utf-8', errors='replace')
        filename = _parse_header_value(headers, 'filename')
        uploaded_type = None
        for line in headers.splitlines():
            if line.lower().startswith('content-type:'):
                uploaded_type = line.split(':', 1)[1].strip()
                break
        if payload.endswith(b'\r\n'):
            payload = payload[:-2]
        return (payload, filename, uploaded_type)
    raise HTTPException(status_code=400, detail='Multipart upload must include a file field named file')
