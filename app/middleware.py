"""HTTP middleware callables extracted from ``app/main.py`` (Phase-15).

Extracted from ``app/main.py`` lines 1765-1813 (final phase of the
hybrid-pattern router/middleware split). Same template as the
Phase-1-14 router extractions: ``import app.main as main`` at module
top, every global / helper read through ``main.<name>`` *inside*
callable bodies.

Two plain async callables are exported -- ``authentication_middleware``
and ``app_navigation_middleware``. They are NOT decorated with
``@app.middleware('http')`` here because the decorator requires a
live FastAPI ``app`` instance, and importing ``app`` here would
create a circular dependency. Instead, ``app/main.py`` registers
them at startup via ``app.middleware('http')(callable)`` calls
appended to the bottom of ``main.py`` AFTER all helpers + every
``include_router`` rebind has run.

The hybrid-pattern load-order that resolves the circular edge:

1. ``app/main.py`` starts loading. Its top imports + middle-body
   helpers + bottom-of-file rebinds execute in order.
2. ``app/main.py`` reaches the bottom-of-file block containing::
       from app.middleware import authentication_middleware, app_navigation_middleware
       app.middleware('http')(authentication_middleware)
       app.middleware('http')(app_navigation_middleware)
3. Python loads ``app/middleware.py``. Its module top executes::
       import app.main as main
   ``app.main`` is partially loaded -- Python returns the partial
   module (standard circular-import handling). The two ``async def``
   callables defined here are deferred-execution closures that
   resolve ``main.<attr>`` at call time, NOT at module top.
4. ``app/main.py`` continues -- both ``app.middleware('http')(...)``
   calls register the callables on the FastAPI app's middleware
   stack. The registration order matches the original
   ``@app.middleware('http')`` decorator order: authentication FIRST,
   navigation SECOND.

Callables moved (2):

- ``authentication_middleware`` -- session-cookie validation +
  admin-path gating + CSRF token check on mutating ``/api/*`` methods.
- ``app_navigation_middleware`` -- injects ``<script src="/static/nav.js">``
  before the closing ``</body>`` of every HTML page response.

the previous ``@app.middleware('http')`` decorators lived on
``app/main.py`` lines 1771 + 1799; the @decorator syntax can't move
into another module without the live ``app`` instance, so we keep the
*registration* line in main.py and move only the *callable body* here.

Helpers KEPT on ``app.main`` (this module calls them via ``main.<name>``):

- ``main.effective_auth_config`` - facaded config read for the auth
  subsystem. Resolves on-disk ``auth`` setting + defaults.
- ``main.PUBLIC_PATHS`` - set of paths that bypass authentication
  (``/favicon.ico``, ``/login``, ``/setup``).
- ``main.PUBLIC_PREFIXES`` - tuple of path prefixes that bypass auth
  (``/static/``).
- ``main.auth`` - the AuthService instance.
- ``main.auth.users_exist`` - boolean check for any user row.
- ``main.auth.get_session`` - cookie -> session lookup.
- ``main.JSONResponse`` - re-exported at ``app.main`` from
  ``fastapi.responses`` per hybrid-pattern rule 5 (Phase-13 fix).
- ``main.RedirectResponse`` - same re-export contract.
- ``main.Response`` - same re-export contract (``starlette.responses.Response``
  actually; FastAPI re-exports it under starlette's name).
- ``main.SESSION_COOKIE_NAME`` - the resolved session-cookie name
  (auth.config['session_cookie_name']).
- ``main.CSRF_HEADER`` - the header name CSRF tokens live in for
  mutating requests.
- ``main.ADMIN_PATHS`` - set of paths that require admin role.
- ``main.MUTATING_METHODS`` - ``{'POST', 'PUT', 'PATCH', 'DELETE'}`` --
  any non-GET method needs CSRF.

Tests go through ``LocalClient.request`` rather than calling
``main.<attr>`` directly, so the Phase-15 invariant walker validates
every ``main.<attr>`` reach here against ``app.main``'s actual
namespace on every test run.

No new file ``app/middleware.py`` previously existed; this is the
first non-router module under ``app/`` outside of the leaf modules
(``app.auth``, ``app.database``, ``app.camera_backend``, etc.). The
hybrid-pattern discipline (Pool A bare-name rebinds in main.py +
Pool C bare-name reach in consumers) extends to non-router files
without modification -- the rule set in ``app/api/__init__.py``
applies verbatim.
"""

from __future__ import annotations

import logging
import urllib.parse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

import app.state as _state
from app.auth import CSRF_HEADER
from app.auth_helpers import set_session_cookie
from app.config_facades import effective_auth_config
from app.rate_limiter import admin_limiter
ADMIN_PATHS = _state.ADMIN_PATHS
MUTATING_METHODS = _state.MUTATING_METHODS
PUBLIC_PATHS = _state.PUBLIC_PATHS
PUBLIC_PREFIXES = _state.PUBLIC_PREFIXES

# Module-level logger so the origin-check warning path doesn't need to
# re-instantiate on every request. Same logging tree as ``daygle.ai``
# elsewhere; logged at WARNING because Origin/Referer anomalies on
# pre-auth paths are interesting but not necessarily hostile.
logger = logging.getLogger(__name__)


def _is_same_origin(request: Request) -> tuple[bool, str]:
    """Same-origin check for state-changing requests (defence-in-depth vs CSRF).

    Reads ``Origin`` first, then falls back to ``Referer`` (some privacy
    extensions strip only one). Returns ``(True, '')`` on a match; on
    mismatch returns ``(False, '<reason>')``. ``Origin: null`` is treated
    as a mismatch for non-pre-auth paths so sandboxed iframes / ``data:``
    URI exploits don't bypass the guard.

    Mismatch criteria on state-changing ``/api/`` requests:
      * missing BOTH Origin and Referer headers
      * scheme / host / port disagree with ``request.url``
      * ``Origin: null`` literal (privacy browser / sandboxed)
    """
    origin = request.headers.get('Origin')
    referer = request.headers.get('Referer')
    if origin is None and referer is None:
        return False, 'Missing Origin and Referer headers'
    source = origin or referer or ''
    if not source or source.strip().lower() == 'null':
        return False, (
            f'{"Origin" if origin else "Referer"} is empty/null '
            f'(sandboxed or privacy-mode browser)'
        )
    try:
        parsed = urllib.parse.urlsplit(source)
    except ValueError:
        return False, f'Unparsable {"Origin" if origin else "Referer"} value'
    # Resolve the *externally visible* scheme/host/port. ``request.url`` reflects
    # the connection uvicorn actually received, which is the INTERNAL
    # ``http://host:8080`` when a TLS-terminating reverse proxy sits in front --
    # while the browser's ``Origin`` is the EXTERNAL ``https://host``. Comparing
    # the two directly hard-403s every state-changing request in that (documented)
    # topology. When the direct peer is a trusted proxy we therefore honour
    # ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` to reconstruct the external
    # origin. This reuses the same trust model as ``_request_ip`` (only peers in
    # ``auth.trusted_proxies`` can influence it), so an untrusted client cannot
    # forge the comparison target by sending its own forwarded headers.
    expected_scheme = request.url.scheme
    expected_host = request.url.hostname
    expected_port = request.url.port
    from app.auth_gates import _trusted_proxies
    direct_peer = request.client.host if getattr(request, 'client', None) else ''
    if direct_peer in _trusted_proxies():
        forwarded_proto = request.headers.get('x-forwarded-proto')
        if forwarded_proto:
            expected_scheme = forwarded_proto.split(',')[0].strip() or expected_scheme
        forwarded_host = request.headers.get('x-forwarded-host')
        if forwarded_host:
            # First hop is the external host; may be ``host`` or ``host:port``
            # (IPv6 arrives bracketed, which ``urlsplit`` handles via ``//``).
            host_entry = forwarded_host.split(',')[0].strip()
            fparsed = urllib.parse.urlsplit(f'//{host_entry}')
            if fparsed.hostname:
                expected_host = fparsed.hostname
                expected_port = fparsed.port
    # Normalise the scheme's default port: a browser ``Origin`` omits the port
    # for 443 (https) / 80 (http), so ``https://host`` (port None) must compare
    # equal to an ``https`` request served on 443.
    def _effective_port(scheme: str, port: int | None) -> int | None:
        if port is not None:
            return port
        return {'https': 443, 'http': 80}.get(scheme)

    if (
        parsed.scheme != expected_scheme
        or parsed.hostname != expected_host
        or _effective_port(parsed.scheme, parsed.port)
        != _effective_port(expected_scheme, expected_port)
    ):
        return False, (
            f'{"Origin" if origin else "Referer"} '
            f'{parsed.scheme}://{parsed.hostname or "<empty>"}'
            f'{":" + str(parsed.port) if parsed.port else ""} '
            f'does not match request '
            f'{expected_scheme}://{expected_host}'
            f'{":" + str(expected_port) if expected_port else ""}'
        )
    return True, ''


async def authentication_middleware(request: Request, call_next):
    auth_config = effective_auth_config()
    if not auth_config.get('enabled', True):
        return await call_next(request)
    path = request.url.path
    if path in PUBLIC_PATHS or any(
        (path.startswith(prefix) for prefix in PUBLIC_PREFIXES)
    ):
        return await call_next(request)
    has_users = _state.auth.users_exist()
    if not has_users:
        if path.startswith('/api/'):
            return JSONResponse(
                {'detail': 'Initial administrator setup is required.'},
                status_code=403,
            )
        return RedirectResponse('/setup', status_code=303)
    _cookie_name = str(auth_config.get('cookie_name', 'session'))
    session = _state.auth.get_session(request.cookies.get(_cookie_name))
    if session is None:
        if path.startswith('/api/'):
            return JSONResponse(
                {'detail': 'Authentication required'}, status_code=401,
            )
        return RedirectResponse('/login', status_code=303)
    request.state.session = session
    request.state.user = session['user']
    admin_required = (
        path in ADMIN_PATHS
        or path.startswith('/api/users')
        or path.startswith('/api/settings/ai')
        or path.startswith('/api/settings/system')
        or path.startswith('/api/update/')
        or (path.startswith('/api/cameras') and request.method in MUTATING_METHODS)
        or (path.startswith('/api/settings/alert-email') and request.method in MUTATING_METHODS)
        or (path.startswith('/api/settings/alert-push') and request.method in MUTATING_METHODS)
        or (path.startswith('/api/settings/camera-offline') and request.method in MUTATING_METHODS)
        or (
            (path.startswith('/api/events') or path.startswith('/api/alerts'))
            and 'dismiss' in path
            and (request.method in MUTATING_METHODS)
        )
    )
    if admin_required and session['user']['role'] != 'admin':
        if path.startswith('/api/'):
            return JSONResponse(
                {'detail': 'Admin access required'}, status_code=403,
            )
        # Page routes: return a proper 403 HTML page instead of raw JSON.
        return HTMLResponse(
            content='''<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8">
<title>Access Denied - Daygle AI Camera</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/styles.css">
</head>
<body class="error-page-body">
<div class="error-page">
<h1>403</h1>
<p>You need administrator access to view this page.</p>
<a href="/" class="button primary">Return to Dashboard</a>
</div>
</body>
</html>''',
            status_code=403,
        )
    # Round-5 / M1: per-IP sliding-window throttle on admin state-changing
    # /api/* requests. Runs AFTER the role check (so 401/403 still fire
    # without spending a throttle slot on unauth'd callers) and BEFORE the
    # CSRF + Origin crypto work (so a wholesale flood cannot pin the CPU
    # round-4 H4-style). The limiter key is the trusted first-hop IP from
    # ``app.auth_gates._request_ip`` (RFC-7239-compatible).
    if (
        admin_required
        and session['user']['role'] == 'admin'
        and request.method in MUTATING_METHODS
        and path.startswith('/api/')
    ):
        from app.auth_gates import _request_ip
        admin_ip = _request_ip(request)
        if admin_ip and admin_limiter.is_rate_limited(admin_ip):
            return JSONResponse(
                {'detail': 'Admin endpoint rate-limited; slow down.'},
                status_code=429,
                headers={'Retry-After': '1'},
            )
        if admin_ip:
            admin_limiter.record(admin_ip)
    if (path.startswith('/api/') and request.method in MUTATING_METHODS):
        # M1: same-origin guard runs BEFORE the cookie+header CSRF check as
        # defence-in-depth. If the session cookie ever leaks cross-origin,
        # the Origin/Referer mismatch rejects the request FIRST so an
        # attacker can't ride the cookie on a /api/ POST. Pre-auth paths
        # (/api/auth/login + /api/setup) log a warning instead of a hard
        # reject: those POSTs are exposed to a real client that might
        # legitimately send ``Origin: null`` (privacy-mode browsers,
        # sandboxed iframes, ``data:``-URI navigations, etc.) -- the
        # trade-off is documented and prefer-warning over lockout.
        is_same_origin, origin_reason = _is_same_origin(request)
        if path in PUBLIC_PATHS or path == '/api/setup':
            if not is_same_origin:
                logger.warning(
                    'Same-origin check failed on pre-auth path %s: %s',
                    path, origin_reason,
                )
        elif not is_same_origin:
            return JSONResponse(
                {'detail': f'Origin check failed: {origin_reason}'},
                status_code=403,
            )
        csrf_header = request.headers.get(CSRF_HEADER)
        if not csrf_header or csrf_header != session['csrf_token']:
            return JSONResponse(
                {'detail': 'CSRF token missing or invalid'}, status_code=403,
            )
    response = await call_next(request)
    # ``expires_at`` is a sliding server-side deadline. Refresh the browser
    # cookie as well, otherwise its fixed login-time expiry can discard an
    # otherwise-renewed session after the first timeout window.
    # Logout deliberately clears the cookie in its handler; do not append a
    # fresh sliding-session cookie after that response has been generated.
    if not (path == '/logout' and request.method == 'POST'):
        set_session_cookie(
            response,
            request,
            session['session_token'],
            session['expires_at'],
            auth_config=auth_config,
        )
    return response


async def app_navigation_middleware(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get('content-type', '')
    path = request.url.path

    # ── Security response headers ────────────────────────────────────────
    # Defence-in-depth: set recommended security headers on every
    # response. These protect against clickjacking (X-Frame-Options),
    # MIME-type sniffing (X-Content-Type-Options), referrer leakage
    # (Referrer-Policy), and legacy browser features (Permissions-Policy).
    # CSP is omitted here because the application dynamically injects
    # inline styles and scripts that would require a per-route policy;
    # it can be added incrementally.
    security_headers: dict[str, str] = {
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'strict-origin-when-cross-origin',
        'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    }
    # Strict-Transport-Security should only be set over HTTPS.
    if request.url.scheme == 'https':
        security_headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

    if path in PUBLIC_PATHS or not content_type.startswith('text/html'):
        # Apply security headers even on non-HTML / public responses.
        # We mutate the response headers in-place rather than creating a new
        # Response, which would consume body_iterator and break streaming
        # endpoints (e.g. recording/video streams).
        for key, value in security_headers.items():
            response.headers.setdefault(key, value)
        # Static assets (JS/CSS) must be revalidated on EVERY page load so a
        # browser never keeps executing a stale pre-update script after an
        # update rewrites web/*.js in place -- the exact failure that kept the
        # dashboard running the old UTC-date-string `since` filter ("Today"
        # stopped at 10am local for UTC+10 operators) until the tab was
        # hard-refreshed. The ETag / Last-Modified validators Starlette emits
        # make the revalidation cheap (304) when the file is unchanged.
        # Direct assignment (not setdefault) deliberately overrides any
        # Cache-Control StaticFiles/FileResponse might emit.
        if path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        # Public HTML pages (login, setup) should also not be cached, so a
        # stale copy isn't shown after the auth state changes.
        if content_type.startswith('text/html'):
            response.headers.setdefault('Cache-Control', 'no-store, must-revalidate')
        return response
    body = b''
    async for chunk in response.body_iterator:
        body += chunk
    marker = b'</body>'
    script = b'<script src="/static/nav.js"></script>'
    if marker in body and script not in body:
        body = body.replace(marker, script + marker)
    headers = dict(response.headers)
    headers.pop('content-length', None)
    for key, value in security_headers.items():
        headers.setdefault(key, value)
    # Don't let the browser serve a cached copy of a protected HTML page
    # after the session has expired - otherwise the user sees stale UI.
    headers.setdefault('Cache-Control', 'no-store, must-revalidate')
    return Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type='text/html',
    )
