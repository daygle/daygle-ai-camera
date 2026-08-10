"""API integration tests: setup/login/users/profile, system & camera settings, backup/restore, and audit log.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_favicon_is_served_publicly(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        status, headers, body = client.request("/favicon.ico")
        assert status == 200
        assert "image/svg+xml" in (LocalClient.header(headers, "Content-Type") or "")
        assert "<svg" in body
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_recording_playback_page_route(tmp_path, monkeypatch):
    """/recordings/{id} serves the recordings page with inline player, without shadowing the
    literal /recordings/timeline route or matching non-numeric ids."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        # A numeric id serves the recordings page (inline player).
        status, _headers, body = client.request('/recordings/482')
        assert status == 200
        assert 'id="clipPlayer"' in body
        assert '<title>Recordings - Daygle AI Camera</title>' in body
        # The literal /recordings/timeline route still wins (int converter +
        # declaration order), serving the timeline page rather than the recordings page.
        status, _headers, timeline_body = client.request('/recordings/timeline')
        assert status == 200
        assert '<title>Timeline - Daygle AI Camera</title>' in timeline_body
        assert '<title>Recordings - Daygle AI Camera</title>' not in timeline_body
        # A non-numeric id must not match the playback route.
        status, _headers, _ = client.request('/recordings/not-a-number', follow_redirects=False)
        assert status == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_setup_login_success_session_validation_and_protected_routes(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        status, headers, _body = client.request("/favicon.ico")
        assert status == 200
        assert "image/svg+xml" in (LocalClient.header(headers, "Content-Type") or "")

        status, headers, _body_text = client.request("/", follow_redirects=False)
        assert status == 303
        assert LocalClient.header(headers, "Location") == "/setup"

        _setup_admin(client)

        status, headers, _body_text = client.request("/setup", follow_redirects=False)
        assert status == 303
        assert LocalClient.header(headers, "Location") == "/login"

        anonymous = LocalClient(base_url)
        status, headers, _body_text = anonymous.request("/", follow_redirects=False)
        assert status == 303
        assert LocalClient.header(headers, "Location") == "/login"
        status, _headers, _body_json = anonymous.request("/api/status")
        assert status == 401

        csrf = _login(client)
        status, _headers, root = client.request("/")
        assert status == 200
        assert "Dashboard" in root

        status, _headers, payload = client.request("/api/status")
        assert status == 200
        assert payload["status"] == "online"

        status, _headers, _frame_blocked = _post_frame_detection(client)
        assert status == 403
        status, _headers, frame_payload = _post_frame_detection(client, csrf)
        assert status == 200
        assert isinstance(frame_payload["detections"], list)
        assert frame_payload["count"] == len(frame_payload["detections"])

        assert client.request("/api/events")[0] == 200
        assert client.request("/api/alerts")[0] == 200
        assert client.request("/api/stats")[2]["total_events"] == 0
        assert client.request("/api/config")[2]["auth"]["enabled"] is True
        assert client.request("/static/app.js")[0] == 200

        with sqlite3.connect(database_path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"users", "user_sessions", "login_attempts", "app_settings"}.issubset(tables)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_login_failure_and_account_lockout(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        for _ in range(5):
            client.request("/login")
            csrf = client.cookie("daygle_csrf")
            status, _headers, body = client.request("/login", method="POST", form={"username": "admin", "password": "wrong", "csrf_token": csrf or ""})
            assert status == 200
            assert "Invalid username or password" in body

        client.request("/login")
        csrf = client.cookie("daygle_csrf")
        status, _headers, body = client.request("/login", method="POST", form={"username": "admin", "password": "Admin123!", "csrf_token": csrf or ""})
        assert status == 200
        assert "temporarily locked" in body
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_logout_user_creation_and_password_reset(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, viewer = client.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert viewer["role"] == "viewer"

        status, _headers, updated = client.request(
            f"/api/users/{viewer['id']}",
            method="PATCH",
            json_body={
                "username": "viewer-renamed",
                "first_name": "View",
                "last_name": "Er",
                "email": "viewer@example.com",
                "role": "viewer",
                "is_active": True,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert updated["username"] == "viewer-renamed"
        assert updated["first_name"] == "View"
        assert updated["last_name"] == "Er"
        assert updated["email"] == "viewer@example.com"
        assert updated["role"] == "viewer"

        status, _headers, invalid_update = client.request(
            f"/api/users/{viewer['id']}",
            method="PATCH",
            json_body={"is_active": "false"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "Account status must be true or false" in invalid_update["detail"]

        status, _headers, updated = client.request(
            f"/api/users/{viewer['id']}",
            method="PATCH",
            json_body={"password": "Viewer456!", "role": "viewer", "is_active": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert updated["role"] == "viewer"

        status, _headers, payload = client.request("/logout", method="POST", headers={"X-CSRF-Token": csrf})
        assert status == 200
        assert payload["ok"] is True
        assert client.request("/api/status")[0] == 401

        viewer_client = LocalClient(base_url)
        _login(viewer_client, "viewer-renamed", "Viewer456!")
        assert viewer_client.request("/api/status")[0] == 200
        assert viewer_client.request("/api/users")[0] == 403

        assert viewer_client.request("/api/config")[2]["ai"]["backend"] == "onnx"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_user_account_name_email_fields(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        # Create user with name/email fields
        status, _headers, user = client.request(
            "/api/users",
            method="POST",
            json_body={"username": "named", "password": "Named123!", "role": "viewer", "first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert user["first_name"] == "Jane"
        assert user["last_name"] == "Doe"
        assert user["email"] == "jane@example.com"

        # Create user with null name fields (must not 500)
        status, _headers, user2 = client.request(
            "/api/users",
            method="POST",
            json_body={"username": "nullfields", "password": "Null1234!", "role": "viewer", "first_name": None, "last_name": None, "email": None},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert user2["first_name"] == ""
        assert user2["last_name"] == ""
        assert user2["email"] == ""

        # Update profile name/email and verify /api/auth/me returns them (not blank).
        # Changing the email is a sensitive field change, so the H4 guard requires
        # the current password as proof of possession.
        named_client = LocalClient(base_url)
        named_csrf = _login(named_client, "named", "Named123!")
        status, _headers, updated = named_client.request(
            "/api/profile",
            method="PUT",
            json_body={"username": "named", "first_name": "Janet", "last_name": "Smith", "email": "janet@example.com", "timezone": "UTC", "date_format": "iso", "time_format": "24h", "current_password": "Named123!"},
            headers={"X-CSRF-Token": named_csrf},
        )
        assert status == 200
        assert updated["first_name"] == "Janet"
        assert updated["email"] == "janet@example.com"

        # /api/auth/me must return updated fields so the profile form pre-fills correctly
        status, _headers, me = named_client.request("/api/auth/me")
        assert status == 200
        assert me["user"]["first_name"] == "Janet"
        assert me["user"]["last_name"] == "Smith"
        assert me["user"]["email"] == "janet@example.com"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_admin_ai_settings_viewer_denied_and_db_override(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        csrf = _login(admin)
        status, _headers, settings = admin.request(
            "/api/settings/ai",
            method="PUT",
            json_body={"backend": "onnx", "confidence": 0.72, "iou_threshold": 0.33, "input_size": 320},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert settings["confidence"] == 0.72
        config_payload = admin.request("/api/config")[2]
        assert config_payload["ai"]["confidence"] == 0.72
        assert admin.request("/api/status/ai")[2]["active_config_source"] == "database"
        with sqlite3.connect(database_path) as db:
            value = db.execute("SELECT value FROM app_settings WHERE key = 'ai'").fetchone()[0]
        assert json.loads(value)["confidence"] == 0.72

        status, _headers, viewer = admin.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer2", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, viewer["username"], "Viewer123!")
        assert viewer_client.request("/api/settings/alert-email")[0] == 200
        status, _headers, body = viewer_client.request(
            "/api/settings/ai",
            method="PUT",
            json_body={"confidence": 0.2},
            headers={"X-CSRF-Token": viewer_csrf},
        )
        assert status == 403
        assert body["detail"] == "Admin access required"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_profile_update_and_password_change(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, profile = client.request(
            "/api/profile",
            method="PUT",
            json_body={"timezone": "UTC", "date_format": "iso", "time_format": "24h"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert profile["timezone"] == "UTC"
        assert profile["date_format"] == "iso"

        status, _headers, changed = client.request(
            "/api/profile/password",
            method="POST",
            json_body={"current_password": "Admin123!", "new_password": "Admin456!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert changed["ok"] is True

        client.request("/logout", method="POST", headers={"X-CSRF-Token": csrf})
        new_client = LocalClient(base_url)
        _login(new_client, "admin", "Admin456!")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_system_settings_are_editable_from_api(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, camera = client.request(
            "/api/cameras/camera-1",
            method="PUT",
            json_body={"backend": "rtsp", "width": 640, "height": 360, "fps": 12, "device": "rtsp", "flip": "none", "stream_url": "rtsp://127.0.0.1:554/stream1"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert camera["width"] == 640
        assert client.request("/api/status")[2]["resolution"] == {"width": 640, "height": 360}

        status, _headers, recording = client.request(
            "/api/settings/system/recording",
            method="PUT",
            json_body={
                "pre_event_seconds": 2,
                "post_event_seconds": 3,
                "max_clip_seconds": 10,
                "format": "mp4",
                "retention_days": 7,
                "max_storage_gb": 5,
                "auto_purge_enabled": True,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert recording["pre_event_seconds"] == 2
        # Global enabled/mode/continuous were removed; only clip mechanics remain.
        assert "mode" not in recording
        assert "enabled" not in recording

        status, _headers, storage = client.request(
            "/api/settings/system/storage",
            method="PUT",
            json_body={"data_dir": str(tmp_path / "runtime-data"), "snapshots_dir": str(tmp_path / "runtime-snaps"), "events_dir": str(tmp_path / "runtime-events"), "recordings_dir": str(tmp_path / "runtime-recordings")},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert storage["database"]
        assert Path(storage["snapshots_dir"]).exists()

        status, _headers, auth_settings = client.request(
            "/api/settings/system/auth",
            method="PUT",
            json_body={"session_timeout_hours": 6, "max_login_attempts": 4, "lockout_minutes": 10},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert auth_settings["max_login_attempts"] == 4

        system_settings = client.request("/api/settings/system")[2]
        assert system_settings["camera"]["width"] == 640
        assert system_settings["recording"]["format"] == "mp4"
        assert system_settings["auth"]["lockout_minutes"] == 10
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_runtime_data_reset_clears_operational_data_but_keeps_settings(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, updated_recording = client.request(
            '/api/settings/system/recording',
            method='PUT',
            json_body={
                'pre_event_seconds': 5,
                'post_event_seconds': 10,
                'max_clip_seconds': 60,
                'format': 'mp4',
                'retention_days': 21,
                'max_storage_gb': 8,
                'auto_purge_enabled': True,
            },
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert updated_recording['retention_days'] == 21

        import app.main as main_module

        file_path = tmp_path / 'data' / 'recordings' / 'reset-test.mp4'
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b'not-a-real-video')

        event_id = main_module.database.add_event(
            created_at='2026-06-07T00:00:00+00:00',
            source='camera',
            snapshot_path=None,
            detections=[{'label': 'dog', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}],
            metadata={'camera_id': 'camera-1', 'camera_name': 'Primary Camera'},
        )
        main_module.database.add_recording(
            event_id=event_id,
            camera_id='camera-1',
            started_at='2026-06-07T00:00:00+00:00',
            ended_at='2026-06-07T00:00:10+00:00',
            duration_seconds=10.0,
            file_path=str(file_path),
            thumbnail_path=None,
            source='camera',
            created_at='2026-06-07T00:00:00+00:00',
            trigger_type='alert',
            trigger_label='dog',
        )
        main_module.database.add_alert(
            created_at='2026-06-07T00:00:01+00:00',
            rule_name='Dog alert',
            event_id=event_id,
            label='dog',
            confidence=0.9,
            message='Alert triggered: dog detected',
        )

        # Runtime-data wipe is a two-step confirm flow (M2): POST /preview to
        # obtain a single-use confirm token, then DELETE with ?confirm=true and
        # the token echoed back in the X-Runtime-Data-Confirm header.
        status, _headers, preview_payload = client.request(
            '/api/system/runtime-data/preview',
            method='POST',
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200, (status, preview_payload)
        confirm_token = preview_payload['confirm_token']
        status, _headers, reset_payload = client.request(
            '/api/system/runtime-data?confirm=true',
            method='DELETE',
            headers={'X-CSRF-Token': csrf, 'X-Runtime-Data-Confirm': confirm_token},
        )
        assert status == 200
        assert reset_payload['deleted']['events'] >= 1
        assert reset_payload['deleted']['recordings'] >= 1
        assert reset_payload['deleted']['alerts'] >= 1
        assert reset_payload['deleted']['objects'] >= 1

        assert client.request('/api/events')[2] == []
        assert client.request('/api/recordings')[2] == []
        assert client.request('/api/alerts')[2] == []
        assert client.request('/api/stats')[2]['objects'] == []

        status, _headers, settings_payload = client.request('/api/settings/system')
        assert status == 200
        assert settings_payload['recording']['retention_days'] == 21
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_onvif_camera_settings_build_rtsp_url(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    settings = mods.payload_validators.validate_camera_settings({
        'backend': 'onvif',
        'host': '192.168.1.50',
        'port': 554,
        'path': '/stream1',
        'username': 'daygle user',
        'password': 'pa:ss',
        'width': 1920,
        'height': 1080,
        'fps': 15,
        'flip': 'none',
    })

    assert settings['backend'] == 'onvif'
    assert mods.utils.build_stream_url(settings) == 'rtsp://daygle%20user:pa%3Ass@192.168.1.50:554/stream1'
    camera = mods.camera_instance.create_camera(settings)
    assert camera.stream_url == 'rtsp://daygle%20user:pa%3Ass@192.168.1.50:554/stream1'


def test_onvif_stream_url_uses_form_credentials_when_url_is_bare(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    settings = mods.payload_validators.validate_camera_settings({
        'backend': 'onvif',
        'stream_url': 'rtsp://192.168.40.103:554/live/0/MAIN',
        'username': 'admin',
        'password': 'pa:ss',
        'width': 1280,
        'height': 720,
        'fps': 15,
        'flip': 'none',
    })

    assert mods.utils.build_stream_url(settings) == 'rtsp://admin:pa%3Ass@192.168.40.103:554/live/0/MAIN'


def test_opencv_stream_camera_reuses_rtsp_capture(monkeypatch):
    from app.camera_backend import OpenCvStreamCamera

    class FakeImage:
        shape = (720, 1280, 3)

    class FakeEncoded:
        def tobytes(self):
            return b'jpeg'

    class FakeCapture:
        instances = []

        def __init__(self, stream_url):
            self.stream_url = stream_url
            self.buffer_size = None
            self.grab_count = 0
            self.release_count = 0
            FakeCapture.instances.append(self)

        def set(self, prop, value):
            self.buffer_size = (prop, value)

        def isOpened(self):
            return True

        def grab(self):
            self.grab_count += 1
            return True

        def read(self):
            return True, FakeImage()

        def retrieve(self):
            return True, FakeImage()

        def release(self):
            self.release_count += 1

    class FakeCv2:
        CAP_PROP_BUFFERSIZE = 38

        @staticmethod
        def VideoCapture(stream_url):
            return FakeCapture(stream_url)

        @staticmethod
        def imencode(_extension, _image):
            return True, FakeEncoded()

    monkeypatch.setitem(sys.modules, 'cv2', FakeCv2)
    monkeypatch.delenv('OPENCV_FFMPEG_CAPTURE_OPTIONS', raising=False)

    camera = OpenCvStreamCamera('rtsp://admin:password@192.168.40.103:554/live/0/MAIN')
    first_jpeg, first_frame = camera.read_jpeg()
    second_jpeg, second_frame = camera.read_jpeg()

    assert first_jpeg == b'jpeg'
    assert second_jpeg == b'jpeg'
    assert first_frame['frame_number'] == 1
    assert second_frame['frame_number'] == 2
    assert len(FakeCapture.instances) == 1
    assert FakeCapture.instances[0].buffer_size == (FakeCv2.CAP_PROP_BUFFERSIZE, 1)
    assert camera._stale_frame_grabs() == 3
    # The drain is adaptive: it discards at least _stale_frame_grabs() frames
    # per read and keeps draining while grabs return instantly (as the fake
    # always does), so the count is at least 3 per read rather than exactly 3.
    assert FakeCapture.instances[0].grab_count >= 6
    assert FakeCapture.instances[0].release_count == 0
    assert 'rtsp_transport;tcp' in os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']
    assert 'fflags;discardcorrupt' in os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']


def test_opencv_stream_camera_applies_ffmpeg_log_level_after_each_videocapture(monkeypatch):
    """_configure_ffmpeg_log_level must be called after every VideoCapture
    construction - including on reconnect - so FFmpeg's own init cannot reset
    the quiet level back to a noisy default.

    Background monitor threads from earlier tests in the suite keep polling
    cameras (``live_alert_monitor_loop``) while this test runs, and those polls
    construct ``VideoCapture`` through the same fake ``cv2`` - the shared
    ``instances`` list is therefore cross-thread state and cannot be used for
    exact-count assertions. Reconnect behaviour is keyed by stream URL so other
    cameras cannot flip this camera's first-read failure, and constructions /
    ``_configure_ffmpeg_log_level`` calls are paired only on the test thread.
    """
    import threading

    import app.camera_backend as camera_backend

    main_tid = threading.get_ident()
    constructs: list[int] = []       # len(FakeCapture.instances) per test-thread construction
    configure_snaps: list[int] = []  # len(FakeCapture.instances) per test-thread configure call

    class FakeImage:
        shape = (720, 1280, 3)

    class FakeEncoded:
        def tobytes(self):
            return b'jpeg'

    class FakeCapture:
        instances: list = []
        failed_urls: set = set()

        def __init__(self, stream_url):
            FakeCapture.instances.append(self)
            self._stream_url = stream_url
            self._reads = 0
            if threading.get_ident() == main_tid:
                constructs.append(len(FakeCapture.instances))

        def set(self, _prop, _value):
            pass

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            self._reads += 1
            # Each camera's first capture fails its first reads to trigger a
            # reconnect; later captures for the same URL succeed. Keying on the
            # URL isolates this camera from background monitors' captures.
            if self._stream_url not in FakeCapture.failed_urls and self._reads <= 2:
                if self._reads == 2:
                    FakeCapture.failed_urls.add(self._stream_url)
                return False, None
            return True, FakeImage()

        def retrieve(self):
            return self.read()

        def release(self):
            pass

    class FakeCv2:
        CAP_PROP_BUFFERSIZE = 38

        @staticmethod
        def VideoCapture(stream_url):
            return FakeCapture(stream_url)

        @staticmethod
        def imencode(_ext, _img):
            return True, FakeEncoded()

    monkeypatch.setitem(sys.modules, 'cv2', FakeCv2)
    monkeypatch.delenv('OPENCV_FFMPEG_CAPTURE_OPTIONS', raising=False)

    def trace_configure():
        if threading.get_ident() == main_tid:
            configure_snaps.append(len(FakeCapture.instances))

    monkeypatch.setattr(camera_backend, '_configure_ffmpeg_log_level', trace_configure)

    camera = camera_backend.OpenCvStreamCamera('rtsp://example/stream')
    FakeCapture.instances.clear()
    camera.read_jpeg()

    # The fake fails this camera's first capture, so read_jpeg must have
    # reconnected at least once (two test-thread constructions). Background
    # monitor threads may construct extra captures through the same fake, so
    # only the test thread's own constructions are counted here.
    assert len(constructs) >= 2, "expected a reconnect to create another VideoCapture"
    # _configure_ffmpeg_log_level must have been called exactly once per
    # VideoCapture construction, on the thread that built it.
    assert len(configure_snaps) == len(constructs), (
        f"expected one log-level call per VideoCapture, got {len(configure_snaps)} "
        f"calls for {len(constructs)} constructions"
    )
    # Each call must have happened *after* its own VideoCapture was built: the
    # instance count it observes is at least the count at construction time
    # (background threads can only raise it further).
    assert all(snap >= cnt for snap, cnt in zip(configure_snaps, constructs)), (
        "each _configure_ffmpeg_log_level call must run after its own VideoCapture "
        f"was built (constructs={constructs}, configures={configure_snaps})"
    )


def test_onvif_camera_settings_require_stream_source(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    try:
        mods.payload_validators.validate_camera_settings({'backend': 'onvif', 'width': 640, 'height': 480, 'fps': 10, 'flip': 'none'})
    except Exception as exc:  # FastAPI raises HTTPException here.
        assert getattr(exc, 'status_code', None) == 400
        assert 'stream_url is required' in str(getattr(exc, 'detail', ''))
    else:
        raise AssertionError('Expected ONVIF camera validation to require a stream URL or host')


def test_admin_can_backup_and_restore_database_from_api(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, camera = client.request(
            '/api/cameras/camera-1',
            method='PUT',
            json_body={'backend': 'rtsp', 'width': 640, 'height': 360, 'fps': 12, 'device': 'rtsp', 'flip': 'none', 'stream_url': 'rtsp://127.0.0.1:554/stream1'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert camera['width'] == 640

        status, headers, backup_bytes = client.request('/api/settings/system/database/backup')
        assert status == 200
        assert isinstance(backup_bytes, bytes)
        assert 'daygle-database-' in (LocalClient.header(headers, 'content-disposition') or '')
        with sqlite3.connect(database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0] == 1

        # The server-side snapshot is deleted once the download completes
        # (background task), so poll briefly rather than asserting instantly.
        backups_dir = database_path.parent / 'backups'
        deadline = time.time() + 5
        while time.time() < deadline and list(backups_dir.glob('daygle-database-*.sqlite3')):
            time.sleep(0.05)
        assert list(backups_dir.glob('daygle-database-*.sqlite3')) == []

        status, _headers, camera = client.request(
            '/api/cameras/camera-1',
            method='PUT',
            json_body={'backend': 'rtsp', 'width': 800, 'height': 450, 'fps': 20, 'device': 'rtsp', 'flip': 'none', 'stream_url': 'rtsp://127.0.0.1:554/stream1'},
            headers={'X-CSRF-Token': csrf},
        )
        assert status == 200
        assert camera['width'] == 800

        multipart_body, content_type = _multipart_file('file', 'backup.sqlite3', backup_bytes, 'application/vnd.sqlite3')
        status, _headers, restored = client.request(
            '/api/settings/system/database/restore',
            method='POST',
            data=multipart_body,
            headers={'Content-Type': content_type, 'X-CSRF-Token': csrf},
            timeout=15,
        )
        assert status == 200
        assert restored['ok'] is True
        assert Path(restored['safety_backup']).exists()

        system_settings = client.request('/api/settings/system')[2]
        assert system_settings['camera']['width'] == 640
        assert client.request('/api/status')[2]['resolution'] == {'width': 640, 'height': 360}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_admin_can_download_full_backup_from_api(tmp_path, monkeypatch):
    """The full backup endpoint returns a zip containing the database snapshot,
    the recordings directory, and the snapshots directory - and the generated
    archive is cleaned up after the download completes."""
    import io
    import zipfile

    app, database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)

        clip = main.storage.recordings_dir / 'clip.mp4'
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b'video-bytes')
        main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'test.png')

        status, _headers, body = client.request(
            '/api/settings/system/database/backup/full', timeout=30,
        )
        assert status == 200, 'full backup endpoint must be admin-gated and reachable'
        assert isinstance(body, bytes)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = set(archive.namelist())
            assert 'manifest.json' in names
            assert 'recordings/clip.mp4' in names
            assert any(n.startswith('database/') and n.endswith('.sqlite3') for n in names)

        # The server-side archive is deleted once the download completes.
        backups_dir = Path(database_path).parent / 'backups'
        deadline = time.time() + 10
        while time.time() < deadline and list(backups_dir.glob('daygle-full-*.zip')):
            time.sleep(0.05)
        assert list(backups_dir.glob('daygle-full-*.zip')) == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_audit_log_admin_access_and_viewer_denied(tmp_path, monkeypatch):
    app, _ = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    viewer = LocalClient(base_url)
    unauth = LocalClient(base_url)
    try:
        _setup_admin(admin)
        csrf = _login(admin)

        # Admin: login events should appear in the audit log
        status, _, payload = admin.request("/api/audit")
        assert status == 200
        assert "entries" in payload
        assert "total" in payload
        assert payload["total"] >= 1
        assert any(e["action"] == "login" for e in payload["entries"])

        # Pagination: limit and offset
        status, _, page = admin.request("/api/audit?limit=1&offset=0")
        assert status == 200
        assert len(page["entries"]) == 1

        # Filter by action
        status, _, filtered = admin.request("/api/audit?action=login")
        assert status == 200
        assert all(e["action"] == "login" for e in filtered["entries"])

        # Filter by username
        status, _, by_user = admin.request("/api/audit?username=admin")
        assert status == 200
        assert all(e["username"] == "admin" for e in by_user["entries"])

        # Viewer is denied (create one first via admin)
        admin.request("/api/users", method="POST", json_body={"username": "viewer1", "password": "Viewer123!", "role": "viewer"}, headers={"X-CSRF-Token": csrf})
        _login(viewer, username="viewer1", password="Viewer123!")
        status, _, _ = viewer.request("/api/audit")
        assert status == 403

        # Unauthenticated is denied
        status, _, _ = unauth.request("/api/audit")
        assert status == 401
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_audit_log_records_admin_actions(tmp_path, monkeypatch):
    app, _ = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        csrf = _login(admin)

        # Create a user -> should record a 'create' / 'user' entry
        admin.request("/api/users", method="POST", json_body={"username": "newuser", "password": "NewUser1!", "role": "viewer"}, headers={"X-CSRF-Token": csrf})

        status, _, payload = admin.request("/api/audit?action=create&resource=user")
        assert status == 200
        assert payload["total"] >= 1
        entry = payload["entries"][0]
        assert entry["action"] == "create"
        assert entry["resource"] == "user"
        assert entry["username"] == "admin"
        assert entry["status"] == "success"
        assert entry.get("details", {}).get("username") == "newuser"

        # Failed login -> should record a 'login' / 'failed' entry
        bad = LocalClient(base_url)
        bad.request("/login")
        bad_csrf = bad.cookie("daygle_csrf") or ""
        bad.request("/login", method="POST", form={"username": "admin", "password": "wrong", "csrf_token": bad_csrf}, follow_redirects=False)

        status, _, logins = admin.request("/api/audit?action=login")
        assert status == 200
        failed = [e for e in logins["entries"] if e["status"] == "failed"]
        assert len(failed) >= 1
        assert failed[0]["username"] == "admin"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_audit_log_api(tmp_path, monkeypatch):
    """GET /api/audit returns audit log entries with pagination and filtering."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, _body = client.request(
            "/api/settings/alert-email",
            method="PUT",
            json_body={"enabled": False, "host": "audit-test.example.com", "port": 587, "from_address": "audit@example.com"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        status, _headers, audit = client.request("/api/audit")
        assert status == 200
        assert "entries" in audit
        assert "total" in audit
        assert "limit" in audit
        assert "offset" in audit
        assert len(audit["entries"]) >= 1
        actions = [entry["action"] for entry in audit["entries"]]
        assert "update" in actions
        status, _headers, limited = client.request("/api/audit?limit=1")
        assert status == 200
        assert len(limited["entries"]) <= 1
        assert limited["limit"] == 1
        status, _headers, filtered = client.request("/api/audit?action=update")
        assert status == 200
        assert all(entry["action"] == "update" for entry in filtered["entries"])
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_system_live_settings_update(tmp_path, monkeypatch):
    """PUT /api/settings/system/live updates live stream settings."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, updated = client.request(
            "/api/settings/system/live",
            method="PUT",
            json_body={
                "snapshot_refresh_ms": 300,
                "detection_status_refresh_ms": 3000,
                "background_detection_enabled": False,
                "detection_interval_seconds": 1.0,
                "event_debounce_seconds": 15.0,
                "detection_history_minutes": 5,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 200
        assert updated["snapshot_refresh_ms"] == 300
        assert updated["detection_status_refresh_ms"] == 3000
        assert updated["background_detection_enabled"] is False
        assert updated["detection_interval_seconds"] == 1.0
        assert updated["event_debounce_seconds"] == 15.0
        assert updated["detection_history_minutes"] == 5
        status, _headers, system = client.request("/api/settings/system")
        assert status == 200
        assert system["live"]["detection_interval_seconds"] == 1.0
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_system_resources_endpoint(tmp_path, monkeypatch):
    """/api/system/resources returns CPU/load/RAM and is admin-gated."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)

        status, _headers, payload = client.request("/api/system/resources")
        assert status == 200
        assert set(payload) == {"cpu_percent", "cpu_count", "load_average", "memory"}
        # On the Linux CI host these are populated; values are best-effort so
        # only assert the shape, not exact numbers.
        assert payload["cpu_count"] is None or payload["cpu_count"] >= 1
        mem = payload["memory"]
        if mem is not None:
            assert mem["used"] <= mem["total"]
            assert 0 <= mem["percent"] <= 100

        # Anonymous callers are rejected before reaching the handler.
        anon = LocalClient(base_url)
        assert anon.request("/api/system/resources")[0] == 401

        # Viewers (non-admin) are forbidden from the host-metrics endpoint.
        client.request(
            "/api/users",
            method="POST",
            json_body={"username": "viewer", "password": "Viewer123!", "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        viewer_client = LocalClient(base_url)
        _login(viewer_client, "viewer", "Viewer123!")
        assert viewer_client.request("/api/system/resources")[0] == 403
    finally:
        server.should_exit = True
        thread.join(timeout=5)
