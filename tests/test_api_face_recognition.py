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
            data=json.dumps({'enabled': False, 'match_threshold': 0.6}).encode(),
            headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        )
        assert status == 200
        # Disabled config reloads successfully but is not "available".
        assert body['reload_succeeded'] is False
        assert body['match_threshold'] == 0.6

        # The setting round-trips on a fresh GET.
        status, _headers, fetched = client.request('/api/settings/face-recognition')
        assert status == 200
        assert fetched['match_threshold'] == 0.6
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


def test_list_embedding_models(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _h, body = client.request('/api/settings/face-recognition/embedding-models')
        assert status == 200
        ids = {m['id'] for m in body['models']}
        assert 'arcface-r100' in ids
        assert all('url' not in m for m in body['models'])  # internal detail hidden
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_download_embedding_model_unknown_id(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _h, _b = client.request(
            '/api/settings/face-recognition/embedding-models/nope/download',
            method='POST',
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_download_embedding_model_sets_active_model(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    import app.model_management as mm
    import app.api.settings_face_recognition_router as frr

    # Redirect model downloads into tmp and stub the network fetch so the test
    # never pulls the real ~249MB model.
    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)

    def fake_download(url, destination, **_kwargs):
        assert url.startswith('https://')
        destination.write_bytes(b'fake onnx')

    monkeypatch.setattr(frr, '_download_weights', fake_download)

    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _h, body = client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100/download',
            method='POST',
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        # The downloaded model becomes the active embedding model; recognition
        # stays disabled until an admin enables it.
        assert body['model_path'] == 'models/arcface-r100.onnx'
        assert body['model_id'] == 'arcface-r100'
        assert body['enabled'] is False
        assert (models_dir / 'arcface-r100.onnx').read_bytes() == b'fake onnx'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_put_face_recognition_enables_after_download(tmp_path, monkeypatch):
    """Regression: the settings form omits model fields, so enabling must not
    wipe a downloaded embedding model (the UI now carries model_path/model_id
    through the payload - this locks the backend contract for that payload).
    """
    app, _db = _load_app(tmp_path, monkeypatch)
    import app.model_management as mm
    import app.api.settings_face_recognition_router as frr

    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)

    def fake_download(url, destination, **_kwargs):
        destination.write_bytes(b'fake onnx')

    monkeypatch.setattr(frr, '_download_weights', fake_download)

    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _h, body = client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100/download',
            method='POST',
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert body['model_path'] == 'models/arcface-r100.onnx'

        # Exactly what the (fixed) front-end sends: the form fields plus
        # the active model carried through hidden inputs. Unknown-face
        # alerting is no longer part of this payload (it lives on the
        # Face Rules tab's ``_unknown`` system rule).
        status, _h, body = client.request(
            '/api/settings/face-recognition',
            method='PUT',
            data=json.dumps({
                'enabled': True,
                'match_threshold': 0.5,
                'min_face_pixels': 0,
                'retention_days': 0,
                'model_path': body['model_path'],
                'model_id': body['model_id'],
            }).encode(),
            headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert body['enabled'] is True
        assert body['model_path'] == 'models/arcface-r100.onnx'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _stub_models_env(monkeypatch, tmp_path):
    """Redirect the models dir into tmp and stub the network fetch.

    Must be called AFTER _load_app (which re-imports the app.* tree). Returns the
    tmp models directory; the fake fetch writes a small placeholder onnx.
    """
    import app.model_management as mm
    import app.api.settings_face_recognition_router as frr
    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)

    def fake_download(url, destination, **_kwargs):
        assert url.startswith('https://')
        destination.write_bytes(b'fake onnx')

    monkeypatch.setattr(frr, '_download_weights', fake_download)
    return models_dir


def test_embedding_models_report_installed_and_active(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    models_dir = _stub_models_env(monkeypatch, tmp_path)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        # Download the fp32 model -> it becomes installed + active.
        status, _h, body = client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100/download',
            method='POST', headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        by_id = {m['id']: m for m in body['models']}
        assert by_id['arcface-r100']['installed'] is True
        assert by_id['arcface-r100']['active'] is True
        # The int8 variant is neither installed nor active.
        assert by_id['arcface-r100-int8']['installed'] is False
        assert by_id['arcface-r100-int8']['active'] is False
        assert (models_dir / 'arcface-r100.onnx').exists()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_select_installed_embedding_model_switches_active(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    models_dir = _stub_models_env(monkeypatch, tmp_path)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        # fp32 downloaded + active; place the int8 file on disk (installed, not active).
        client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100/download',
            method='POST', headers={'X-CSRF-Token': csrf},
        )
        (models_dir / 'arcface-r100-int8.onnx').write_bytes(b'fake int8')

        # Selecting an installed model must switch active without re-downloading.
        status, _h, body = client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100-int8/select',
            method='POST', headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert body['model_path'] == 'models/arcface-r100-int8.onnx'
        by_id = {m['id']: m for m in body['models']}
        assert by_id['arcface-r100-int8']['active'] is True
        assert by_id['arcface-r100']['active'] is False
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_select_uninstalled_embedding_model_rejected(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    _stub_models_env(monkeypatch, tmp_path)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _h, _b = client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100-int8/select',
            method='POST', headers={'X-CSRF-Token': csrf},
        )
        assert status == 400  # not installed -> can't select
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_delete_embedding_model_refuses_active_and_removes_inactive(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    models_dir = _stub_models_env(monkeypatch, tmp_path)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100/download',
            method='POST', headers={'X-CSRF-Token': csrf},
        )
        (models_dir / 'arcface-r100-int8.onnx').write_bytes(b'fake int8')

        # The active model cannot be deleted.
        status, _h, _b = client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100',
            method='DELETE', headers={'X-CSRF-Token': csrf},
        )
        assert status == 400
        assert (models_dir / 'arcface-r100.onnx').exists()

        # An installed, inactive model can be deleted.
        status, _h, body = client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100-int8',
            method='DELETE', headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert not (models_dir / 'arcface-r100-int8.onnx').exists()
        by_id = {m['id']: m for m in body['models']}
        assert by_id['arcface-r100-int8']['installed'] is False
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_update_embedding_model_refreshes_file(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    models_dir = _stub_models_env(monkeypatch, tmp_path)
    import app.api.settings_face_recognition_router as frr
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100/download',
            method='POST', headers={'X-CSRF-Token': csrf},
        )
        assert (models_dir / 'arcface-r100.onnx').read_bytes() == b'fake onnx'

        # Update re-downloads over the existing file.
        def fresh_download(url, destination, **_kwargs):
            destination.write_bytes(b'refreshed onnx')

        monkeypatch.setattr(frr, '_download_weights', fresh_download)
        status, _h, _b = client.request(
            '/api/settings/face-recognition/embedding-models/arcface-r100/update',
            method='POST', headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert (models_dir / 'arcface-r100.onnx').read_bytes() == b'refreshed onnx'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_embedding_model_actions_unknown_id(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    _stub_models_env(monkeypatch, tmp_path)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        for verb, path in [
            ('POST', '/api/settings/face-recognition/embedding-models/nope/select'),
            ('POST', '/api/settings/face-recognition/embedding-models/nope/update'),
            ('DELETE', '/api/settings/face-recognition/embedding-models/nope'),
        ]:
            status, _h, _b = client.request(path, method=verb, headers={'X-CSRF-Token': csrf})
            assert status == 404, path
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_face_recognition_page_served_to_admin(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _h, page = client.request('/face-recognition')
        assert status == 200
        assert '<title>Face Recognition - Daygle AI Camera</title>' in page
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_face_recognition_page_requires_admin(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)  # not logged in
        status, _h, _b = client.request('/face-recognition', follow_redirects=False)
        assert status in (302, 303, 401, 403)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
