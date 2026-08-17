"""API integration tests: per-label still/moving object settings (Objects page).

Covers the ``/api/settings/objects`` GET/PUT endpoints (admin gating,
validation, persistence) and the ``/objects`` page route.
"""
from __future__ import annotations

import json
import sqlite3

from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_objects_settings_defaults_and_page_route(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)

        status, _headers, payload = client.request("/api/settings/objects")
        assert status == 200
        assert payload["default_mode"] == "moving"
        assert payload["labels"] == {}
        assert payload["still_alerts"] == {}
        assert "available_labels" in payload

        # The dedicated page is served to admins.
        status, _headers, page = client.request("/objects")
        assert status == 200
        assert "<title>Objects - Daygle AI Camera</title>" in page
        assert "Per-Object Overrides" in page
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_objects_settings_save_round_trip(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, updated = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "moving", "labels": {"car": "still", "person": "moving"}},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert updated["default_mode"] == "moving"
        # ``person: moving`` equals the global default, so it is collapsed away.
        assert updated["labels"] == {"car": "still"}

        # Persisted to the database (still_alerts always present, empty by default).
        with sqlite3.connect(database_path) as db:
            row = db.execute("SELECT value FROM app_settings WHERE key = 'objects'").fetchone()
        assert row is not None
        stored = json.loads(row[0])
        assert stored == {"default_mode": "moving", "labels": {"car": "still"}, "group_modes": {}, "still_alerts": {}}

        # GET returns what was saved.
        status, _headers, payload = client.request("/api/settings/objects")
        assert status == 200
        assert payload["default_mode"] == "moving"
        assert payload["labels"] == {"car": "still"}
        assert payload["still_alerts"] == {}

        # Still-dwell thresholds round-trip: numeric minutes are persisted,
        # while 0 (off) and sub-floor values are dropped.
        status, _headers, updated = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={
                "default_mode": "any",
                "labels": {},
                "still_alerts": {"package": 10, "person": 0, "cat": 0.5},
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert updated["still_alerts"] == {"package": 10}
        status, _headers, payload = client.request("/api/settings/objects")
        assert status == 200
        assert payload["still_alerts"] == {"package": 10}

        # An override equal to the default is collapsed away, and invalid modes
        # never reach the database.
        status, _headers, updated = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "still", "labels": {"car": "still", "person": "moving", "bird": "bogus"}},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "must be 'any', 'moving', or 'still'" in updated["detail"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_objects_settings_group_modes_round_trip(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, updated = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "moving", "group_modes": {"animal": "still", "pet": "moving"}},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        # pet: moving equals the global default, so it is collapsed away.
        assert updated["group_modes"] == {"animal": "still"}

        status, _headers, payload = client.request("/api/settings/objects")
        assert status == 200
        assert payload["group_modes"] == {"animal": "still"}

        # An invalid group mode is rejected.
        status, _headers, body = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "moving", "group_modes": {"animal": "bogus"}},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "group_modes" in body["detail"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_objects_settings_validation(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, body = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "sometimes"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "default_mode must be 'any', 'moving', or 'still'" in body["detail"]

        status, _headers, body = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "any", "labels": "not-a-dict"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "labels must be an object" in body["detail"]

        status, _headers, body = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "any", "labels": {"person": 42}},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 400

        status, _headers, body = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "any", "still_alerts": "not-a-dict"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "still_alerts must be an object" in body["detail"]

        status, _headers, body = client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "any", "still_alerts": {"package": -5}},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "still_alerts" in body["detail"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_objects_settings_viewer_denied(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        csrf = _login(admin)
        status, _headers, viewer = admin.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200

        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, viewer["username"], "Viewer123!")
        assert viewer_client.request("/api/settings/objects")[0] == 403
        status, _headers, body = viewer_client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "moving"},
            headers={"X-CSRF-Token": viewer_csrf},
        )
        assert status == 403
        assert body["detail"] == "Admin access required"

        # The page itself is admin-gated too.
        status, _headers, _page = viewer_client.request("/objects", follow_redirects=False)
        assert status == 403
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_objects_settings_audit_logged(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        client.request(
            "/api/settings/objects",
            method="PUT",
            json_body={"default_mode": "moving", "labels": {"car": "still"}},
            headers={"X-CSRF-Token": csrf},
        )
        status, _headers, audit = client.request("/api/audit?action=update&resource=settings.objects")
        assert status == 200
        assert audit["total"] >= 1
        assert audit["entries"][0]["username"] == "admin"
        assert audit["entries"][0]["details"]["default_mode"] == "moving"
        assert "car" in audit["entries"][0]["details"]["labels"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
