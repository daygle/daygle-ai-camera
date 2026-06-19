"""Email Alert Settings APIRouter.

Extracted from ``app/main.py`` (Phase 7 of the hybrid-pattern router split).
Same template as ``app/api/settings_ai_router.py`` (Phase 2) and
``app/api/events_router.py`` (Phase 5): ``import app.main as main`` at
module level, every global / helper read through ``main.<name>`` *inside*
handler bodies.

Handlers moved (2):

- GET   /api/settings/alert-email
- PUT   /api/settings/alert-email

The splice was AST tree-filter + unparse (Phase 2 / Phase 3 / Phase 4 /
Phase 5 / Phase 6 safe pattern). See ``app/api/__init__.py`` for the
full hybrid-pattern rules.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.effective_email_alert_settings`` - facaded config read for the
  alert-email channel. Resolves the on-disk ``alert_email`` setting,
  applies any defaults, and returns the dict the front-end renders.
- ``main.validate_alert_email_settings`` - validated-config coercion
  (rejects extra keys, clamps SMTP port ranges, etc.). Returns a clean
  dict ready to ``database.set_setting``.
- ``main.database.set_setting(key, value, ts)`` - persists the validated
  config row alongside the UTC timestamp.
- ``main.utc_now`` - timestamp helper.
- ``main.require_admin`` - auth gate; rejects non-admin callers.
- ``main.write_audit_log`` - admin-only mutation ledger.

Tests go through ``LocalClient.request`` rather than calling
``main.<attr>`` directly, so no back-compat alias on ``app.main`` is
needed. The existing tests
``tests/test_api_router_split_invariants.py::test_all_main_attr_references_resolve_on_app_main``
+ ``routes-coverage`` invariant exercises every ``main.<attr>`` lookup
below on every test run.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

import app.main as main

router = APIRouter()


@router.get('/api/settings/alert-email')
def get_alert_email_settings():
    return main.effective_email_alert_settings()


@router.put('/api/settings/alert-email')
async def update_alert_email_settings(request: Request):
    main.require_admin(request)
    payload = await request.json()
    settings = main.validate_alert_email_settings(payload)
    result = main.database.set_setting('alert_email', settings, main.utc_now())
    main.write_audit_log(request, 'update', 'settings.alert_email')
    return result
