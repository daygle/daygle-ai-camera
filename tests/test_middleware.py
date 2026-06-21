"""Phase-15 integration tests for ``app/middleware.py``.

Phase-15 extracted the last 2 ``@app.middleware('http')`` decorators out of
``app/main.py`` and into ``app/middleware.py`` as plain async callables. The
extraction can't move the decorator itself -- ``@app.middleware('http')``
signs the callable onto the FastAPI app's middleware stack at app-instance
time, and ``app.main`` is what ``app/middleware`` imports to break the
circular load-order -- so ``app/main.py`` still owns the 2 registration
lines (``app.middleware('http')(authentication_middleware)`` etc.) at the
bottom of the file.

What the middleware actually does:

``authentication_middleware`` (request-path branch order in source):

1. ``auth.enabled=False`` -> passthrough (no redirects, no CSRF, no setup).
2. Path in ``PUBLIC_PATHS`` (``/favicon.ico``, ``/login``, ``/setup``) or
   starts with ``PUBLIC_PREFIXES`` (``/static/``) -> passthrough.
3. ``main.auth.users_exist()`` is False -> ``/api/*`` returns 403 with
   ``{"detail": "Initial administrator setup is required."}``; non-api
   returns 303 redirect to ``/setup``.
4. ``main.auth.get_session(session_cookie)`` is None -> ``/api/*`` returns
   401 with ``{"detail": "Authentication required"}``; non-api returns 303
   redirect to ``/login``.
5. Admin-required path with non-admin session -> 403 ``{"detail": "Admin
   access required"}`` (for both HTML and ``/api/*``).
6. ``/api/*`` mutating method WITHOUT a matching X-CSRF-Token header -> 403
   ``{"detail": "CSRF token missing or invalid"}``. ``POST /logout`` also
   needs CSRF (special-cased path).

``app_navigation_middleware`` (called after each request):

1. Path in ``PUBLIC_PATHS`` -> pass-through (skip script injection).
2. Response content-type not ``text/html`` -> pass-through.
3. Otherwise: lazy-iterate the response body, prepend
   ``<script src="/static/nav.js"></script>`` immediately before the closing
   ``</body>`` marker (idempotent -- skip if already injected).

These tests cover the full critical-path lifecycle end-to-end via the
FastAPI ``app`` instance running on a uvicorn thread, just like the
existing tests in ``tests/test_api.py`` and ``tests/test_web_auth_router_integration.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_api import (  # noqa: E402
    LocalClient,
    _load_app,
    _login,
    _server,
    _setup_admin,
    _post_frame_detection,
)


# The exact tag app_navigation_middleware injects before </body>.
NAV_SCRIPT_TAG = '<script src="/static/nav.js"></script>'
# Shared regex for matching the closing </body> marker.
_BODY_CLOSE_RE = "</body>"


# ---------------------------------------------------------------------------
# 1. PUBLIC_PATHS / PUBLIC_PREFIXES bypass -- the no-auth-required routes.
# ---------------------------------------------------------------------------


def test_public_paths_serve_without_session_or_admin(tmp_path, monkeypatch):
    """``PUBLIC_PATHS`` (``/login``, ``/setup``, ``/favicon.ico``) must serve
    200 with no admin user existing yet and no session cookie.

    This proves the middleware's branch-1 passthrough is reachable. Without
    the passthrough, an unauthenticated browser would deadlock before ever
    reaching the setup wizard.

    Body-text invariants are asserted by ``_setup_admin`` in tests/test_api.py;
    here we only assert that the routes serve 200 without redirecting, which
    is the property the middleware contract is responsible for.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        # /login -- login form page (HTML, content-type text/html)
        status, headers, login_body = client.request("/login")
        assert status == 200, f"/login (no admin, no session) expected 200, got {status}"
        assert isinstance(login_body, str), (
            "/login should render an HTML page (string body), not JSON"
        )
        assert "text/html" in (LocalClient.header(headers, "Content-Type") or ""), (
            "/login should be served as text/html"
        )

        # /setup -- create-administrator form page (HTML)
        status, headers, setup_body = client.request("/setup")
        assert status == 200, f"/setup (no admin, no session) expected 200, got {status}"
        assert isinstance(setup_body, str), (
            "/setup should render an HTML page (string body), not JSON"
        )
        assert "text/html" in (LocalClient.header(headers, "Content-Type") or ""), (
            "/setup should be served as text/html"
        )

        # /favicon.ico -- served as SVG (PUBLIC_PATHS bypass + content-type guard
        # against the navigation middleware accidentally injecting nav.js into
        # an SVG response).
        status, headers, favicon_body = client.request("/favicon.ico")
        assert status == 200, f"/favicon.ico expected 200, got {status}"
        assert "image/svg+xml" in (LocalClient.header(headers, "Content-Type") or ""), (
            "/favicon.ico should be served as image/svg+xml"
        )
        assert NAV_SCRIPT_TAG not in favicon_body, (
            "favicon body must NOT have nav.js script injected "
            "(content-type guard in app_navigation_middleware)"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_static_files_bypass_auth(tmp_path, monkeypatch):
    """``PUBLIC_PREFIXES`` (``/static/``) must serve without a session.

    Without this, the login form's referenced CSS/JS would 401 and the login
    page would be unrenderable.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        # No _setup_admin, no _login -- still needs to serve.
        status, _headers, _body = LocalClient(base_url).request("/static/app.js")
        assert status == 200, f"/static/app.js (no admin, no session) expected 200, got {status}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 2. No users exist yet (admin setup not done) -- redirect to /setup.
# ---------------------------------------------------------------------------


def test_no_users_root_redirects_to_setup(tmp_path, monkeypatch):
    """``users_exist()`` False + non-/api path -> 303 -> /setup."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        client = LocalClient(base_url)
        status, headers, _body = client.request("/", follow_redirects=False)
        assert status == 303, f"GET / (no users) expected 303, got {status}"
        assert LocalClient.header(headers, "Location") == "/setup", (
            f"GET / (no users) should redirect to /setup; "
            f"got Location={LocalClient.header(headers, 'Location')!r}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_no_users_api_returns_setup_required_403(tmp_path, monkeypatch):
    """``users_exist()`` False + /api path -> 403 with the
    ``Initial administrator setup is required.`` detail.

    The contract is JSON, not HTML, so the bootstrap UI's fetch calls land on
    a parseable error body instead of an HTML page the JS can't consume.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        client = LocalClient(base_url)
        status, _headers, body = client.request("/api/status")
        assert status == 403, f"GET /api/status (no users) expected 403, got {status}"
        assert body == {"detail": "Initial administrator setup is required."}, (
            f"body mismatch: got {body!r}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 3. Admin user exists, no session -- redirect to /login / 401 /api.
# ---------------------------------------------------------------------------


def test_no_session_root_redirects_to_login(tmp_path, monkeypatch):
    """``get_session(cookie)`` is None + non-/api path -> 303 -> /login."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        # Bootstrap an admin so users_exist() is True but consume their
        # cookie jar -- this client is "logged out" now.
        _setup_admin(LocalClient(base_url))
        anonymous = LocalClient(base_url)
        status, headers, _body = anonymous.request("/", follow_redirects=False)
        assert status == 303, f"anonymous GET / expected 303, got {status}"
        assert LocalClient.header(headers, "Location") == "/login", (
            f"anonymous GET / should redirect to /login; "
            f"got Location={LocalClient.header(headers, 'Location')!r}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_no_session_api_returns_401(tmp_path, monkeypatch):
    """``get_session(cookie)`` is None + /api path -> 401 with
    ``Authentication required`` detail."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        _setup_admin(LocalClient(base_url))
        anonymous = LocalClient(base_url)
        status, _headers, body = anonymous.request("/api/status")
        assert status == 401, f"anonymous GET /api/status expected 401, got {status}"
        assert body == {"detail": "Authentication required"}, f"body mismatch: got {body!r}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 4. Mutating /api calls -- CSRF guard.
# ---------------------------------------------------------------------------


def test_mutating_api_without_csrf_returns_403(tmp_path, monkeypatch):
    """POST /api/* with a valid SESSION but NO X-CSRF-Token header -> 403
    ``CSRF token missing or invalid``.

    Regression guard for the CSRF check that protects state-changing endpoints
    from cross-site form-submission attacks.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        client = LocalClient(base_url)
        _setup_admin(client)
        _login(client)  # establishes session; csrf returned but intentionally unused
        status, _headers, body = _post_frame_detection(client)  # no csrf kwarg
        assert status == 403, f"POST /api/detect/frame (no csrf) expected 403, got {status}"
        assert body == {"detail": "CSRF token missing or invalid"}, (
            f"CSRF rejection should produce this body; got {body!r}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_mutating_api_with_wrong_csrf_returns_403(tmp_path, monkeypatch):
    """A valid session + a wrong X-CSRF-Token value -> 403.

    This guards against the case where a static token is accidentally reused
    or the session cookie is replayed with a stale/forged CSRF value.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        client = LocalClient(base_url)
        _setup_admin(client)
        _login(client)
        status, _headers, body = client.request(
            "/api/detect/frame",
            method="POST",
            data=b"x",
            headers={"Content-Type": "image/png", "X-CSRF-Token": "not-the-real-token"},
        )
        assert status == 403, f"POST /api/detect/frame (wrong csrf) expected 403, got {status}"
        assert body["detail"] == "CSRF token missing or invalid"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_mutating_api_with_correct_csrf_passes_middleware(tmp_path, monkeypatch):
    """A valid session + the right X-CSRF-Token -> 200 (middleware does NOT
    short-circuit, the endpoint runs and returns its normal response).

    This is the positive-case regression guard: any future middleware rewrite
    that mistakenly rejects ALL mutating requests would fail here even though
    tests #7 / #8 silently pass.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        client = LocalClient(base_url)
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, body = _post_frame_detection(client, csrf)
        assert status == 200, (
            f"POST /api/detect/frame (valid session + valid csrf) expected 200, "
            f"got {status}; middleware is over-blocking"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_logout_post_without_csrf_returns_403(tmp_path, monkeypatch):
    """POST /logout is explicitly CSRF-protected even though it isn't a
    ``/api/*`` path -- cross-check that branch-6 doesn't gate on the
    ``/api/`` prefix alone.

    The positive case (POST /logout WITH csrf -> 200 "ok") is already
    exercised end-to-end by ``test_logout_user_creation_and_password_reset``
    in tests/test_api.py -- the middleware logic is what we're covering here,
    so the negative case is sufficient.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        client = LocalClient(base_url)
        _setup_admin(client)
        _login(client)
        status, _headers, body = client.request("/logout", method="POST")
        assert status == 403, f"POST /logout (no csrf) expected 403, got {status}"
        assert body == {"detail": "CSRF token missing or invalid"}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 5. Admin gating -- non-admin session -> 403 on admin paths.
# ---------------------------------------------------------------------------


def test_non_admin_session_denied_admin_api_path(tmp_path, monkeypatch):
    """A real viewer session hitting an admin-only ``/api/*`` PUT -> 403
    ``Admin access required``.

    The CSRF check runs AFTER the admin check in the source, so an admin-path
    rejection happens before CSRF mismatch can mask it. Verify both here.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        admin = LocalClient(base_url)
        _setup_admin(admin)
        admin_csrf = _login(admin)
        # Create a viewer-role user.
        status, _h, viewer = admin.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert status == 200 and viewer["role"] == "viewer"

        # Log in as the viewer.
        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, "viewer", "Viewer123!")

        # PUT /api/settings/ai is an admin path -- viewer should be 403'd.
        status, _headers, body = viewer_client.request(
            "/api/settings/ai",
            method="PUT",
            json_body={"confidence": 0.2},
            headers={"X-CSRF-Token": viewer_csrf},
        )
        assert status == 403, f"viewer PUT /api/settings/ai expected 403, got {status}"
        assert isinstance(body, dict) and body.get("detail") == "Admin access required", (
            f"body mismatch: got {body!r}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_non_admin_session_denied_admin_html_path(tmp_path, monkeypatch):
    """HTML admin paths (e.g. ``/cameras``) also fall under branch-5 and
    return 403 ``Admin access required`` (JSON body, NOT an HTML redirect)."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        admin = LocalClient(base_url)
        _setup_admin(admin)
        admin_csrf = _login(admin)
        admin.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        viewer_client = LocalClient(base_url)
        _login(viewer_client, "viewer", "Viewer123!")
        status, _headers, body = viewer_client.request("/cameras", follow_redirects=False)
        assert status == 403, f"viewer GET /cameras expected 403, got {status}"
        assert isinstance(body, dict) and body["detail"] == "Admin access required", (
            f"GET /cameras by viewer should return JSON 403, got {body!r}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 6. app_navigation_middleware -- script injection into HTML responses.
# ---------------------------------------------------------------------------


def test_navigation_middleware_injects_script_into_html_root(tmp_path, monkeypatch):
    """After login, GET / returns the dashboard shell (HTML) with the
    ``<script src=\"/static/nav.js\">...</script>`` tag injected immediately
    before ``</body>``.

    Without this, the navigation sidebar/header doesn't render in the UI.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        client = LocalClient(base_url)
        _setup_admin(client)
        _login(client)
        status, headers, body = client.request("/")
        assert status == 200, f"GET / (after login) expected 200, got {status}"
        assert isinstance(body, str), f"GET / should return HTML string body, got {type(body).__name__}"
        assert "text/html" in (LocalClient.header(headers, "Content-Type") or ""), (
            "GET / should serve HTML so the navigation middleware exercises its injection branch"
        )
        assert NAV_SCRIPT_TAG in body, (
            f"app_navigation_middleware should prepend {NAV_SCRIPT_TAG!r}; "
            f"injected nowhere in the body"
        )
        # The injection contract: tag immediately before </body>.
        script_idx = body.index(NAV_SCRIPT_TAG)
        body_idx = body.index(_BODY_CLOSE_RE)
        assert script_idx < body_idx, (
            f"{NAV_SCRIPT_TAG!r} must precede </body> in the response body; "
            f"got script at {script_idx}, </body> at {body_idx}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_navigation_middleware_skips_json_responses(tmp_path, monkeypatch):
    """GET /api/status returns JSON -- nav.js is NOT injected.

    Without this guard, the JSON response body becomes invalid (HTML in
    a JSON response breaks frontend JSON.parse) -- which is exactly the
    regression that motivated the strict content-type branch.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        client = LocalClient(base_url)
        _setup_admin(client)
        _login(client)
        status, headers, body = client.request("/api/status")
        assert status == 200
        # LocalClient auto-decodes JSON -- assert decoded JSON, not a string.
        assert isinstance(body, dict), (
            f"/api/status should return JSON dict; nav.middleware should NOT "
            f"have tampered with it (got {type(body).__name__})"
        )
        # And the Content-Type must NOT be mutated to HTML.
        ctype = LocalClient.header(headers, "Content-Type") or ""
        assert not ctype.startswith("text/html"), (
            f"Content-Type should remain JSON-ish; got {ctype!r}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_navigation_middleware_skips_public_paths(tmp_path, monkeypatch):
    """The middleware explicit-paths branch (PUBLIC_PATHS) prevents the nav
    script from being added to ``/login`` etc. -- otherwise the login form
    would import the nav-sidebar code at login time, wasting a request and
    potentially conflicting with cookie state on the login form.

    We bootstrap ``_setup_admin`` so the system has a database / setup
    state; after that, anonymous browsing of ``/login`` should still return
    a clean login page with no nav.js injection.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        _setup_admin(LocalClient(base_url))
        # New cookie jar, no session -- test the public-path passthrough.
        anonymous = LocalClient(base_url)
        status, headers, body = anonymous.request("/login")
        assert status == 200
        assert isinstance(body, str)
        # Source contract: "request.url.path in main.PUBLIC_PATHS" -> return.
        assert NAV_SCRIPT_TAG not in body, (
            "app_navigation_middleware should NOT inject nav.js into "
            "PUBLIC_PATHS responses (/login here)"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 7. auth.enabled=False -- middleware passthrough.
# ---------------------------------------------------------------------------


def test_authentication_middleware_bypasses_when_auth_disabled(tmp_path, monkeypatch):
    """When ``main.effective_auth_config()`` returns ``{'enabled': False, ...}``,
    the middleware's branch-0 short-circuits and calls ``call_next`` directly.
    Concretely: an anonymous GET / returns 200 (the dashboard shell) rather
    than 303 -> ``/setup`` or 303 -> ``/login``.

    This is the deployment mode where the operator deliberately disables auth
    (single-user LAN install with no cookie seat).
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        # Override effective_auth_config(runtime) to report enabled=False.
        # Patch on app.middleware (its true import site) since middleware.py
        # now imports effective_auth_config directly from app.config_facades
        # rather than reaching it via main.effective_auth_config.
        middleware = sys.modules["app.middleware"]
        monkeypatch.setattr(
            middleware,
            "effective_auth_config",
            lambda: {
                "enabled": False,
                "session_timeout_hours": 12,
                "max_login_attempts": 5,
                "lockout_minutes": 15,
            },
        )
        # No _setup_admin, no _login. Fully anonymous.
        anonymous = LocalClient(base_url)
        status, headers, body = anonymous.request("/", follow_redirects=False)
        assert status == 200, (
            f"With auth.enabled=False, anonymous GET / should serve (200), "
            f"NOT redirect (303). Got status {status} "
            f"Location={LocalClient.header(headers, 'Location')!r}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
