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

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response

import app.state as _state
from app.auth import CSRF_HEADER
from app.config_facades import effective_auth_config


async def authentication_middleware(request: Request, call_next):
    if not effective_auth_config().get('enabled', True):
        return await call_next(request)
    path = request.url.path
    if path in _state.PUBLIC_PATHS or any(
        (path.startswith(prefix) for prefix in _state.PUBLIC_PREFIXES)
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
    _cookie_name = str(effective_auth_config().get('cookie_name', 'session'))
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
        path in _state.ADMIN_PATHS
        or path.startswith('/api/users')
        or path.startswith('/api/settings/ai')
        or path.startswith('/api/settings/system')
        or path.startswith('/api/update/')
        or (path.startswith('/api/cameras') and request.method in _state.MUTATING_METHODS)
        or (path.startswith('/api/settings/alert-email') and request.method in _state.MUTATING_METHODS)
        or (path.startswith('/api/settings/alert-push') and request.method in _state.MUTATING_METHODS)
        or (path.startswith('/api/settings/camera-offline') and request.method in _state.MUTATING_METHODS)
        or (
            (path.startswith('/api/events') or path.startswith('/api/alerts'))
            and 'dismiss' in path
            and (request.method in _state.MUTATING_METHODS)
        )
    )
    if admin_required and session['user']['role'] != 'admin':
        return JSONResponse(
            {'detail': 'Admin access required'}, status_code=403,
        )
    if (path.startswith('/api/') or path == '/logout') and request.method in _state.MUTATING_METHODS:
        csrf_header = request.headers.get(CSRF_HEADER)
        if not csrf_header or csrf_header != session['csrf_token']:
            return JSONResponse(
                {'detail': 'CSRF token missing or invalid'}, status_code=403,
            )
    return await call_next(request)


async def app_navigation_middleware(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get('content-type', '')
    if request.url.path in _state.PUBLIC_PATHS or not content_type.startswith('text/html'):
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
    return Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type='text/html',
    )
