"""FastAPI APIRouter package.

Hybrid pattern: routers do ``import app.main as main`` at module level and read
every global/lock/helper via ``main.X`` *inside* handler bodies. This preserves
test back-compat (tests do ``monkeypatch.setattr(main, 'database', fake)`` and
call ``main.<helper_name>(...)`` directly) without rewriting tests.

Rules when extracting an endpoint into a router:

1. HTTP route, decorators, body parsing, response shaping -> move to the router.
2. Module-level globals (``database``, ``detector``, ``_sound_statuses``,
   ``_live_detection_status``, etc.) stay on ``app.main``. Routers read
   them through ``main.<name>`` only inside handler bodies.
3. Helpers / pure functions that tests reference as ``main.<name>``
   (e.g. ``main._sound_status_reason``) stay defined on ``app.main`` even if
   the router is their only caller - *do not* move them into the router file.
4. ``app.include_router(...)`` for the new router is appended to the bottom of
   ``app/main.py`` (after every global is defined) so the circular ``import
   app.main as main`` inside the router file resolves against the fully-loaded
   module when the first request comes in.
5. **Cross-module re-exports that handlers reach via ``main.<name>``
   (exception classes, import-as aliases, from-import callables,
   module-level instance assignments like ``main.database``) are part of
   the router contract.** ``app/main.py``'s from-import / import-as /
   top-level assignment inventory is the authoritative list
   (``main.AuthError``, ``main.auth``, ``main.database``,
   ``main.require_admin``, ``main.write_audit_log``, ``main.utc_now``,
   ...); a new ``main.<attr>`` in any router means adding the
   corresponding import or assignment to ``app/main.py`` first. The
   existing
   ``tests/test_api_router_split_invariants.py`` assertions walk
   ``main.<attr>`` reachability only against ``tests/test_api.py``,
   not against ``app/api/*.py``, so a missing rebind will not fail at
   module-import time and will silently NameError only when an
   exercising handler runs.

To extract the next router, use ``app/api/sound_router.py`` as the template.
"""
