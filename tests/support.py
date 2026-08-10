"""Shared pytest harness extracted from the former monolithic tests/test_api.py.

Holds the HTTP client (LocalClient), app-bootstrap (_load_app/_server), and
fixture helpers imported by the tests/test_api_*.py suites and by
test_middleware / test_web_auth_router_integration. Not collected by pytest
(no test_ prefix).
"""
from __future__ import annotations

import importlib

import json

import os

import shutil

import socket

import sqlite3

import subprocess

import contextlib

import sys

import threading

import time

from datetime import datetime, timedelta, timezone

from http.cookiejar import CookieJar

from pathlib import Path

from urllib.error import HTTPError

from urllib.parse import urlencode

from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

import pytest

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

class LocalClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    @staticmethod
    def header(headers: dict[str, str], name: str) -> str | None:
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def cookie(self, name: str) -> str | None:
        for cookie in self.cookies:
            if cookie.name == name:
                return cookie.value
        return None

    def request(
        self,
        path: str,
        method: str = "GET",
        form: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
        timeout: float = 5,
    ):
        request_data = data
        request_headers = dict(headers or {})
        if form is not None:
            request_data = urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if json_body is not None:
            request_data = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        # Simulate a real browser: send a same-origin ``Origin`` header so the
        # middleware's same-origin CSRF defence (which rejects mutating /api
        # requests that carry no Origin/Referer) sees a matching origin.
        request_headers.setdefault("Origin", self.base_url)
        opener = self.opener if follow_redirects else build_opener(HTTPCookieProcessor(self.cookies), NoRedirect)
        request = Request(f"{self.base_url}{path}", data=request_data, method=method, headers=request_headers)
        try:
            with opener.open(request, timeout=timeout) as response:  # noqa: S310 - local test server only
                return response.status, dict(response.headers), _body(response)
        except HTTPError as exc:
            return exc.code, dict(exc.headers), _error_body(exc)

class NoRedirect(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):  # noqa: ANN001
        fp.status = code
        fp.code = code
        fp.headers = headers
        return fp

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302

def _multipart_file(field_name: str, filename: str, content: bytes, content_type: str = 'application/octet-stream') -> tuple[bytes, str]:
    boundary = 'daygle-test-boundary'
    body = b''.join([
        f'--{boundary}\r\n'.encode('utf-8'),
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode('utf-8'),
        f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8'),
        content,
        f'\r\n--{boundary}--\r\n'.encode('utf-8'),
    ])
    return body, f'multipart/form-data; boundary={boundary}'

TEST_IMAGE_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04'
    b'\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'
)

def _post_frame_detection(client: LocalClient, csrf_token: str | None = None):
    headers = {'Content-Type': 'image/png'}
    if csrf_token:
        headers['X-CSRF-Token'] = csrf_token
    return client.request('/api/detect/frame', method='POST', data=TEST_IMAGE_PNG, headers=headers)

def _body(response):
    data = response.read()
    if "application/json" in response.headers.get("content-type", ""):
        return json.loads(data.decode("utf-8"))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

def _error_body(exc: HTTPError):
    text = exc.read().decode("utf-8")
    if "application/json" in exc.headers.get("content-type", ""):
        return json.loads(text)
    return text

def _load_app(tmp_path: Path, monkeypatch, extra_ai: str = ""):
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "data" / "daygle.sqlite3"
    config_path.write_text(
        f"""
server:
  host: 127.0.0.1
  port: 8080
auth:
  enabled: true
  session_timeout_hours: 12
  max_login_attempts: 5
  lockout_minutes: 15
  rate_limit_max_attempts: 10
ai:
  backend: onnx
  confidence: 0.45
{extra_ai}
live:
  # Integration tests feed a single detection frame and assert an event fires
  # immediately, which predates temporal confirmation. The product default is
  # now 2-of-3 (see DEFAULT_LIVE_CONFIG); pin single-frame here so these
  # plumbing tests stay deterministic. Confirmation itself is covered by
  # tests/test_detection_confirmation.py.
  detection_confirm_frames: 1
storage:
  data_dir: {tmp_path / 'data'}
  database: {database_path}
  snapshots_dir: {tmp_path / 'data' / 'snapshots'}
  events_dir: {tmp_path / 'data' / 'events'}
  recordings_dir: {tmp_path / 'data' / 'recordings'}
recording:
  enabled: true
  mode: motion
  continuous: false
  pre_event_seconds: 5
  post_event_seconds: 10
  max_clip_seconds: 60
  format: mp4
  retention_days: 14
  max_storage_gb: 20
  auto_purge_enabled: true
alerts:
  rules:
    - name: Cat alert
      object: cat
      min_confidence: 0.50
      cooldown_seconds: 0
      enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAYGLE_CONFIG", str(config_path))
    # Pop the ENTIRE app namespace from sys.modules so the next import
    # constructs a completely fresh application tree. Without this, parent
    # packages (``app``, ``app.api``) and sibling modules (``app.database``,
    # ``app.detector``) retain pointers to the PREVIOUS test's module-level
    # objects, producing cross-test state contamination (the e365ec5->Phase-2
    # lesson). Forward-compatible with future Phase-N routers / sub-modules.
    for mod in list(sys.modules.keys()):
        if mod == "app" or mod.startswith("app."):
            sys.modules.pop(mod, None)
    main_mod = importlib.import_module("app.main")
    main_mod._startup()
    return main_mod.app, database_path

def _server(app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, thread, f"http://127.0.0.1:{port}"

def _m():
    """Canonical module namespace - call after _load_app() only."""
    import types
    ns = types.SimpleNamespace()
    ns.state = sys.modules["app.state"]
    ns.live_snapshot = sys.modules["app.live_snapshot"]
    ns.detection_state = sys.modules["app.detection_state"]
    ns.event_debounce = sys.modules["app.event_debounce"]
    ns.alert_dispatch = sys.modules["app.alert_dispatch"]
    ns.detection_status = sys.modules["app.detection_status"]
    ns.recording_extension = sys.modules["app.recording_extension"]
    ns.live_monitor = sys.modules["app.live_monitor"]
    ns.utils = sys.modules["app.utils"]
    ns.camera_instance = sys.modules["app.camera_instance"]
    ns.media_utils = sys.modules["app.media_utils"]
    ns.model_management = sys.modules["app.model_management"]
    ns.backup = sys.modules["app.backup"]
    ns.sound_monitor = sys.modules["app.sound_monitor"]
    ns.zone_detection = sys.modules["app.zone_detection"]
    ns.camera_config = sys.modules["app.camera_config"]
    ns.zone_schema = sys.modules["app.zone_schema"]
    ns.payload_validators = sys.modules["app.payload_validators"]
    ns.camera_lifecycle = sys.modules["app.camera_lifecycle"]
    ns.camera_id = sys.modules["app.camera_id"]
    ns.recording_settings = sys.modules["app.recording_settings"]
    return ns

def _setup_admin(client: LocalClient, username: str = "admin", password: str = "Admin123!") -> None:
    status, _headers, body = client.request("/setup")
    assert status == 200
    assert "Create administrator" in body
    csrf = client.cookie("daygle_csrf")
    status, headers, _body_text = client.request(
        "/setup",
        method="POST",
        form={"username": username, "password": password, "confirm_password": password, "csrf_token": csrf or ""},
        follow_redirects=False,
    )
    assert status == 303
    assert LocalClient.header(headers, "Location") == "/login"

def _login(client: LocalClient, username: str = "admin", password: str = "Admin123!") -> str:
    status, _headers, _body_text = client.request("/login")
    assert status == 200
    csrf = client.cookie("daygle_csrf")
    status, headers, _body_text = client.request(
        "/login",
        method="POST",
        form={"username": username, "password": password, "csrf_token": csrf or ""},
        follow_redirects=False,
    )
    assert status == 303
    assert LocalClient.header(headers, "Location") == "/"
    assert client.cookie("daygle_session")
    status, _headers, me = client.request("/api/auth/me")
    assert status == 200
    return me["csrf_token"]

class _PreRollCaptureService:
    """Fake recording service that records the ``pre_seconds`` the capture thread
    hands to the prebuffer render, so tests can assert on the clamped pre-roll."""

    def __init__(self):
        self.captured = {}

    def prebuffer_window_seconds(self, recording_config=None):
        return 120

    def write_rtsp_clip_with_prebuffer(self, *, stream_url, camera_id, file_path,
                                       triggered_at, pre_seconds, post_seconds,
                                       max_duration_seconds, buffer_seconds=None):
        self.captured['pre_seconds'] = pre_seconds
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(file_path).write_text('clip', encoding='utf-8')
        content_start_ts = triggered_at.timestamp() - pre_seconds
        return content_start_ts, float(pre_seconds + post_seconds)

def _run_capture_with_previous_end(tmp_path, monkeypatch, *, camera_id, previous_gap_seconds,
                                   pre_event_seconds):
    """Drive ``start_rtsp_recording_capture`` for a camera whose previous clip ended
    ``previous_gap_seconds`` before the new trigger; return the pre-roll the render
    was asked to use."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    service = _PreRollCaptureService()
    monkeypatch.setattr(main._state, 'recording_service', service)
    main._state.active_rtsp_recordings.clear()
    main._state.last_rtsp_capture_end.clear()

    # Trigger in the past so the post-event wait resolves immediately.
    triggered_at = datetime.now(timezone.utc) - timedelta(seconds=100)
    main._state.last_rtsp_capture_end[camera_id] = triggered_at.timestamp() - previous_gap_seconds

    file_path = tmp_path / 'recordings' / f'event_{camera_id}.mp4'
    mods.recording_extension.start_rtsp_recording_capture(
        'rtsp://cam/stream',
        {'file_path': str(file_path), 'duration_seconds': 10, 'trigger_type': 'motion'},
        1,
        [{'label': 'person'}],
        recording_id=1,
        camera_id=camera_id,
        event_time=triggered_at.isoformat(),
        recording_config={'pre_event_seconds': pre_event_seconds, 'post_event_seconds': 5, 'max_clip_seconds': 60},
    )

    deadline = time.time() + 3
    while 'pre_seconds' not in service.captured and time.time() < deadline:
        time.sleep(0.02)
    main._state.active_rtsp_recordings.clear()
    main._state.last_rtsp_capture_end.clear()
    return service.captured.get('pre_seconds')

def _zone_camera_settings(zone_rules: list) -> dict:
    """Return minimal camera settings dict with a full-frame zone using the given rules."""
    return {
        'id': 'camera-1',
        'name': 'Front Door',
        'detection': {
            'zones': [
                {
                    'id': 'full-frame',
                    'name': 'Full Frame',
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'monitor_motion': False,
                    'monitor_objects': True,
                    'object_rules': zone_rules,
                },
            ],
        },
        'recording': {'continuous': False},
    }

def _email_alert_capture(main, monkeypatch):
    """Configure global SMTP settings and capture every message the mailer would deliver.

    Returns the list that receives one dict per delivered message ({'To', 'Subject', 'Body'}).
    SMTP transport is stubbed so no network connection is attempted.
    """
    main.database.set_setting(
        'alert_email',
        {
            'enabled': True,
            'host': 'smtp.example.com',
            'port': 587,
            'username': 'user',
            'password': 'secret',
            'from_address': 'camera@example.com',
            'use_tls': True,
            'use_ssl': False,
        },
        main.utc_now(),
    )
    sent: list[dict[str, str]] = []

    @staticmethod
    @contextlib.contextmanager
    def _create_smtp_session():
        yield "fake-smtp-session"

    def fake_deliver(self, message, **kwargs):
        sent.append({
            'To': message['To'],
            'Subject': message['Subject'],
        })

    @contextlib.contextmanager
    def _fake_create_smtp_session(self):
        yield 'fake-smtp-session'
    monkeypatch.setattr(main.EmailAlertService, '_create_smtp_session', _fake_create_smtp_session)
    monkeypatch.setattr(main.EmailAlertService, '_deliver', fake_deliver)
    return sent

def _zone_camera_settings_with_email(label: str):
    """Full-frame zone with a single alert+email rule for the given object label."""
    return _zone_camera_settings([
        {
            'label': label,
            'record_on_detect': True,
            'alert_on_detect': True,
            'min_confidence': 0.5,
            'cooldown_seconds': 0,
            'email_enabled': True,
            'email_recipients': ['glenbday82@gmail.com'],
        },
    ])

__all__ = [
    'CookieJar',
    'HTTPCookieProcessor',
    'HTTPError',
    'HTTPRedirectHandler',
    'LocalClient',
    'NoRedirect',
    'Path',
    'REPO_ROOT',
    'Request',
    'TEST_IMAGE_PNG',
    '_PreRollCaptureService',
    '_body',
    '_email_alert_capture',
    '_error_body',
    '_free_port',
    '_load_app',
    '_login',
    '_m',
    '_multipart_file',
    '_post_frame_detection',
    '_run_capture_with_previous_end',
    '_server',
    '_setup_admin',
    '_zone_camera_settings',
    '_zone_camera_settings_with_email',
    'build_opener',
    'contextlib',
    'datetime',
    'importlib',
    'json',
    'os',
    'pytest',
    'shutil',
    'socket',
    'sqlite3',
    'subprocess',
    'sys',
    'threading',
    'time',
    'timedelta',
    'timezone',
    'urlencode',
    'uvicorn',
]
