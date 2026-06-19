"""Camera Offline Alert Settings APIRouter.

Extracted from ``app/main.py`` (Phase-7 of the hybrid-pattern router split).
Same template as ``app/api/settings_ai_router.py`` (Phase-2),
``app/api/events_router.py`` (Phase-5), ``app/api/alert_email_router.py``
(Phase-7 email sibling), and ``app/api/alert_push_router.py`` (Phase-7
push sibling): ``import app.main as main`` at module level, every
global / helper read through ``main.<name>`` *inside* handler bodies.

Handlers moved (2):

- GET   /api/settings/camera-offline
- PUT   /api/settings/camera-offline

Note: the PUT body shape here differs slightly from the email/push
siblings. There is **no** ``validate_camera_offline_alert_settings``
helper on ``app.main`` -- the original code coerces the payload inline
via ``max(1, int(...))`` with a try/except fallback to ``1``, and
records NO audit log entry. This is a pre-existing inconsistency
between handlers in main.py, preserved verbatim here per the
minimal-changes rule. Future cleanup: lift the inline coerce into a
shared ``main.validate_camera_offline_alert_settings(payload)`` helper
in a non-Phase-N commit; until then the router keeps the inline shape.

The splice was AST tree-filter + unparse (Phase-2 / Phase-3 / Phase-4 /
Phase-5 / Phase-6 safe pattern). See ``app/api/__init__.py`` for the
full hybrid-pattern rules.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.effective_camera_offline_alert_settings`` - facaded config read
  for the camera-offline alert channel (enabled flag + threshold).
- ``main.database.set_setting`` - persists the validated config row.
- ``main.utc_now`` - timestamp helper.
- ``main.require_admin`` - auth gate; rejects non-admin callers.
- ``HTTPException`` (from FastAPI) - 400 on non-dict payloads.

Tests go through ``LocalClient.request`` rather than calling
``main.<attr>`` directly, so no back-compat alias on ``app.main`` is
needed. The existing tests
``tests/test_api_router_split_invariants.py::test_all_main_attr_references_resolve_on_app_main``
+ ``routes-coverage`` invariant exercises every ``main.<attr>`` lookup
below on every test run.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

import app.main as main

router = APIRouter()


@router.get('/api/settings/camera-offline')
def get_camera_offline_alert_settings():
    return main.effective_camera_offline_alert_settings()


@router.put('/api/settings/camera-offline')
async def update_camera_offline_alert_settings(request: Request):
    main.require_admin(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Invalid settings payload')
    validated = {'enabled': bool(payload.get('enabled', False))}
    try:
        validated['offline_delay_minutes'] = max(1, int(payload.get('offline_delay_minutes', 1)))
    except (TypeError, ValueError):
        validated['offline_delay_minutes'] = 1
    result = main.database.set_setting('camera_offline_alert', validated, main.utc_now())
    return result
