"""Async API routes must not run synchronous database work on the event loop.

An ``async def`` route executes on the event loop itself, so a synchronous
SQLite write inside it (``db.set_setting``, ``write_audit_log`` ...) can stall
every other request and live stream for up to the 30 s busy timeout while a
recording or detection thread holds the write lock. Such routes are plain
``def`` (FastAPI runs them in its threadpool; read a JSON body with the
``admin_json_object`` dependency) or push the work through
``run_in_threadpool``.
"""
from __future__ import annotations

import ast
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1] / 'app' / 'api'
_SYNC_DB_MARKERS = ('db.', 'write_audit_log', '.connect(')


def _blocking_async_routes() -> list[str]:
    offenders: list[str] = []
    for path in sorted(API_DIR.glob('*.py')):
        source = path.read_text(encoding='utf-8')
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            body = ast.get_source_segment(source, node) or ''
            if 'run_in_threadpool' in body:
                continue
            if any(marker in body for marker in _SYNC_DB_MARKERS):
                offenders.append(f'{path.name}:{node.lineno} {node.name}')
    return offenders


def test_async_routes_do_not_do_synchronous_database_work():
    assert _blocking_async_routes() == []


def test_admin_json_object_gates_before_reading_the_body():
    import asyncio

    import pytest
    from fastapi import HTTPException

    import app.request_helpers as helpers

    class _Request:
        def __init__(self) -> None:
            self.body_read = False

    request = _Request()

    def deny(_request):
        raise HTTPException(status_code=403, detail='Admin access required')

    async def read(_request):
        request.body_read = True
        return {}

    original_gate, original_read = helpers.require_admin, helpers.read_json_object
    helpers.require_admin, helpers.read_json_object = deny, read
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(helpers.admin_json_object(request))
    finally:
        helpers.require_admin, helpers.read_json_object = original_gate, original_read
    assert exc.value.status_code == 403
    assert request.body_read is False
