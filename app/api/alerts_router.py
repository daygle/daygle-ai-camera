"""Alerts APIRouter.

Extracted from ``app/main.py`` (Phase-5 of the hybrid-pattern router split).
Same template as ``app/api/recordings_router.py`` (Phase-3),
``app/api/cameras_router.py`` (Phase-4), and ``app/api/events_router.py``
(Phase-5): ``import app.main as main`` at module level, every global /
helper read through ``main.<name>`` *inside* handler bodies.

Handlers moved (4):

- GET    /api/alerts
- DELETE /api/alerts
- POST   /api/alerts/dismiss-all
- POST   /api/alerts/{group_key}/dismiss

The splice was AST tree-filter + unparse (the safe pattern Phase-2 / Phase
3 / Phase-4 used). See ``app/api/__init__.py`` for the full hybrid-pattern
rules.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.database.alerts`` / ``.delete_all_alerts`` /
  ``.dismiss_all_alerts`` / ``.dismiss_alert_group`` — per-handler alert
  storage operations. ``dismiss_alert`` (singular) lives on the
  ``events`` side (``database.dismiss_event``) so users can dismiss
  one event at a time; the ``dismiss_alert_group`` here is the bulk
  equivalent for an alert group identifier.
- ``main.write_audit_log`` — admin-only mutation ledger. Used only by
  ``delete_all_alert_history`` (the bulk purge); per-group dismisses
  are not audited because they happen on every user interaction.
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

from fastapi import APIRouter, Query, Request

import app.main as main

router = APIRouter()


@router.get('/api/alerts')
def alert_history(limit: int = Query(25, ge=1, le=200)):
    return main.database.alerts(limit=limit)


@router.delete('/api/alerts')
def delete_all_alert_history(request: Request):
    main.require_admin(request)
    deleted = main.database.delete_all_alerts()
    main.write_audit_log(request, 'delete_all', 'alert_history', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}


@router.post('/api/alerts/dismiss-all')
def dismiss_all_alerts_route():
    dismissed = main.database.dismiss_all_alerts()
    return {'ok': True, 'dismissed': dismissed}


@router.post('/api/alerts/{group_key}/dismiss')
def dismiss_alert_group_route(group_key: str):
    dismissed = main.database.dismiss_alert_group(group_key)
    return {'ok': True, 'dismissed': dismissed}
