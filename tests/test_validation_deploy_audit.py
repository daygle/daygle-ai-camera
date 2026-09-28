"""Regression tests for the input-validation and deployment audit fixes.

* Settings bodies are parsed strictly: ``NaN`` / ``Infinity`` are rejected, as
  are non-object bodies, instead of crashing a validator (500) or persisting.
* A malformed face-rules body can no longer silently delete every rule.
* Camera ``stream_url`` must use a network scheme (ffmpeg must never be given
  ``file:``, ``concat:``, ...) and ``host`` cannot rewrite the RTSP URL; the
  connection test applies the same rules.
* Media directories can no longer point at the data root or the application
  tree, and the runtime-data wipe refuses such a directory regardless.
* Auth settings reject NaN and a max rate-limit delay below the base delay
  (either bricked startup), and CIDR ``trusted_proxies`` entries now match.
* A non-list zone ``points`` value no longer crashes camera validation.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin


def _put(client, csrf, path, raw: bytes):
    return client.request(
        path, method='PUT', data=raw,
        headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    )


def test_settings_reject_non_finite_numbers_and_non_object_bodies(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        for path, body in (
            ('/api/settings/system/live', b'{"detection_interval_seconds": NaN}'),
            ('/api/settings/system/recording', b'{"retention_days": Infinity}'),
            ('/api/settings/system/auth', b'{"session_timeout_hours": NaN}'),
            ('/api/settings/ai', b'{"input_size": Infinity}'),
            ('/api/settings/system/live', b'[]'),
            ('/api/settings/system/storage', b'5'),
            ('/api/settings/alert-email', b'null'),
            ('/api/cameras', b'{"cameras": [{"fps": -Infinity}]}'),
        ):
            status, _h, response = _put(client, csrf, path, body)
            assert status == 400, (path, body, status, response)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_malformed_face_rules_body_does_not_wipe_rules(tmp_path, monkeypatch):
    app, db_path = _load_app(tmp_path, monkeypatch)
    import app.state as _state

    rules = {'rules': [{'id': 'person_1', 'person_id': '1', 'name': 'Alice', 'enabled': True}]}
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _h, _b = _put(client, csrf, '/api/settings/face-detection-rules', json.dumps(rules).encode())
        assert status == 200
        for body in (b'[]', b'{}', b'{"rules": "x"}'):
            status, _h, _b = _put(client, csrf, '/api/settings/face-detection-rules', body)
            assert status == 400, body
        stored = _state.database.get_setting('face_detection_rules')
        assert [rule['id'] for rule in stored['rules']] == ['person_1']
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.parametrize('stream_url', [
    'file:///etc/shadow', 'concat:/etc/passwd|/etc/group', 'subfile,,start,0,end,0,,:/etc/passwd',
    '/etc/passwd', 'rtsp:', 'data:video/mp4;base64,AAAA',
])
def test_camera_stream_url_must_be_a_network_stream(stream_url):
    validators = importlib.import_module('app.payload_validators')
    with pytest.raises(HTTPException):
        validators.validate_camera_stream_source({'stream_url': stream_url})


@pytest.mark.parametrize('stream_url', [
    'rtsp://cam/stream1', 'RTSPS://cam:322/s', 'http://cam/mjpeg', 'https://cam/video',
    'rtmp://cam/live', 'srt://cam:9000', 'udp://239.0.0.1:1234',
])
def test_camera_stream_url_accepts_network_schemes(stream_url):
    validators = importlib.import_module('app.payload_validators')
    validators.validate_camera_stream_source({'stream_url': stream_url})


@pytest.mark.parametrize('host', ['cam/../x', 'evil.example@cam', 'cam?x', 'cam#f', 'cam name', 'cam\nx'])
def test_camera_host_cannot_rewrite_the_rtsp_url(host):
    validators = importlib.import_module('app.payload_validators')
    with pytest.raises(HTTPException):
        validators.validate_camera_stream_source({'host': host})
    validators.validate_camera_stream_source({'host': 'fd00::10'})
    validators.validate_camera_stream_source({'host': 'cam.local'})


def test_connection_test_endpoint_refuses_local_protocols(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _h, _b = client.request(
            '/api/cameras/test-connection', method='POST',
            data=json.dumps({'stream_url': 'file:///etc/passwd'}).encode(),
            headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        )
        assert status == 400
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_media_dirs_cannot_target_the_data_root_or_app_tree(monkeypatch, tmp_path):
    validators = importlib.import_module('app.payload_validators')
    app_root = (tmp_path / 'opt' / 'daygle').resolve()
    data_dir = app_root / 'data'
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(validators, '_APP_ROOT', app_root)
    monkeypatch.setattr(validators, '_STARTUP_DATA_DIR', data_dir)
    monkeypatch.setattr(validators, '_STARTUP_DATA_PARENT', data_dir.parent)
    monkeypatch.setattr(validators, 'effective_storage_config', lambda: {})

    for key, value in (
        ('snapshots_dir', str(data_dir)),          # the data root (holds the DB)
        ('events_dir', str(app_root)),             # the application itself
        ('snapshots_dir', str(app_root / 'app')),  # the code
        ('recordings_dir', str(app_root / '.venv')),
    ):
        with pytest.raises(HTTPException):
            validators.validate_storage_settings({key: value})
    out = validators.validate_storage_settings({'snapshots_dir': str(data_dir / 'snaps')})
    assert out['snapshots_dir'] == str(data_dir / 'snaps')


def test_media_dirs_may_still_move_to_a_sibling_outside_the_app(monkeypatch, tmp_path):
    validators = importlib.import_module('app.payload_validators')
    data_dir = (tmp_path / 'srv' / 'data').resolve()
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(validators, '_APP_ROOT', (tmp_path / 'opt' / 'daygle').resolve())
    monkeypatch.setattr(validators, '_STARTUP_DATA_DIR', data_dir)
    monkeypatch.setattr(validators, '_STARTUP_DATA_PARENT', data_dir.parent)
    monkeypatch.setattr(validators, 'effective_storage_config', lambda: {})
    sibling = str(data_dir.parent / 'recordings')
    assert validators.validate_storage_settings({'recordings_dir': sibling})['recordings_dir'] == sibling


def test_runtime_wipe_refuses_a_directory_holding_the_database(tmp_path, monkeypatch):
    recording_extension = importlib.import_module('app.recording_extension')
    state = importlib.import_module('app.state')
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    database = data_dir / 'daygle.sqlite3'
    database.write_bytes(b'db')
    (data_dir / 'photo.jpg').write_bytes(b'x')
    monkeypatch.setattr(state, 'database', SimpleNamespace(database_path=str(database)))

    assert recording_extension.clear_runtime_media_directory(str(data_dir)) == 0
    assert database.exists() and (data_dir / 'photo.jpg').exists()

    snapshots = data_dir / 'snapshots'
    snapshots.mkdir()
    (snapshots / 'a.jpg').write_bytes(b'x')
    assert recording_extension.clear_runtime_media_directory(str(snapshots)) > 0
    assert not (snapshots / 'a.jpg').exists()
    assert database.exists()


def test_auth_settings_reject_values_that_would_brick_startup(monkeypatch):
    validators = importlib.import_module('app.payload_validators')
    monkeypatch.setattr(validators, 'effective_auth_config', lambda: {})
    for bad in (
        {'session_timeout_hours': float('nan')},
        {'rate_limit_base_delay': float('nan')},
        {'rate_limit_base_delay': 30, 'rate_limit_max_delay': 10},
    ):
        with pytest.raises(HTTPException):
            validators.validate_auth_settings(bad)
    ok = validators.validate_auth_settings({'rate_limit_base_delay': 10, 'rate_limit_max_delay': 10})
    assert ok['rate_limit_max_delay'] == 10


def test_trusted_proxy_cidr_entries_match(monkeypatch):
    auth_gates = importlib.import_module('app.auth_gates')
    monkeypatch.setattr(auth_gates, '_trusted_proxies', lambda: frozenset({'10.0.0.0/24', '::1'}))
    assert auth_gates.is_trusted_proxy('10.0.0.7')
    assert auth_gates.is_trusted_proxy('::1')
    assert not auth_gates.is_trusted_proxy('10.0.1.7')
    assert not auth_gates.is_trusted_proxy('')

    request = SimpleNamespace(
        client=SimpleNamespace(host='10.0.0.7'),
        headers={'x-forwarded-for': '203.0.113.9, 10.0.0.7'},
    )
    assert auth_gates._request_ip(request) == '203.0.113.9'


def test_non_list_zone_points_fall_back_to_the_rectangle():
    zone_schema = importlib.import_module('app.zone_schema')
    zones = zone_schema.normalize_monitoring_zones([
        {'id': 'z', 'name': 'Z', 'points': 1, 'x': 0.1, 'y': 0.1, 'width': 0.5, 'height': 0.5},
    ])
    assert len(zones) == 1
    assert len(zones[0]['points']) >= 3


def test_update_script_allowlist_is_anchored():
    text = (Path(__file__).resolve().parents[1] / 'scripts' / 'update.sh').read_text()
    assert "EXPECTED_REMOTE_REGEX='^(" in text
    assert 'git pull --ff-only origin' in text


def _unit_directives() -> dict[str, str]:
    text = (Path(__file__).resolve().parents[1] / 'systemd' / 'daygle-ai-camera.service').read_text()
    directives: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(('#', '[')) and '=' in line:
            key, value = line.split('=', 1)
            directives[key] = value
    return directives


def test_systemd_unit_carries_root_compatible_hardening():
    directives = _unit_directives()
    for key in (
        'ProtectKernelModules', 'ProtectKernelTunables', 'ProtectControlGroups',
        'ProtectHostname', 'RestrictNamespaces', 'LockPersonality',
        'RestrictRealtime', 'RestrictSUIDSGID', 'PrivateTmp',
    ):
        assert directives.get(key) == 'yes', key
    assert directives.get('SystemCallArchitectures') == 'native'


def test_systemd_unit_does_not_restrict_devices_or_the_updater():
    """These break real features: /dev restrictions (including the implicit
    DeviceAllow= from ProtectClock / ProtectKernelLogs) hide the GPU and
    cameras; ProtectSystem/ProtectHome/NoNewPrivileges break the in-app
    updater; MemoryDenyWriteExecute can break JIT-compiling inference runtimes."""
    directives = _unit_directives()
    for key in (
        'PrivateDevices', 'DevicePolicy', 'DeviceAllow', 'ProtectClock', 'ProtectKernelLogs',
        'ProtectSystem', 'ProtectHome', 'NoNewPrivileges', 'MemoryDenyWriteExecute',
    ):
        assert key not in directives, key
