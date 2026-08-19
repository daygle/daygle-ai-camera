"""API smoke tests for the Stage 2c enrollment router (app/api/persons_router.py).

Exercises person CRUD, the admin gate, and the enroll-guard when recognition
is not configured. Successful face embedding is covered at the service level
(test_face_recognition_service.py) since it needs a loaded model.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def _json(csrf, body):
    return {
        'method': 'POST',
        'data': json.dumps(body).encode(),
        'headers': {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    }


def test_person_crud_flow(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # Create.
        status, _h, created = client.request(
            '/api/persons', **_json(csrf, {'name': 'Alex', 'notes': 'household'})
        )
        assert status == 200
        pid = created['id']
        assert created['name'] == 'Alex'
        assert created['face_count'] == 0

        # List.
        status, _h, listed = client.request('/api/persons')
        assert status == 200
        assert any(p['id'] == pid for p in listed['persons'])

        # Get (with faces).
        status, _h, fetched = client.request(f'/api/persons/{pid}')
        assert status == 200
        assert fetched['faces'] == []

        # Update name; notes preserved.
        status, _h, updated = client.request(
            f'/api/persons/{pid}',
            method='PATCH',
            data=json.dumps({'name': 'Alexis'}).encode(),
            headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert updated['name'] == 'Alexis'
        assert updated['notes'] == 'household'

        # Delete.
        status, _h, _b = client.request(
            f'/api/persons/{pid}', method='DELETE', headers={'X-CSRF-Token': csrf}
        )
        assert status == 200
        status, _h, _b = client.request(f'/api/persons/{pid}')
        assert status == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_create_person_requires_name(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _h, _b = client.request('/api/persons', **_json(csrf, {'name': '   '}))
        assert status == 400
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_enroll_face_rejected_when_recognition_disabled(tmp_path, monkeypatch):
    import cv2
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _h, created = client.request('/api/persons', **_json(csrf, {'name': 'Alex'}))
        pid = created['id']
        ok, buf = cv2.imencode('.png', __import__('numpy').zeros((112, 112, 3), dtype='uint8'))
        assert ok
        status, _h, _b = client.request(
            f'/api/persons/{pid}/faces',
            method='POST',
            data=buf.tobytes(),
            headers={'Content-Type': 'image/png', 'X-CSRF-Token': csrf},
        )
        # Recognition is off by default -> enrollment is refused with a 400.
        assert status == 400
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_face_thumbnail_endpoint(tmp_path, monkeypatch):
    """The thumbnail endpoint serves stored JPEG bytes for a face, and 404s for
    a face with no thumbnail or one belonging to a different person."""
    import numpy as np

    app, _db_path = _load_app(tmp_path, monkeypatch)
    # Import AFTER _load_app: it purges and re-imports the whole app.* tree, so
    # the live singletons (state.database) only exist once startup has run.
    import app.state as state
    from app.face_recognition import embedding_to_bytes
    db = state.database
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)

        pid = db.add_person('Alex')
        other = db.add_person('Sam')
        emb = embedding_to_bytes(np.asarray([1, 0, 0, 0], dtype=np.float32))
        jpeg = b'\xff\xd8\xff\xe0stub-jpeg'
        with_thumb = db.add_person_face(pid, embedding=emb, dim=4, model='arcface', thumbnail=jpeg)
        without_thumb = db.add_person_face(pid, embedding=emb, dim=4, model='arcface')

        # The listing reports the flag without shipping the blob.
        status, _h, fetched = client.request(f'/api/persons/{pid}')
        assert status == 200
        flags = {f['id']: f['has_thumbnail'] for f in fetched['faces']}
        assert flags == {with_thumb: True, without_thumb: False}

        # Stored thumbnail is served verbatim as image/jpeg.
        status, headers, body = client.request(f'/api/persons/{pid}/faces/{with_thumb}/thumbnail')
        assert status == 200
        assert headers.get('content-type') == 'image/jpeg'
        assert body == jpeg

        # No thumbnail -> 404.
        status, _h, _b = client.request(f'/api/persons/{pid}/faces/{without_thumb}/thumbnail')
        assert status == 404

        # A face that isn't this person's -> 404 (no cross-person leakage).
        status, _h, _b = client.request(f'/api/persons/{other}/faces/{with_thumb}/thumbnail')
        assert status == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_face_thumbnail_requires_admin(tmp_path, monkeypatch):
    app, _db_path = _load_app(tmp_path, monkeypatch)
    import app.state as state  # after _load_app re-imports the app.* tree
    db = state.database
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)  # not logged in
        pid = db.add_person('Alex')
        status, _h, _b = client.request(f'/api/persons/{pid}/faces/1/thumbnail')
        assert status in (401, 403)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_persons_require_admin(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)  # admin exists but not logged in
        status, _h, _b = client.request('/api/persons')
        assert status in (401, 403)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_people_page_served_to_admin(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _h, page = client.request('/people')
        assert status == 200
        assert '<title>People - Daygle AI Camera</title>' in page
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_people_page_requires_admin(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)  # not logged in
        status, _h, _b = client.request('/people', follow_redirects=False)
        assert status in (302, 303, 401, 403)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
