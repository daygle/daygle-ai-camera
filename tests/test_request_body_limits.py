"""Regression tests: JSON request bodies and ONVIF replies are size-bounded."""

from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.ptz as ptz
from app.request_helpers import MAX_JSON_BYTES, read_json_body, read_json_object


def _request(body: bytes, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    """Build a Starlette request whose body is delivered in one ASGI message."""
    sent = {'done': False}

    async def receive():
        if sent['done']:
            return {'type': 'http.disconnect'}
        sent['done'] = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    scope = {
        'type': 'http',
        'method': 'POST',
        'path': '/api/test',
        'headers': headers or [],
    }
    return Request(scope, receive)


def test_json_body_under_cap_is_parsed():
    request = _request(b'{"name": "front door"}', [(b'content-length', b'21')])
    assert asyncio.run(read_json_object(request)) == {'name': 'front door'}


def test_json_body_with_declared_oversize_is_rejected_before_reading():
    request = _request(b'{}', [(b'content-length', str(MAX_JSON_BYTES + 1).encode())])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_json_body(request))
    assert exc.value.status_code == 413


def test_json_body_without_content_length_is_capped_while_streaming():
    # Chunked/undeclared bodies must still hit the cumulative cap.
    oversized = b'[' + b' ' * MAX_JSON_BYTES + b']'
    request = _request(oversized)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_json_body(request))
    assert exc.value.status_code == 413


def test_malformed_json_stays_a_client_error():
    request = _request(b'{not json', [(b'content-length', b'9')])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_json_body(request))
    assert exc.value.status_code == 400


def test_onvif_reply_within_cap_is_decoded():
    reply = io.BytesIO(b'<soap>ok</soap>')
    assert ptz._read_onvif_response(reply) == '<soap>ok</soap>'


def test_onvif_reply_over_cap_is_refused():
    oversized = io.BytesIO(b'x' * (ptz._MAX_ONVIF_RESPONSE_BYTES + 10))
    with pytest.raises(OSError, match='exceeds'):
        ptz._read_onvif_response(oversized)
