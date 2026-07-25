"""Request-level helpers extracted from app/main.py.

These helpers were previously defined in app/main.py alongside the FastAPI
application. Moving them here lets router files import them directly without
going through the ``import app.main as main`` hybrid pattern.

The functions do NOT import app.main - they use direct imports only.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs

from fastapi import HTTPException, Request

from app.auth import utc_now
from app.auth_gates import _request_ip
from app.database import EventDatabase

# Audit-log redactor: any key whose lowercase name matches one of these
# patterns has its value replaced by ``***`` so credentials never reach
# SQLite (and therefore never reach DB backups, ops copies, or sysadmin
# reads). Substring matching is intentional: redacting ``smtp_token``,
# ``tokenizer`` or ``client_secret`` is safer than missing an oddly-cased
# credential. ``_redact_audit_details`` recurses into nested dicts/lists
# and leaves non-container scalars (int, bool, float, None) untouched.
_REDACT_AUDIT_KEY_REGEX = re.compile(
    r'password|secret|token|api[_-]?key|access[_-]?key|credential',
    re.IGNORECASE,
)
_REDACTED_PLACEHOLDER = '***'


def _redact_audit_details(details: Any) -> Any:
    """Recursively redact values whose key matches the secret-name regex.

    Walks ``details`` (a ``dict``, ``list``, or scalar) and returns a deep
    copy where every key whose name matches ``_REDACT_AUDIT_KEY_REGEX`` has
    its value replaced by ``_REDACTED_PLACEHOLDER``. Lists are traversed
    element-by-element; non-container scalars are returned unchanged.
    """
    if isinstance(details, dict):
        redacted: dict[str, Any] = {}
        for key, value in details.items():
            if isinstance(key, str) and _REDACT_AUDIT_KEY_REGEX.search(key):
                redacted[key] = _REDACTED_PLACEHOLDER
            else:
                redacted[key] = _redact_audit_details(value)
        return redacted
    if isinstance(details, list):
        return [_redact_audit_details(item) for item in details]
    if isinstance(details, tuple):
        return tuple(_redact_audit_details(item) for item in details)
    return details

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

    The ``details`` dict is passed through ``_redact_audit_details`` BEFORE the
    INSERT so credentials (SMTP password, ntfy token, API keys, etc.) never
    land in the ``audit_log.details`` column. Stripping at write time means
    backups, ops copies, and ad-hoc ``SELECT * FROM audit_log`` queries can
    no longer leak plaintext secrets.
    """
    user: dict[str, Any] | None = getattr(request.state, 'user', None)
    user_id: int | None = int(user['id']) if user else None
    username: str = str(user['username']) if user else 'anonymous'
    safe_details = _redact_audit_details(details)
    try:
        database.add_audit_log(
            created_at=utc_now(),
            user_id=user_id,
            username=username,
            action=action,
            resource=resource,
            resource_id=str(resource_id) if resource_id is not None else None,
            details=safe_details,
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


# H3 fix: cap the maximum size of an uploaded image. The previous
# implementation read ``await request.body()`` directly, which is
# unbounded -- a multi-gigabyte POST to ``/api/detect/frame`` (admin-only
# today, but the same helper is reachable from any future mutating
# endpoint) would consume all available RAM and crash the process.
# 10 MB is comfortably more than any reasonable detector test image
# (the largest legitimate test frame we have seen is well under 1 MB).
MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024


async def _read_uploaded_image(request: Request) -> tuple[bytes, str | None, str | None]:
    content_type = request.headers.get('content-type', '')
    # Early-reject any request whose declared size exceeds the cap.
    # Check the Content-Length header BEFORE reading the body so a
    # hostile client cannot force a 100MB allocation just to get
    # rejected at the parse step.
    declared_length = request.headers.get('content-length')
    if declared_length is not None:
        try:
            if int(declared_length) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Uploaded image exceeds {MAX_UPLOAD_BYTES} bytes (Content-Length: {declared_length}).",
                )
        except ValueError:
            # Malformed Content-Length -> fall through to the streaming cap below.
            pass
    # Defence-in-depth: read in chunks and abort if the cumulative
    # size exceeds the cap even if the client lied about Content-Length
    # (or sent Transfer-Encoding: chunked with no Content-Length at
    # all). This protects against header-stripping / chunk-encoding
    # bypass of the upfront check.
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Uploaded image exceeds {MAX_UPLOAD_BYTES} bytes.",
            )
        chunks.append(chunk)
    body = b''.join(chunks)
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
