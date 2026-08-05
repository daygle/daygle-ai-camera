"""API integration tests: the Snapshots library (/api/snapshots).

The Snapshots page lists every event that captured a frame (non-empty
``snapshot_path``) and lets an admin delete a stored image without touching
the event or its recording. These tests pin the list filter, the `since`
bound, the delete semantics (file removed + path columns cleared + event
kept), and the admin gate.

Shared harness (LocalClient, _load_app, _server, _login, _setup_admin, ...)
lives in tests/support.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_list_snapshots_returns_only_framed_events(tmp_path, monkeypatch):
    """/api/snapshots lists events with a saved frame and skips frameless ones."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        _login(admin)
        now = datetime.now(timezone.utc).isoformat()
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'snap.png')
        framed_id = main.database.add_event(
            created_at=now,
            source='motion',
            snapshot_path=snapshot_path,
            detections=[{'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.5, 'height': 0.5}}],
            alert_triggered=True,
            metadata={'camera_id': 'front', 'camera_name': 'Front Door'},
        )
        main.database.add_alert(
            now, 'Front Door / person', framed_id, 'person', 0.9, 'person matched',
        )
        # Sound events are frameless and must NOT appear in the library.
        main.database.add_event(
            created_at=now, source='sound', snapshot_path=None, detections=[],
        )

        status, _headers, snapshots = admin.request('/api/snapshots')
        assert status == 200
        assert [snapshot['id'] for snapshot in snapshots] == [framed_id]
        snapshot = snapshots[0]
        assert snapshot['has_snapshot'] is True
        assert snapshot['detections'][0]['label'] == 'person'
        assert snapshot['alert'] is not None
        assert snapshot['metadata']['camera_name'] == 'Front Door'

        # The snapshot endpoint still serves the framed event's image.
        status, headers, body = admin.request(f'/api/events/{framed_id}/snapshot')
        assert status == 200
        assert LocalClient.header(headers, 'Content-Type') == 'image/jpeg'
        assert isinstance(body, (bytes, bytearray)) and len(body) > 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_list_snapshots_since_filter(tmp_path, monkeypatch):
    """The `since` bound narrows the library to recent frames."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        _login(admin)
        old = main.database.add_event(
            created_at='2026-06-01T00:00:00+00:00',
            source='motion',
            snapshot_path=main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'old.png'),
            detections=[],
        )
        recent = main.database.add_event(
            created_at='2026-06-10T00:00:00+00:00',
            source='motion',
            snapshot_path=main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'recent.png'),
            detections=[],
        )

        status, _headers, all_snapshots = admin.request('/api/snapshots')
        assert status == 200
        assert {snapshot['id'] for snapshot in all_snapshots} == {old, recent}

        # Local-day-start Z-suffix bound (what the frontend sends) must land
        # on the right side of the boundary after UTC normalisation.
        status, _headers, filtered = admin.request(
            '/api/snapshots?since=2026-06-05T00:00:00.000Z'
        )
        assert status == 200
        assert [snapshot['id'] for snapshot in filtered] == [recent]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_delete_snapshot_removes_image_keeps_event(tmp_path, monkeypatch):
    """DELETE /api/snapshots/{id} removes the file and clears the path columns
    while leaving the event row and any linked recording intact."""
    app, database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        csrf = _login(admin)
        now = datetime.now(timezone.utc).isoformat()
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'del.png')
        snapshot_file = Path(snapshot_path)
        assert snapshot_file.exists()
        event_id = main.database.add_event(
            created_at=now,
            source='motion',
            snapshot_path=snapshot_path,
            detections=[{'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.5, 'height': 0.5}}],
            metadata={'camera_id': 'front'},
        )
        recording_id = main.database.add_recording(
            event_id=event_id,
            camera_id='front',
            started_at=now,
            ended_at=now,
            duration_seconds=1.0,
            file_path=str(tmp_path / 'rec.mp4'),
            thumbnail_path=None,
            source='camera',
            created_at=now,
            trigger_type='object',
            trigger_label='person',
        )

        status, _headers, deleted = admin.request(
            f'/api/snapshots/{event_id}', method='DELETE', headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert deleted['ok'] is True
        assert not snapshot_file.exists(), 'snapshot image must be deleted from disk'

        # The event still exists but no longer advertises a snapshot.
        status, _headers, event = admin.request(f'/api/events/{event_id}')
        assert status == 200
        assert event['has_snapshot'] is False
        status, _headers, snap = admin.request(f'/api/events/{event_id}/snapshot')
        assert status == 404

        # The linked recording survives the snapshot delete.
        status, _headers, recording = admin.request(f'/api/recordings/{recording_id}')
        assert status == 200
        assert recording['id'] == recording_id

        # The event no longer shows up in the library.
        status, _headers, snapshots = admin.request('/api/snapshots')
        assert status == 200
        assert snapshots == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_delete_snapshot_gates_and_404s(tmp_path, monkeypatch):
    """Non-admins get 403; missing events and frameless events get 404."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        admin_csrf = _login(admin)
        status, _headers, viewer = admin.request(
            '/api/users',
            method='POST',
            json_body={'username': 'snapviewer', 'password': 'Viewer123!', 'role': 'viewer'},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200

        now = datetime.now(timezone.utc).isoformat()
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'gate.png')
        event_id = main.database.add_event(
            created_at=now, source='motion', snapshot_path=snapshot_path, detections=[],
        )
        frameless_id = main.database.add_event(
            created_at=now, source='sound', snapshot_path=None, detections=[],
        )

        # Viewer cannot delete (backend requires admin).
        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, viewer['username'], 'Viewer123!')
        status, _headers, denied = viewer_client.request(
            f'/api/snapshots/{event_id}', method='DELETE', headers={'X-CSRF-Token': viewer_csrf},
        )
        assert status == 403
        assert denied['detail'] == 'Admin access required'
        assert Path(snapshot_path).exists(), 'viewer 403 must not delete the image'

        # Missing event -> 404.
        status, _headers, missing = admin.request(
            '/api/snapshots/999999', method='DELETE', headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 404

        # Frameless event -> 404 (nothing to delete).
        status, _headers, frameless = admin.request(
            f'/api/snapshots/{frameless_id}', method='DELETE', headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 404

        # Admin can still delete the real snapshot afterwards.
        status, _headers, deleted = admin.request(
            f'/api/snapshots/{event_id}', method='DELETE', headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200
        assert deleted['ok'] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_snapshots_list_requires_auth(tmp_path, monkeypatch):
    """Unauthenticated clients get 401 from /api/snapshots (middleware)."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    anonymous = LocalClient(base_url)
    try:
        _setup_admin(anonymous)
        status, _headers, body = anonymous.request('/api/snapshots')
        assert status == 401
        assert body == {'detail': 'Authentication required'}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_list_snapshots_applies_recording_scoping(tmp_path, monkeypatch):
    """A viewer must not see a snapshot whose linked recording belongs to
    another user - /api/snapshots reuses the events router's scoping rule."""
    app, database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        admin_csrf = _login(admin)
        status, _headers, viewer = admin.request(
            '/api/users',
            method='POST',
            json_body={'username': 'scopedviewer', 'password': 'Viewer123!', 'role': 'viewer'},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200

        now = datetime.now(timezone.utc).isoformat()
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'scope.png')
        event_id = main.database.add_event(
            created_at=now, source='motion', snapshot_path=snapshot_path,
            detections=[{'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.5, 'height': 0.5}}],
            metadata={'camera_id': 'front'},
        )
        recording_id = main.database.add_recording(
            event_id=event_id, camera_id='front',
            started_at=now, ended_at=now, duration_seconds=1.0,
            file_path=str(tmp_path / 'rec.mp4'), thumbnail_path=None,
            source='camera', created_at=now, trigger_type='object', trigger_label='person',
        )
        # Stamp the recording as owned by the admin (user id 1, the first
        # account created by _setup_admin); the viewer must then be scoped
        # out of the event entirely.
        with sqlite3.connect(database_path) as db:
            db.execute('UPDATE recordings SET owner_user_id = 1 WHERE id = ?', (recording_id,))

        viewer_client = LocalClient(base_url)
        _login(viewer_client, viewer['username'], 'Viewer123!')
        status, _headers, viewer_snapshots = viewer_client.request('/api/snapshots')
        assert status == 200
        assert viewer_snapshots == [], 'viewer must not see a snapshot owned by another user'

        # Admins still see it.
        status, _headers, admin_snapshots = admin.request('/api/snapshots')
        assert status == 200
        assert [snapshot['id'] for snapshot in admin_snapshots] == [event_id]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_snapshots_page_serves_for_authenticated_user(tmp_path, monkeypatch):
    """GET /snapshots renders the page for any authenticated user."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        _login(admin)
        status, _headers, body = admin.request('/snapshots')
        assert status == 200
        assert 'Snapshots' in body
        assert 'snapshotGallery' in body
    finally:
        server.should_exit = True
        thread.join(timeout=5)
