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


def auth_page(title: str, body: str) -> HTMLResponse:
    """Build an HTMLResponse for a standalone auth page (login / setup)."""
    return HTMLResponse(
        f'<!doctype html>\n'
        f'<html lang="en"><head><meta charset="utf-8" />'
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
) -> None:
    """Set the session cookie on *response*."""
    session_hours = float(effective_auth_config().get('session_timeout_hours', 12))
    response.set_cookie(
        _session_cookie_name(), token,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
        expires=expires_at,
        max_age=int(session_hours * 3600),
    )


def clear_auth_cookies(response: Response) -> None:
    """Delete both the CSRF and session cookies from *response*."""
    response.delete_cookie(_session_cookie_name())
    response.delete_cookie(CSRF_COOKIE)
