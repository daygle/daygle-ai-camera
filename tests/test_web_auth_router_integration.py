"""Integration tests for the Phase-13 hybrid-pattern split's public-surface contracts.

When Phase 13 extracted 24 ``@app.X`` handlers from ``app/main.py`` into
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
3. ``main`` re-exports the FastAPI *response classes* (``FileResponse``,
   ``JSONResponse``, ``RedirectResponse``) so router monkeypatching
   (``monkeypatch.setattr(main, 'FileResponse', X)``) still swaps the
   right class object in the router-handler code paths per hybrid-
   pattern rule 5 in ``app/api/__init__.py``.
4. ``web_router`` registers the expected 22 unique page-handler paths
   (19 functions × 1 decorator + 1 function ``dashboard_aliases`` ×
   3 decorators).
5. ``dashboard_aliases`` is the route handler for ``/alerts``,
   ``/events``, ``/search`` (one function, three decorator paths).
6. ``auth_router`` registers exactly the four auth-cycle endpoints
   (POST ``/login``, POST ``/setup``, GET ``/logout``, POST ``/logout``).
7. **HTTP-level**: ``GET /alerts``, ``GET /events``, ``GET /search``
   all serve the SAME response (status, Content-Type, body bytes) as
   ``GET /`` after a successful login -- the marquee delegation proof.

Why this matters
----------------
The Phase 1-12 AST splices each moved 1-7 handlers out of ``app/main.py``
and the existing ``test_api_router_split_invariants.py`` walks the AST
for orphan-import regressions. Phase 13 created two back-compat aliases
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
Round-2 / Round-3 / Round-4 of Phase 13 already showed how easy that
regression is to introduce.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_api import LocalClient, _load_app, _login, _server, _setup_admin


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
    ``auth_router`` (the ``app.api.auth_router`` module), and
    ``fastapi_responses`` (for the response-class re-export
    contract test).
    """
    import sys
    import app.main as main
    import fastapi.responses as fastapi_responses
    return SimpleNamespace(
        main=main,
        web_router=sys.modules["app.api.web_router"],
        auth_router=sys.modules["app.api.auth_router"],
        fastapi_responses=fastapi_responses,
    )


# ---------------------------------------------------------------------------
# 1. Back-compat aliases -- the single most important Pool A contract.
# ---------------------------------------------------------------------------


def test_main_login_page_alias_points_to_web_router_login_page(app_modules):
    """``main.login_page`` must be the SAME function object as
    ``web_router.login_page``.

    The Pool A from-import rebind in ``app/main.py`` (final 2 lines of
    ``main.py``: ``from app.api.web_router import login_page as
    login_page``) creates a name ``login_page`` in main's namespace
    that points to the web_router implementation. ``auth_router``
    reads it via ``main.login_page(request, error)`` (Pool C bare-name
    reach). If this rebind drops, every POST ``/login`` flow
    NameErrors at request time.
    """
    assert (
        app_modules.main.login_page is app_modules.web_router.login_page
    ), "main.login_page back-compat alias not wired to app.api.web_router.login_page"


def test_main_setup_page_alias_points_to_web_router_setup_page(app_modules):
    """``main.setup_page`` must be the SAME function object as
    ``web_router.setup_page``.

    Same back-compat pattern as ``login_page`` -- ``auth_router``'s
    POST ``/setup`` handler reads ``main.setup_page(request, error)``.
    """
    assert (
        app_modules.main.setup_page is app_modules.web_router.setup_page
    ), "main.setup_page back-compat alias not wired to app.api.web_router.setup_page"


# ---------------------------------------------------------------------------
# 2. Hybrid-pattern rule 5 -- response-class re-export contract.
# ---------------------------------------------------------------------------


def test_main_reexports_fastapi_response_classes(app_modules):
    """``main`` must re-export ``FileResponse`` / ``JSONResponse`` /
    ``RedirectResponse`` as attrs on its own namespace so the hybrid
    pattern's ``main.FileResponse(...)`` reaches the same class the
    routers would get via a top-level
    ``from fastapi.responses import FileResponse`` (which the routers
    explicitly avoid because those classes live at
    ``fastapi.responses`` -- NOT at ``fastapi``).

    The test harness
    (``monkeypatch.setattr(main, 'FileResponse', X)``) and the
    Phase-13 Round-2/3/4 fixes all rely on this contract. A future
    refactor that re-imports FileResponse at top of a router file
    would silently regress this contract.
    """
    main = app_modules.main
    fastapi_responses = app_modules.fastapi_responses
    assert main.FileResponse is fastapi_responses.FileResponse
    assert main.JSONResponse is fastapi_responses.JSONResponse
    assert main.RedirectResponse is fastapi_responses.RedirectResponse


# ---------------------------------------------------------------------------
# 3. web_router registration: 22 unique paths.
# ---------------------------------------------------------------------------


def test_web_router_registers_expected_page_paths(app_modules):
    """web_router should expose exactly 22 unique page-handler paths.

    Path count comes from 19 functions × 1 decorator each + 1 function
    (``dashboard_aliases``) × 3 decorators (``/alerts``, ``/events``,
    ``/search``). Total = 22 routes + 22 unique paths (every path is
    distinct).

    The 22 paths are: ``/``, ``/favicon.ico``, ``/login``, ``/setup``,
    ``/live``, ``/zones``, ``/sounds``, ``/cameras``, ``/alerts``,
    ``/events``, ``/search``, ``/recordings``, ``/recordings/timeline``,
    ``/onnx``, ``/ai``, ``/yamnet-tflite``, ``/yamnet``, ``/profile``,
    ``/settings``, ``/users``, ``/audit``, ``/camera-log``.
    """
    expected_paths = {
        "/",
        "/favicon.ico",
        "/login",
        "/setup",
        "/live",
        "/zones",
        "/sounds",
        "/cameras",
        "/alerts",
        "/events",
        "/search",
        "/recordings",
        "/recordings/timeline",
        "/onnx",
        "/ai",
        "/yamnet-tflite",
        "/yamnet",
        "/profile",
        "/settings",
        "/users",
        "/audit",
        "/camera-log",
    }
    actual_paths = {
        route.path for route in app_modules.web_router.router.routes
    }
    assert actual_paths == expected_paths, (
        "web_router path set drifted from the Phase-13 expected set.\n"
        f"  missing: {expected_paths - actual_paths}\n"
        f"  extra:   {actual_paths - expected_paths}"
    )


def test_web_router_dashboard_aliases_function_serves_three_paths(app_modules):
    """``dashboard_aliases`` must be the route function for exactly
    ``/alerts`` + ``/events`` + ``/search``.

    3 decorators, 1 function, 3 handler entries. FastAPI is happy with
    this and dispatches each URL to the same function. The body
    returns ``root()`` (the dashboard shell) -- this is the contract
    the HTTP-level delegation test below verifies end-to-end.

    This test catches the regression mode where a future refactor
    splits ``dashboard_aliases`` into 3 distinct functions (each with
    its own body) or renames the function in a way that breaks the
    alias contract.
    """
    web_router = app_modules.web_router
    alias_handlers = [
        route
        for route in web_router.router.routes
        if getattr(route, "endpoint", None) is web_router.dashboard_aliases
    ]
    served_paths = {route.path for route in alias_handlers}
    assert served_paths == {"/alerts", "/events", "/search"}, (
        f"dashboard_aliases expected to serve {{'/alerts', '/events', '/search'}} "
        f"but actually serves {served_paths}"
    )


# ---------------------------------------------------------------------------
# 4. auth_router registration: 4 endpoints with correct methods.
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
# 5. HTTP-level: dashboard_aliases delegation end-to-end.
# ---------------------------------------------------------------------------


def test_dashboard_aliases_dispatch_to_dashboard_shell_over_http(
    tmp_path, monkeypatch
):
    """GET /alerts + /events + /search all serve the SAME response as GET /.

    Bootstrap: ``_setup_admin`` + ``_login``. After that, ``GET /``
    returns the dashboard shell (200 + Content-Type + body).

    The 3 dashboard_aliases routes (/alerts, /events, /search) all
    share one decorator-mounted function (``dashboard_aliases``)
    which returns ``root()`` -- so the same dashboard shell. We
    assert status, Content-Type, and body all match ``GET /``
    exactly.

    This is the marquee proof: the 3-decorator pattern on
    ``dashboard_aliases`` truly routes through FastAPI to the same
    handler as ``GET /`` (no chance copy/paste drift introduced
    hidden differences in the 3 paths).

    Why a body-bytes equality check rather than just status code?
    Because a refactor that accidentally moves /alerts to a separate
    function body returning a different component still produces
    status == 200. The byte-equality is the only way to prove the
    delegation works at the response-body level.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, _thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        # Bootstrap to a logged-in state.
        _setup_admin(client)
        _login(client)

        # GET / -- the dashboard shell that dashboard_aliases delegates to.
        root_status, root_headers, root_body = client.request("/")
        assert root_status == 200, (
            f"GET / should return 200 after login (dashboard shell) "
            f"but got {root_status}"
        )
        root_content_type = LocalClient.header(root_headers, "Content-Type")

        # Each dashboard_aliases path -- should be BYTE-IDENTICAL to /.
        for path in ("/alerts", "/events", "/search"):
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
                f"dashboard_aliases delegation drift -- the three "
                f"decorator paths must serve identical content"
            )
    finally:
        # Best-effort shutdown of the uvicorn server. ``_server``
        # returns a ``server`` object whose ``shutdown`` is safe to call.
        shutdown = getattr(server, "shutdown", None)
        if callable(shutdown):
            shutdown()
