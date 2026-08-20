"""Integration tests for the Phase-13 hybrid-pattern split's public-surface contracts.

When Phase-13 extracted 24 ``@app.X`` handlers from ``app/main.py`` into
``app/api/web_router.py`` + ``app/api/auth_router.py``, two back-compat
aliases were appended at the bottom of ``app/main.py``::

    from app.api.web_router import login_page as login_page
    from app.api.web_router import setup_page as setup_page

These are the **Pool A from-import rebinds** documented in
``app/api/__init__.py`` hybrid-pattern rule 1. They let the routers
call ``main.login_page(request, error)`` and ``main.setup_page(request,
error)`` (Pool C bare-name reach) without coupling to the underlying
web_router module path. If a future refactor drops either rebind,
``main.login_page`` becomes undefined and ``auth_router`` raises
``NameError`` at request time.

These tests defend that public-surface contract:

1. ``main.login_page`` is the SAME function object as
   ``app.api.web_router.login_page`` -- the back-compat alias is wired.
2. ``main.setup_page`` is the SAME function object as
   ``app.api.web_router.setup_page`` -- the back-compat alias is wired.
3. ``web_router`` registers the expected 24 unique page-handler paths
   (23 functions × 1 decorator + 1 function ``dashboard_aliases`` ×
   1 decorator).
4. ``dashboard_aliases`` is the route handler for ``/alerts``,
   ``/events``, ``/search`` (one function, three decorator paths).
5. ``auth_router`` registers exactly the four auth-cycle endpoints
   (POST ``/login``, POST ``/setup``, GET ``/logout``, POST ``/logout``).
6. **HTTP-level**: ``GET /alerts``, ``GET /events``, ``GET /search``
   all serve the SAME response (status, Content-Type, body bytes) as
   ``GET /`` after a successful login -- the marquee delegation proof.

Why this matters
----------------
The Phase-1-12 AST splices each moved 1-7 handlers out of ``app/main.py``
and the existing ``test_api_router_split_invariants.py`` walks the AST
for orphan-import regressions. Phase-13 created two back-compat aliases
that are NOT rebind-walkable by Pool A (they look bare from main.py's
perspective but resolve to web_router functions via the module's import
system). A future refactor could trivially drop these aliases and the
existing 5 invariants would NOT catch it -- the routers would NameError
at request time, not import time. These integration tests catch that
regression mode explicitly.

Likewise, the response-class monkeypatch contract is documented in
``web_router.py``'s STD-LIB NOTE docstring but never enforced by a test.
A future maintainer who re-transitions from hybrid pattern back to
top-level imports would silently break the monkeypatch contract;
Round-2 / Round-3 / Round-4 of Phase-13 already showed how easy that
regression is to introduce.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin


@pytest.fixture
def app_modules():
    """Lazy-order import: load ``app.main`` FIRST, then read routers
    from already-populated ``sys.modules``.

    The Phase-13 hybrid-pattern is an intentional circular import::

        app/api/<name>_router.py    -->  ``import app.main as main``
                                          (at module top)
        app/main.py                  -->  ``from app.api.<name>_router import
                                            router as <name>_router``
                                          (at module BOTTOM; the
                                          ``include_router`` rebinds)

    Python can resolve that circular edge only if ``app.main`` is
    loaded FIRST (its body triggers each router's fresh load as a
    side-effect; the router's top-level
    ``import app.main as main`` reads the partial ``app.main`` from
    ``sys.modules``, runs its handler definitions, and finishes; then
    ``app.main``'s ``from app.api.<X> import router as <X>_router``
    rebind succeeds because ``<X>.router`` now exists).

    If the order is reversed -- i.e. ``import app.api.auth_router``
    runs FIRST -- then:

      1. Python starts loading ``app.api.auth_router``
      2. ``auth_router`` at top: ``import app.main as main``
      3. ``app.main`` is NOT in ``sys.modules`` yet, so Python
         starts a fresh load of ``app.main``
      4. ``app.main`` body executes top-to-bottom, reaches its
         bottom-of-file rebinds
      5. ``app.main`` tries ``from app.api.auth_router import router
         as auth_router`` -- ``auth_router`` IS in ``sys.modules``
         (partially loaded from step 1) -- but ``router`` hasn't
         been defined yet because the auth_router file is still
         mid-import (the ``router = APIRouter()`` line comes AFTER
         the ``import app.main as main`` top-level)
      6. ``ImportError: cannot import name 'router' from partially
         initialized module 'app.api.auth_router'``

    The fix: import ``app.main`` FIRST in this fixture so the
    chain runs in the safe order. Once ``app.main`` is fully
    loaded, ``sys.modules`` already contains every
    ``app.api.<X>`` module -- we read them directly without
    triggering any further import side-effects.

    Returns a ``SimpleNamespace`` with ``main`` (the ``app.main``
    module), ``web_router`` (the ``app.api.web_router`` module),
    ``auth_router`` (the ``app.api.auth_router`` module).
    """
    import sys
    import app.main as main
    return SimpleNamespace(
        main=main,
        web_router=sys.modules["app.api.web_router"],
        auth_router=sys.modules["app.api.auth_router"],
    )


# ---------------------------------------------------------------------------
# 1. web_router registration: 23 unique paths.
# ---------------------------------------------------------------------------


def test_web_router_registers_expected_page_paths(app_modules):
    """web_router should expose exactly 25 unique page-handler paths.

    Path count comes from 24 functions × 1 decorator each + 1 function
    (``dashboard_aliases``) × 1 decorator (``/search``).
    Total = 23 routes + 23 unique paths (every path is distinct). ``/events``
    is served by its own ``events_page`` function (the single activity feed).
    ``/snapshots`` is the captured-frame library under the Clips menu.
    There is no separate ``/alerts`` page: an alert is a property of an event,
    surfaced as an indicator on the Events row.

    The 24 paths are: ``/``, ``/favicon.ico``, ``/login``, ``/setup``,
    ``/live``, ``/zones``, ``/sounds``, ``/objects``, ``/cameras``,
    ``/events``, ``/search``, ``/recordings``, ``/snapshots``,
    ``/recordings/timeline``,
    ``/onnx``, ``/ai``, ``/arcface``, ``/yamnet-tflite``, ``/yamnet``, ``/profile``,
    ``/settings``, ``/users``, ``/audit``, ``/camera-log``,
    ``/application-log``.
    """
    expected_paths = {
        "/",
        "/favicon.ico",
        "/login",
        "/setup",
        "/live",
        "/zones",
        "/sounds",
        "/objects",
        "/cameras",
        "/events",
        "/search",
        "/recordings",
        "/snapshots",
        "/recordings/timeline",
        "/recordings/{recording_id}",
        "/onnx",
        "/ai",
        "/arcface",
        "/face-recognition",
        "/people",
        "/yamnet-tflite",
        "/yamnet",
        "/profile",
        "/settings",
        "/users",
        "/audit",
        "/camera-log",
        "/application-log",
    }
    actual_paths = {
        route.path for route in app_modules.web_router.router.routes
    }
    assert actual_paths == expected_paths, (
        "web_router path set drifted from the Phase-13 expected set.\n"
        f"  missing: {expected_paths - actual_paths}\n"
        f"  extra:   {actual_paths - expected_paths}"
    )


def test_web_router_dashboard_aliases_function_serves_search(app_modules):
    """``dashboard_aliases`` must be the route function for exactly
    ``/search``.

    ``/events`` is now a dedicated page served by its own ``events_page``
    function (the single activity feed; there is no separate alerts page), so
    ``dashboard_aliases`` is left aliasing only ``/search`` onto the dashboard
    shell. This test catches the regression mode where a future refactor
    re-folds ``/events`` back into the alias, or renames the function.
    """
    web_router = app_modules.web_router
    alias_handlers = [
        route
        for route in web_router.router.routes
        if getattr(route, "endpoint", None) is web_router.dashboard_aliases
    ]
    served_paths = {route.path for route in alias_handlers}
    assert served_paths == {"/search"}, (
        f"dashboard_aliases expected to serve {{'/search'}} "
        f"but actually serves {served_paths}"
    )
    # /events must be its own function, not an alias of the dashboard shell.
    events_paths = {
        route.path
        for route in web_router.router.routes
        if getattr(route, "endpoint", None) is web_router.events_page
    }
    assert events_paths == {"/events"}, (
        f"events_page expected to serve {{'/events'}} but serves {events_paths}"
    )


# ---------------------------------------------------------------------------
# 3. auth_router registration: 4 endpoints with correct methods.
# ---------------------------------------------------------------------------


def test_auth_router_registers_expected_auth_endpoints(app_modules):
    """``auth_router`` should expose exactly 4 auth-cycle endpoints:
    POST ``/login``, POST ``/setup``, GET ``/logout``, POST ``/logout``.

    The ``methods`` set is what FastAPI populates from the ``@router``
    decorator on each handler. The Python-level path introspection
    guards against:
      - dropping a route (path missing from ``methods_by_path``);
      - changing a route's HTTP method (e.g. POST ``/logout`` becoming
        GET ``/logout`` would silently break the logout form
        submission flow);
      - accidentally adding duplicate route paths (path methods would
        be a multi-valued set -- still detectable here).
    """
    auth_router = app_modules.auth_router
    methods_by_path: dict[str, set[str]] = {}
    for route in auth_router.router.routes:
        if hasattr(route, "path") and hasattr(route, "methods"):
            methods_by_path.setdefault(route.path, set()).update(
                route.methods
            )
    assert methods_by_path, "auth_router registered zero routes"
    assert "POST" in methods_by_path.get("/login", set()), (
        f"auth_router missing POST /login endpoint; "
        f"got methods_by_path={methods_by_path}"
    )
    assert "POST" in methods_by_path.get("/setup", set()), (
        "auth_router missing POST /setup endpoint"
    )
    assert "GET" in methods_by_path.get("/logout", set()), (
        "auth_router missing GET /logout endpoint"
    )
    assert "POST" in methods_by_path.get("/logout", set()), (
        "auth_router missing POST /logout endpoint"
    )


# ---------------------------------------------------------------------------
# 4. HTTP-level: dashboard_aliases delegation end-to-end.
# ---------------------------------------------------------------------------


def test_dashboard_aliases_dispatch_to_dashboard_shell_over_http(
    tmp_path, monkeypatch
):
    """GET /search serves the SAME response as GET /.

    Bootstrap: ``_setup_admin`` + ``_login``. After that, ``GET /``
    returns the dashboard shell (200 + Content-Type + body).

    ``/search`` is mounted on ``dashboard_aliases`` which returns
    ``root()`` -- so the same dashboard shell. We assert status,
    Content-Type, and body all match ``GET /`` exactly. ``/events`` is
    served by its own ``events_page`` function (the dedicated Events
    page), so it is checked separately for a distinct 200 response
    rather than byte-equality with the shell.

    This is the marquee proof: the 2-decorator pattern on
    ``dashboard_aliases`` truly routes through FastAPI to the same
    handler as ``GET /`` (no chance copy/paste drift introduced
    hidden differences in the paths).

    Why a body-bytes equality check rather than just status code?
    Because a refactor that accidentally moves an alias to a separate
    function body returning a different component still produces
    status == 200. The byte-equality is the only way to prove the
    delegation works at the response-body level.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    _server_obj, _thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)

        # GET / -- the dashboard shell that dashboard_aliases delegates to.
        root_status, root_headers, root_body = client.request("/")
        assert root_status == 200, (
            f"GET / should return 200 after login (dashboard shell) "
            f"but got {root_status}"
        )
        root_content_type = LocalClient.header(root_headers, "Content-Type")

        # /search -- should be BYTE-IDENTICAL to / (still a dashboard alias).
        for path in ("/search",):
            status, headers, body = client.request(path)
            assert status == 200, (
                f"GET {path} should return 200 after login but got {status}"
            )
            content_type = LocalClient.header(headers, "Content-Type")
            assert content_type == root_content_type, (
                f"GET {path} Content-Type ({content_type!r}) differs "
                f"from GET / ({root_content_type!r}); "
                f"dashboard_aliases delegation drift"
            )
            assert body == root_body, (
                f"GET {path} body differs from GET /; "
                f"dashboard_aliases delegation drift -- the alias "
                f"decorator paths must serve identical content"
            )

        # /events is served by its own dedicated events_page function (the
        # single activity feed), so it should be a distinct 200 response, NOT
        # the dashboard shell.
        events_status, _events_headers, _events_body = client.request("/events")
        assert events_status == 200, (
            f"GET /events should return 200 after login but got {events_status}"
        )
    finally:
        _server_obj.should_exit = True
        _thread.join(timeout=5)
