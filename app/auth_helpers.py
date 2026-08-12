"""Auth page / cookie helpers extracted from app/main.py.

These functions were previously defined in app/main.py alongside the FastAPI
application. Moving them here lets router files import them directly without
going through the ``import app.main as main`` hybrid pattern.

The functions do NOT import app.main - they use direct imports only.
"""
from __future__ import annotations

import secrets
from html import escape

from fastapi.responses import HTMLResponse, Response
from fastapi import Request

from app.auth import CSRF_COOKIE, SESSION_COOKIE
from app.config_facades import effective_auth_config


def _session_cookie_name() -> str:
    """Return the configured session cookie name, evaluated at call time."""
    return str(effective_auth_config().get('cookie_name', SESSION_COOKIE))


def _get_cookie_domain(config: dict | None = None) -> str | None:
    """Return the configured ``auth.cookie_domain`` (operator override) or
    ``None`` so the cookie binds to the response host.

    Returning ``None`` keeps the ``Domain`` attribute off the cookie entirely,
    which is the LAN-friendly default: no subdomain-cookie-tossing surface for
    the single-host deployment. If the operator sets ``auth.cookie_domain``
    explicitly (e.g. ``.lab.example`` for a multi-subdomain rollout), honour
    it here and the same value flows into BOTH the CSRF cookie and the
    session cookie so neither can be subdomain-tossed separately.
    """
    raw = (config if config is not None else effective_auth_config()).get('cookie_domain')
    if not raw:
        return None
    candidate = str(raw).strip()
    return candidate or None


def auth_page(title: str, body: str) -> HTMLResponse:
    """Build an HTMLResponse for a standalone auth page (login / setup)."""
    # Detect theme: honour an explicit localStorage choice first, then fall
    # back to the browser/OS prefers-color-scheme so the login page matches
    # the user's system theme on first visit.
    theme_script = (
        '<script>(function(){'
        'var t=localStorage.getItem("daygle.theme");'
        'if(t)document.documentElement.classList.add(t);'
        'else if(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches)'
        'document.documentElement.classList.add("light");'
        '})()</script>'
    )
    # ``body`` is assembled from fixed templates; callers escape any user
    # supplied error text before interpolating it. This is not an exception
    # detail renderer.
    # codeql[py/stack-trace-exposure]
    return HTMLResponse(
        f'<!doctype html>\n'
        f'<html lang="en">{theme_script}<head><meta charset="utf-8" />'
        f'<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f'<title>{escape(title)} · Daygle AI Camera</title>'
        f'<link rel="stylesheet" href="/static/styles.css" /></head>\n'
        f'<body><main class="auth-shell"><section class="card auth-card">'
        f'<p class="eyebrow">Daygle AI Camera</p>{body}</section></main></body></html>'
    )


def set_csrf_cookie(response: Response, token: str, request: Request) -> None:
    """Set the CSRF cookie on *response*."""
    response.set_cookie(
        CSRF_COOKIE, token,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
        max_age=3600,
        domain=_get_cookie_domain(),
    )


def csrf_token_response(
    request: Request,
    title: str,
    body_template: str,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Generate a CSRF token, set the cookie, and return the auth page."""
    token = secrets.token_urlsafe(32)
    response = auth_page(title, body_template.format(csrf=escape(token)))
    response.status_code = status_code
    set_csrf_cookie(response, token, request)
    return response


def set_session_cookie(
    response: Response,
    request: Request,
    token: str,
    expires_at: str,
    *,
    auth_config: dict | None = None,
) -> None:
    """Set the session cookie on *response*.

    Callers that already resolved the live auth config can pass it to avoid a
    second database read while a response is being finalized (notably during
    backup/restore operations).
    """
    config = auth_config if auth_config is not None else effective_auth_config()
    session_hours = float(config.get('session_timeout_hours', 12))
    response.set_cookie(
        str(config.get('cookie_name', SESSION_COOKIE)), token,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
        expires=expires_at,
        max_age=int(session_hours * 3600),
        # M1 (round-7): bound the cookie to an explicit host if the
        # operator has set ``auth.cookie_domain``. Same domain as the
        # CSRF cookie so neither can be subdomain-tossed separately.
        domain=_get_cookie_domain(config),
    )


def clear_auth_cookies(response: Response) -> None:
    """Delete both the CSRF and session cookies on *response*."""
    response.delete_cookie(_session_cookie_name())
    response.delete_cookie(CSRF_COOKIE)
