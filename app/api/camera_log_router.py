"""Camera-log APIRouter.

Extracted from ``app/main.py`` lines 3327-3338 (Phase 12 of the
hybrid-pattern router split). Same template as ``app/api/live_router.py``
(Phase 10) and ``app/api/admin_router.py`` (Phase 11): ``import app.main
as main`` at module level, every global / helper read through
``main.<name>`` *inside* handler bodies.

Handlers moved (2):

- GET    /api/camera-log     -- ``list_camera_log``
- DELETE /api/camera-log     -- ``clear_camera_log``

These two handlers were non-contiguous in the original ``main.py`` with
a 7-line gap (the gap contained the unrelated ``camera_log_page``
web-page handler for ``GET /camera-log``). The hybrid-pattern AST
splice drops the two API handlers by name and leaves ``camera_log_page``
untouched on main -- ``/camera-log`` is a web-page route, not an API
route, so it stays in main.

BODY-REWRITE NOTE
Handlers in the original ``main.py`` referenced module-level state in
main.py via bare names (``require_admin``, ``database``, ``write_audit_log``).
After extraction to this router, those bare names resolve to ZERO
attributes in our namespace -- handlers would NameError at request time.
Per hybrid-pattern uniformity (rule 5 of ``app/api/__init__.py``), each
bare call is rewritten as ``main.<bare>`` in the router body. Pure
syntactic change with zero behavioral impact.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.require_admin`` -- admin gate shared with the audit + runtime-data
  DELETE handlers.
- ``main.database`` -- EventDatabase instance with ``.list_camera_diagnostics()``,
  ``.count_camera_diagnostics()``, ``.delete_all_camera_diagnostics()``.
- ``main.write_audit_log`` -- audit-log emitter used by the DELETE handler.

FastAPI builtin ``Query`` stays at top-level import (pagination +
filter validation).

Tests go through ``LocalClient.request`` rather than calling
``main.list_camera_log`` / ``main.clear_camera_log`` directly, so no
back-compat alias on ``app.main`` is needed. The Phase 7.1 invariant
``tests/test_api_router_split_invariants.py::test_app_api_imports_in_main_are_consumed``
will catch any orphan-import regression if a future refactor drops the
``from app.api.camera_log_router import router as camera_log_router``
rebind line in ``app/main.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

import app.main as main

router = APIRouter()


@router.get('/api/camera-log')
def list_camera_log(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    camera_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
):
    main.require_admin(request)
    entries = main.database.list_camera_diagnostics(
        limit=limit,
        offset=offset,
        camera_id=camera_id or None,
        event_type=event_type or None,
        severity=severity or None,
    )
    total = main.database.count_camera_diagnostics(
        camera_id=camera_id or None,
        event_type=event_type or None,
        severity=severity or None,
    )
    return {'entries': entries, 'total': total, 'limit': limit, 'offset': offset}


@router.delete('/api/camera-log')
def clear_camera_log(request: Request):
    main.require_admin(request)
    deleted = main.database.delete_all_camera_diagnostics()
    main.write_audit_log(request, 'delete_all', 'camera_log', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}
