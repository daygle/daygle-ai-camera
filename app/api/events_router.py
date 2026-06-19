"""Events APIRouter.

Extracted from ``app/main.py`` (Phase-5 of the hybrid-pattern router split).
Same template as ``app/api/recordings_router.py`` (Phase-3) and
``app/api/cameras_router.py`` (Phase-4): ``import app.main as main`` at
module level, every global / helper read through ``main.<name>`` *inside*
handler bodies.

Handlers moved (6):

- GET    /api/events
- GET    /api/events/{event_id}
- DELETE /api/events/{event_id}
- DELETE /api/events
- POST   /api/events/dismiss-all
- POST   /api/events/{event_id}/dismiss

The splice was AST tree-filter + unparse (the safe pattern Phase-2 / Phase
3 / Phase-4 used). See ``app/api/__init__.py`` for the full hybrid-pattern
rules.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.database.search_events`` / ``.get_event`` / ``.delete_event`` /
  ``.delete_all_events`` / ``.dismiss_event`` / ``.dismiss_all_events`` —
  per-handler event storage operations.
- ``main.write_audit_log`` — used by the two admin-only mutations
  (``delete_event``, ``delete_all_events``).
- ``main.require_admin`` — auth gate reused by every other mutating
  router.

Tests go through ``LocalClient.request`` rather than calling
``main.<attr>`` directly, so no back-compat alias on ``app.main`` is
needed for these endpoints. The existing
``tests/test_api_router_split_invariants.py::test_all_main_attr_references_resolve_on_app_main``
+ ``routes-coverage`` invariant exercises every ``main.<attr>`` lookup
below on every test run.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

import app.main as main

router = APIRouter()


@router.get('/api/events')
def events(
    label: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    alerted_only: bool = False,
    with_recording: bool = False,
):
    return main.database.search_events(label=label, limit=limit, alerted_only=alerted_only, with_recording=with_recording)


@router.get('/api/events/{event_id}')
def event_detail(event_id: int):
    event = main.database.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    return event


@router.delete('/api/events/{event_id}')
def delete_event(event_id: int, request: Request):
    main.require_admin(request)
    event = main.database.delete_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    snapshot_path = event.get('snapshot_path')
    if snapshot_path:
        snapshot = Path(snapshot_path)
        if snapshot.exists() and snapshot.is_file():
            snapshot.unlink(missing_ok=True)
    main.write_audit_log(request, 'delete', 'event', event_id)
    return {'ok': True}


@router.delete('/api/events')
def delete_all_events(request: Request):
    main.require_admin(request)
    deleted = main.database.delete_all_events()
    main.write_audit_log(request, 'delete_all', 'events', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}


@router.post('/api/events/dismiss-all')
def dismiss_all_events_route():
    dismissed = main.database.dismiss_all_events()
    return {'ok': True, 'dismissed': dismissed}


@router.post('/api/events/{event_id}/dismiss')
def dismiss_event_route(event_id: int):
    ok = main.database.dismiss_event(event_id)
    if not ok:
        raise HTTPException(status_code=404, detail='Event not found')
    return {'ok': True}
