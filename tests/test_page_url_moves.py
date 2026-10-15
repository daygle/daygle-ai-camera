"""Models and Detection pages live under /models and /detection."""
from __future__ import annotations

import pytest

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin

OLD_URLS = (
    '/onnx', '/arcface', '/camera-models', '/yamnet-tflite', '/yamnet',
    '/objects', '/sounds', '/face-recognition', '/people', '/live', '/search',
)

PAGES = {
    '/models': '<title>Object Models - Daygle AI Camera</title>',
    '/models/faces': '<title>Face Models - Daygle AI Camera</title>',
    '/models/cameras': '<title>Camera Models - Daygle AI Camera</title>',
    '/models/sound': 'yamnet-tflite.js',
    '/models/settings': '<title>Model Settings - Daygle AI Camera</title>',
    '/detection': 'objects.js',
    '/detection/sounds': 'sounds.js',
    '/detection/faces': '<title>Face Recognition - Daygle AI Camera</title>',
}


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    _setup_admin(client)
    _login(client)
    yield client
    server.should_exit = True
    thread.join(timeout=5)


def test_old_page_urls_are_gone(admin_client):
    for old in OLD_URLS:
        status, _headers, _body = admin_client.request(old, follow_redirects=False)
        assert status == 404, old


def test_new_pages_are_served(admin_client):
    for path, marker in PAGES.items():
        status, _headers, body = admin_client.request(path)
        assert status == 200, path
        assert marker in body, path


def test_model_pages_have_no_inner_tab_row(admin_client):
    for path in ('/models', '/models/faces', '/models/settings'):
        _status, _headers, body = admin_client.request(path)
        assert 'role="tablist"' not in body, path
    # Face Models carries both face detection and recognition.
    _status, _headers, body = admin_client.request('/models/faces')
    assert 'id="faceModelList"' in body and 'id="arcfaceModelList"' in body
    assert body.index('/static/models.js') < body.index('/static/arcface.js')
    # Settings holds the one detection settings form.
    _status, _headers, body = admin_client.request('/models/settings')
    assert 'id="aiSettingsForm"' in body


def test_new_pages_are_admin_only():
    from app.state import ADMIN_PATHS

    assert set(PAGES) <= ADMIN_PATHS
