"""API smoke tests for the Stage 2b face-recognition settings router.

Exercises the router wiring end to end (registration in app.main, admin gate,
validation, persistence) through the shared HTTP harness.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_get_face_recognition_settings_defaults(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _headers, body = client.request('/api/settings/face-recognition')
        assert status == 200
        assert body['enabled'] is False
        assert body['model_loaded'] is False
        assert body['enrolled_people'] == 0
        assert body['enrolled_faces'] == 0
        assert body['match_threshold'] == 0.5
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_put_face_recognition_rejects_enabled_without_model(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, _body = client.request(
            '/api/settings/face-recognition',
            method='PUT',
            data=json.dumps({'enabled': True, 'model_path': ''}).encode(),
            headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        )
        assert status == 400
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_put_face_recognition_persists_disabled_config(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, body = client.request(
            '/api/settings/face-recognition',
            method='PUT',
            data=json.dumps({'enabled': False, 'match_threshold': 0.6, 'alert_unknown': True}).encode(),
            headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        )
        assert status == 200
        # Disabled config reloads successfully but is not "available".
        assert body['reload_succeeded'] is False
        assert body['match_threshold'] == 0.6
        assert body['alert_unknown'] is True

        # The setting round-trips on a fresh GET.
        status, _headers, fetched = client.request('/api/settings/face-recognition')
        assert status == 200
        assert fetched['match_threshold'] == 0.6
        assert fetched['alert_unknown'] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_face_recognition_settings_require_admin(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)  # admin exists, but we do NOT log in
        status, _headers, _body = client.request('/api/settings/face-recognition')
        assert status in (401, 403)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
