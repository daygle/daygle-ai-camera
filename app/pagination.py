"""Opaque keyset cursors for stable list pagination."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 2048


def encode_cursor(resource: str, order: str, timestamp: str, row_id: int) -> str:
    """Encode the last visible row as a versioned, resource-bound cursor."""
    payload = {
        'v': _CURSOR_VERSION,
        'resource': str(resource),
        'order': str(order),
        'timestamp': str(timestamp),
        'id': int(row_id),
    }
    raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_cursor(value: str, resource: str, order: str) -> tuple[str, int]:
    """Decode and validate a cursor, returning ``(timestamp, row_id)``.

    Cursors are deliberately bound to both the list resource and sort direction so
    a client cannot accidentally reuse a recordings cursor on snapshots or mix a
    newest cursor into an oldest traversal.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_CURSOR_LENGTH:
        raise ValueError('Invalid cursor')
    try:
        padded = value + ('=' * (-len(value) % 4))
        payload: Any = json.loads(base64.b64decode(padded, altchars=b'-_', validate=True))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError('Invalid cursor') from exc
    if not isinstance(payload, dict):
        raise ValueError('Invalid cursor')
    if payload.get('v') != _CURSOR_VERSION:
        raise ValueError('Unsupported cursor version')
    if payload.get('resource') != resource or payload.get('order') != order:
        raise ValueError('Cursor does not match this list ordering')
    timestamp = payload.get('timestamp')
    row_id = payload.get('id')
    if not isinstance(timestamp, str) or not timestamp or len(timestamp) > 128:
        raise ValueError('Invalid cursor')
    if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
        raise ValueError('Invalid cursor')
    return timestamp, row_id
