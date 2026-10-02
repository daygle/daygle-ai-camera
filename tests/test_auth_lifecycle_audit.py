"""Auth lifecycle audit: deadlines, cookies, redirects and logout safety."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from starlette.datastructures import Headers, URL
from starlette.responses import Response

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin


@pytest.fixture
def auth(tmp_path):
    module = importlib.import_module('app.auth')
    service = module.AuthService(str(tmp_path / 'auth.sqlite3'), {})
    service.create_user('admin', 'Valid1!Password', role='admin')
    return service


def _sign_in(auth):
    with patch.object(auth, 'verify_password', return_value=True):
        return auth.authenticate('admin', 'Valid1!Password', '127.0.0.1')[1]


def test_login_deadline_is_capped_to_absolute_lifetime(auth):
    auth.apply_config({'session_timeout_hours': 24, 'absolute_session_lifetime_seconds': 60})
    token = _sign_in(auth)
    with auth.connect() as db:
        row = db.execute('SELECT expires_at, absolute_expires_at FROM user_sessions WHERE session_token=?', (token,)).fetchone()
    assert row['expires_at'] == row['absolute_expires_at']


def test_renewed_session_never_reports_deadline_past_absolute_cap(auth):
    token = _sign_in(auth)
    now = datetime.now(timezone.utc)
    cap = (now + timedelta(seconds=40)).isoformat()
    with auth.connect() as db:
        db.execute('UPDATE user_sessions SET expires_at=?, absolute_expires_at=? WHERE session_token=?',
                   ((now + timedelta(minutes=1)).isoformat(), cap, token))
    session = auth.get_session(token)
    assert session['expires_at'] == cap
    with auth.connect() as db:
        assert db.execute('SELECT absolute_expires_at FROM user_sessions WHERE session_token=?', (token,)).fetchone()[0] == cap


def test_fresh_session_poll_does_not_write_last_seen(auth):
    token = _sign_in(auth)
    with auth.connect() as db:
        before = db.execute('SELECT last_seen_at FROM user_sessions WHERE session_token=?', (token,)).fetchone()[0]
    assert auth.get_session(token)
    assert auth.get_session(token)
    with auth.connect() as db:
        after = db.execute('SELECT last_seen_at FROM user_sessions WHERE session_token=?', (token,)).fetchone()[0]
    assert before == after


def test_expired_session_cannot_be_resurrected(auth):
    token = _sign_in(auth)
    with auth.connect() as db:
        db.execute('UPDATE user_sessions SET expires_at=? WHERE session_token=?', ('2000-01-01T00:00:00+00:00', token))
    assert auth.get_session(token) is None
    with auth.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM user_sessions WHERE session_token=?', (token,)).fetchone()[0] == 0


def test_null_absolute_cap_cannot_make_legacy_session_immortal(auth):
    token = _sign_in(auth)
    with auth.connect() as db:
        db.execute('UPDATE user_sessions SET absolute_expires_at=NULL, created_at=?, expires_at=? WHERE session_token=?',
                   ('2000-01-01T00:00:00+00:00', '3026-01-01T00:00:00+00:00', token))
    assert auth.get_session(token) is None


def test_cookie_max_age_tracks_real_deadline(monkeypatch):
    helpers = importlib.import_module('app.auth_helpers')
    request = SimpleNamespace(url=URL('http://camera/'), headers=Headers(), client=SimpleNamespace(host='203.0.113.1'))
    response = Response()
    helpers.set_session_cookie(response, request, 'token', (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                               auth_config={'session_timeout_hours': 12})
    cookie = SimpleCookie(response.headers['set-cookie'])['daygle_session']
    assert 58 <= int(cookie['max-age']) <= 60
    assert cookie['httponly'] and cookie['samesite'] == 'lax'


@pytest.mark.parametrize('trusted', [True, False])
def test_https_proxy_cookie_secure_flag_requires_trust(monkeypatch, trusted):
    helpers = importlib.import_module('app.auth_helpers')
    gates = importlib.import_module('app.auth_gates')
    monkeypatch.setattr(gates, 'is_trusted_proxy', lambda _: trusted)
    request = SimpleNamespace(url=URL('http://camera/'), headers=Headers({'x-forwarded-proto': 'https'}),
                              client=SimpleNamespace(host='10.0.0.1'))
    response = Response()
    helpers.set_session_cookie(response, request, 'token', '', auth_config={})
    assert bool(SimpleCookie(response.headers['set-cookie'])['daygle_session']['secure']) is trusted


@pytest.mark.parametrize('target', ['/events\n/evil', '/\x00evil', '/events\t'])
def test_return_path_rejects_control_characters(target):
    router = importlib.import_module('app.api.web_router')
    assert router._safe_return_to(target) == '/'


def test_login_destination_survives_alias_and_failed_attempt(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        status, _headers, body = client.request('/login?returnTo=%2Frecordings%3Fcamera%3Dfront')
        assert status == 200
        assert 'value="/recordings?camera=front"' in body
        csrf = client.cookie('daygle_csrf')
        status, _headers, body = client.request('/login', method='POST',
            data=urlencode({'username': 'admin', 'password': 'wrong', 'csrf_token': csrf,
                            'return_to': '/recordings?camera=front'}).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        assert status == 200
        assert 'value="/recordings?camera=front"' in body
    finally:
        server.should_exit = True
        thread.join(5)


def test_auth_template_preserves_braces_in_return_path(monkeypatch):
    helpers = importlib.import_module('app.auth_helpers')
    monkeypatch.setattr(helpers, 'set_csrf_cookie', lambda *a: None)
    response = helpers.csrf_token_response(None, 'Login', '<input value="/events?q={example}"><input value="{csrf}">')
    assert b'/events?q={example}' in response.body
    assert b'{csrf}' not in response.body


def test_cross_origin_logout_does_not_revoke_session(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _headers, _body = client.request('/logout', method='POST', headers={'Origin': 'https://evil.example'})
        assert status == 403
        status, headers, _payload = client.request('/api/auth/me')
        assert status == 200
        assert 'no-store' in headers.get('Cache-Control', headers.get('cache-control', ''))
        assert client.request('/logout', method='POST')[0] == 200
    finally:
        server.should_exit = True
        thread.join(5)
