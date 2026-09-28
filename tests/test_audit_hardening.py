"""Regression tests for the codebase-audit fixes.

* A self-service password change revokes the user's OTHER sessions (a stolen
  cookie stops working) while keeping the session that made the change.
* Malformed or non-object JSON bodies answer 400 instead of HTTP 500.
* Non-string profile fields are rejected as a 400, not a 500.
* ``DELETE /api/events`` removes the deleted events' snapshot files instead of
  orphaning them on disk.
* Auth-cookie deletion carries the configured ``auth.cookie_domain``.
* The recording stream path is probed once per unchanged clip, not on every
  HTTP Range request.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from starlette.responses import Response

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin

from app.auth import AuthService


def _json_request(csrf: str, body: bytes, method: str = 'POST') -> dict:
    return {
        'method': method,
        'data': body,
        'headers': {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    }


def test_change_password_revokes_other_sessions_only(tmp_path):
    auth = AuthService(str(tmp_path / 'auth.sqlite3'), {})
    user = auth.create_user('alex', 'Str0ng-Pass!', role='admin')
    _u, current_token, _c, _e = auth.authenticate('alex', 'Str0ng-Pass!', '10.0.0.1')
    _u, stolen_token, _c, _e = auth.authenticate('alex', 'Str0ng-Pass!', '10.0.0.2')

    auth.change_password(
        int(user['id']), 'Str0ng-Pass!', 'N3w-Str0ng-Pass!', keep_session_token=current_token,
    )

    assert auth.get_session(current_token) is not None
    assert auth.get_session(stolen_token) is None


def test_change_password_without_keep_token_revokes_all_sessions(tmp_path):
    auth = AuthService(str(tmp_path / 'auth.sqlite3'), {})
    user = auth.create_user('alex', 'Str0ng-Pass!', role='admin')
    _u, token, _c, _e = auth.authenticate('alex', 'Str0ng-Pass!', '10.0.0.1')

    auth.change_password(int(user['id']), 'Str0ng-Pass!', 'N3w-Str0ng-Pass!')

    assert auth.get_session(token) is None


def test_json_body_errors_are_client_errors(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        # Malformed JSON on a helper-parsed route and on a validator route.
        status, _h, body = client.request('/api/persons', **_json_request(csrf, b'{not json'))
        assert status == 400, body
        status, _h, body = client.request(
            '/api/settings/system/live', **_json_request(csrf, b'{not json', method='PUT'),
        )
        assert status == 400, body
        # Valid JSON that is not an object.
        status, _h, body = client.request('/api/persons', **_json_request(csrf, b'["Alex"]'))
        assert status == 400, body
        status, _h, body = client.request('/api/profile', **_json_request(csrf, b'[]', method='PUT'))
        assert status == 400, body
        # Non-string profile field.
        status, _h, body = client.request(
            '/api/profile', **_json_request(csrf, json.dumps({'first_name': 7}).encode(), method='PUT'),
        )
        assert status == 400, body
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_profile_password_change_keeps_current_session(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    other = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        _login(other)
        status, _h, body = client.request(
            '/api/profile/password',
            **_json_request(csrf, json.dumps({
                'current_password': 'Admin123!', 'new_password': 'N3w-Admin123!',
            }).encode()),
        )
        assert status == 200, body
        status, _h, _b = client.request('/api/auth/me')
        assert status == 200
        status, _h, _b = other.request('/api/auth/me')
        assert status == 401
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_delete_all_events_removes_snapshot_files(tmp_path, monkeypatch):
    app, db_path = _load_app(tmp_path, monkeypatch)
    import app.state as _state

    snapshots = tmp_path / 'data' / 'snapshots'
    snapshots.mkdir(parents=True, exist_ok=True)
    snapshot = snapshots / 'event.jpg'
    thumbnail = snapshots / 'event.thumb.jpg'
    snapshot.write_bytes(b'jpeg')
    thumbnail.write_bytes(b'jpeg')
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        _state.database.add_event(
            created_at=datetime.now(timezone.utc).isoformat(),
            source='camera',
            snapshot_path=str(snapshot),
            thumbnail_path=str(thumbnail),
            detections=[],
        )
        status, _h, body = client.request('/api/events', method='DELETE', headers={'X-CSRF-Token': csrf})
        assert status == 200, body
        assert body['deleted'] == 1
        assert not snapshot.exists()
        assert not thumbnail.exists()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_clear_auth_cookies_uses_configured_cookie_domain(monkeypatch):
    import app.auth_helpers as auth_helpers

    monkeypatch.setattr(
        auth_helpers, 'effective_auth_config',
        lambda: {'cookie_name': 'daygle_session', 'cookie_domain': '.lab.example'},
    )
    response = Response()
    auth_helpers.clear_auth_cookies(response)
    cookies = [value for key, value in response.raw_headers if key == b'set-cookie']
    assert len(cookies) == 2
    assert all(b'Domain=.lab.example' in cookie for cookie in cookies)


def test_playable_stream_is_probed_once_per_unchanged_clip(tmp_path, monkeypatch):
    import app.api.recordings_router as rr

    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'video')
    calls = []

    def fake_stream_path(path):
        calls.append(path)
        return path

    monkeypatch.setattr(rr, 'recording_stream_path', fake_stream_path)
    monkeypatch.setattr(rr, 'mp4_has_video_stream', lambda _path: True)
    monkeypatch.setattr(rr, '_PLAYABLE_STREAM_CACHE', type(rr._PLAYABLE_STREAM_CACHE)())

    assert rr._playable_stream_path(clip) == clip
    assert rr._playable_stream_path(clip) == clip
    assert len(calls) == 1

    # A rewritten clip (new size) is probed again.
    clip.write_bytes(b'rebuilt video')
    assert rr._playable_stream_path(clip) == clip
    assert len(calls) == 2
