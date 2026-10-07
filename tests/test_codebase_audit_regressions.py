"""Focused regressions for the October 2026 codebase audit."""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import threading
import zipfile
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, URL


def test_global_rate_limit_sweep_keeps_recent_hits(monkeypatch):
    module = importlib.import_module('app.rate_limiter')
    limiter = module.SlidingWindowRateLimiter(max_requests=2, window_seconds=60, global_evict_interval=0)
    limiter._hits['active'] = deque([1.0, 80.0, 90.0])
    limiter._hits['expired'] = deque([1.0])
    monkeypatch.setattr(module.time, 'monotonic', lambda: 100.0)
    assert limiter.is_rate_limited('active')
    assert list(limiter._hits['active']) == [80.0, 90.0]
    assert 'expired' not in limiter._hits


def test_exponential_backoff_is_safe_after_large_failure_burst(monkeypatch):
    module = importlib.import_module('app.rate_limiter')
    limiter = module.IPRateLimiter(max_attempts=1, base_delay=2, max_delay=300)
    monkeypatch.setattr(module.time, 'time', lambda: 100.0)
    limiter._attempts['burst'] = [100.0] * 2000
    assert limiter.get_wait_seconds('burst') == 300
    limiter.base_delay = 0
    assert limiter.get_wait_seconds('burst') == 0


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
@pytest.mark.parametrize('field', ['window_seconds', 'base_delay', 'max_delay'])
def test_limiter_rejects_nonfinite_limits(field, value):
    module = importlib.import_module('app.rate_limiter')
    with pytest.raises(ValueError):
        module.IPRateLimiter(**{field: value})


def test_scheduler_never_runs_two_jobs_for_one_camera():
    module = importlib.import_module('app.inference_scheduler')
    entered = threading.Event()
    unblock = threading.Event()
    second = threading.Event()
    other = threading.Event()
    released = threading.Event()

    def runner(_image, _frame, settings):
        if settings['tag'] == 'first':
            entered.set()
            assert unblock.wait(3)
        elif settings['tag'] == 'second':
            second.set()
        else:
            other.set()

    scheduler = module.LiveInferenceScheduler(
        runner, max_workers=2, on_release=lambda cid: released.set() if cid == 'cam' else None,
    )
    scheduler.start()
    try:
        scheduler.submit('cam', {'tag': 'first'}, lambda: ('image', {}))
        assert entered.wait(2)
        scheduler.submit('cam', {'tag': 'second'}, lambda: ('image', {}))
        scheduler.submit('other', {'tag': 'other'}, lambda: ('image', {}))
        assert other.wait(2), 'a busy camera must not block unrelated cameras'
        assert not second.is_set(), 'same-camera jobs must be serialized'
        assert not released.is_set(), 'pending replacement must retain its claim'
        unblock.set()
        assert second.wait(2)
        assert released.wait(2)
    finally:
        unblock.set()
        scheduler.stop()


def test_scheduler_replacement_preserves_queue_position():
    module = importlib.import_module('app.inference_scheduler')
    scheduler = module.LiveInferenceScheduler(lambda *_: None)
    # Queue without workers for deterministic selection and fairness checks.
    scheduler._started = True
    scheduler._ensure_threads_locked = lambda: None
    scheduler.submit('a', {'tag': 'old'}, lambda: None)
    scheduler.submit('b', {}, lambda: None)
    scheduler.submit('a', {'tag': 'new'}, lambda: None)
    job, _depth = scheduler._take_next()
    assert job.camera_id == 'a'
    assert job.settings['tag'] == 'new'
    scheduler._started = False
    assert scheduler._take_next() is None


@pytest.mark.parametrize('headers', [
    {'Origin': 'http://cam:bad'},
    {'Origin': 'http://cam:99999'},
    {'Origin': 'http://cam', 'X-Forwarded-Host': '[broken'},
    {'Origin': 'http://cam', 'X-Forwarded-Host': 'cam:bad'},
])
def test_malformed_origin_and_proxy_headers_fail_closed(headers):
    middleware = importlib.import_module('app.middleware')
    request = SimpleNamespace(headers=Headers(headers), url=URL('http://cam/api/cameras'),
                              client=SimpleNamespace(host='127.0.0.1'))
    ok, _reason = middleware._is_same_origin(request)
    assert not ok


def _body_request(body: bytes):
    async def read_body():
        return body

    async def stream():
        if body:
            yield body

    # ``headers``/``stream`` back the form_data body-size cap (Content-Length
    # pre-check + streaming cap); ``body`` backs read_json_body.
    return SimpleNamespace(body=read_body, stream=stream, headers={})


@pytest.mark.parametrize('body', [b'{"value":1e999}', b'{"value":-1e999}', b'{"nested":[1e999]}'])
def test_json_numeric_overflow_rejected(body):
    helpers = importlib.import_module('app.request_helpers')
    with pytest.raises(HTTPException) as error:
        asyncio.run(helpers.read_json_body(_body_request(body)))
    assert error.value.status_code == 400


def test_invalid_utf8_form_is_a_client_error():
    helpers = importlib.import_module('app.request_helpers')
    with pytest.raises(HTTPException) as error:
        asyncio.run(helpers.form_data(_body_request(b'username=\xff')))
    assert error.value.status_code == 400


def test_unicode_pre_auth_csrf_does_not_crash():
    router = importlib.import_module('app.api.auth_router')
    request = SimpleNamespace(cookies={'daygle_csrf': 'ascii-token'})
    assert not router._csrf_double_submit_ok({'csrf_token': '\u2603'}, request)


def test_audit_log_accepts_auth_disabled_anonymous_user(monkeypatch):
    helpers = importlib.import_module('app.request_helpers')
    captured = []
    monkeypatch.setattr(helpers, '_request_ip', lambda _: '127.0.0.1')
    request = SimpleNamespace(state=SimpleNamespace(user={'id': None, 'username': 'anonymous'}))
    helpers.write_audit_log(request, SimpleNamespace(add_audit_log=lambda **kw: captured.append(kw)), 'save', 'settings')
    assert captured[0]['user_id'] is None


def test_restore_schema_rejected_before_row_queries(tmp_path):
    backup = importlib.import_module('app.backup')
    source = tmp_path / 'source.sqlite3'
    with sqlite3.connect(source) as conn:
        conn.executescript('''
            CREATE TABLE events(id INTEGER PRIMARY KEY);
            CREATE TABLE detections(id INTEGER PRIMARY KEY);
            CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE VIEW users AS SELECT 1 AS id, 'admin' AS role, 1 AS is_active;
        ''')
    with pytest.raises(HTTPException, match='unexpected views or triggers'):
        backup.validate_restore_database(source)


def test_full_restore_rejects_trigger_before_remapping_or_copying(tmp_path, monkeypatch):
    backup = importlib.import_module('app.backup')
    state = importlib.import_module('app.state')
    source = tmp_path / 'source.sqlite3'
    with sqlite3.connect(source) as conn:
        conn.executescript('''
            CREATE TABLE users(id INTEGER PRIMARY KEY, role TEXT, is_active INTEGER);
            INSERT INTO users VALUES(1, 'admin', 1);
            CREATE TABLE events(id INTEGER PRIMARY KEY);
            CREATE TABLE detections(id INTEGER PRIMARY KEY);
            CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TRIGGER hostile AFTER UPDATE ON app_settings BEGIN SELECT 1; END;
        ''')
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('manifest.json', json.dumps({'format': backup.FULL_BACKUP_FORMAT, 'version': 2}))
        zf.write(source, 'database/source.sqlite3')
        zf.writestr('recordings/clip.mp4', b'untrusted')
    monkeypatch.setattr(state, 'database', SimpleNamespace(database_path=tmp_path / 'live.sqlite3'))
    monkeypatch.setattr(backup, 'effective_storage_config', lambda: {})
    monkeypatch.setattr(backup, '_remap_restored_database', lambda *_: pytest.fail('remapped unvalidated schema'))
    monkeypatch.setattr(backup, '_copy_restored_tree', lambda *_: pytest.fail('copied files before rejecting schema'))
    with pytest.raises(HTTPException, match='unexpected views or triggers'):
        backup.restore_full_backup(archive)


@pytest.mark.parametrize('password, stored', [('x' * 73, None), ('Valid1!Pass', 'invalid-bcrypt-hash')])
def test_bcrypt_invalid_inputs_are_failed_verifications(tmp_path, password, stored):
    auth_module = importlib.import_module('app.auth')
    auth = auth_module.AuthService(str(tmp_path / 'auth.sqlite3'), {})
    stored = stored or auth.hash_password('Valid1!Pass')
    assert not auth.verify_password(password, stored)


def test_long_password_login_records_a_normal_failure(tmp_path):
    auth_module = importlib.import_module('app.auth')
    auth = auth_module.AuthService(str(tmp_path / 'auth.sqlite3'), {})
    user = auth.create_user('admin', 'Valid1!Pass', role='admin')
    with pytest.raises(auth_module.AuthError, match='Invalid username or password'):
        auth.authenticate('admin', 'x' * 73, '127.0.0.1')
    assert auth.get_user(user['id'])['failed_attempts'] == 1


def test_scheduler_claim_is_published_before_workers_can_run():
    module = importlib.import_module('app.inference_scheduler')
    claimed = threading.Event()
    scheduler = module.LiveInferenceScheduler(lambda *_: None, on_claim=lambda _: claimed.set())
    scheduler._started = True
    # Deterministically assert ordering at worker admission, not via sleeps.
    scheduler._ensure_threads_locked = lambda: claimed.is_set() or pytest.fail('worker admitted before claim')
    assert scheduler.submit('cam', {}, lambda: None)


def test_scheduler_creation_is_shared_across_concurrent_callers(monkeypatch):
    monitor = importlib.import_module('app.live_monitor')
    state = importlib.import_module('app.state')
    constructed = []
    instances = []
    barrier = threading.Barrier(8)

    class Scheduler:
        def __init__(self, *_args, **_kwargs):
            constructed.append(self)
        def start(self):
            pass
        def set_max_workers(self, _count):
            pass

    monkeypatch.setattr(state, 'live_inference_scheduler', None)
    monkeypatch.setattr(monitor, 'LiveInferenceScheduler', Scheduler)
    monkeypatch.setattr(monitor, '_scheduler_max_workers', lambda: 2)

    def get_scheduler():
        barrier.wait(2)
        instances.append(monitor.get_live_inference_scheduler())

    threads = [threading.Thread(target=get_scheduler) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert len(instances) == 8
    assert len(constructed) == 1
    assert all(instance is constructed[0] for instance in instances)


def test_backup_manifest_size_is_bounded(tmp_path, monkeypatch):
    backup = importlib.import_module('app.backup')
    monkeypatch.setattr(backup, 'FULL_BACKUP_MAX_MANIFEST_BYTES', 32)
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('manifest.json', ' ' * 33)
        zf.writestr('database/source.sqlite3', b'database')
    with pytest.raises(HTTPException, match='manifest is too large'):
        backup.validate_full_backup(archive)


# ── Audit follow-up: cross-role credential redaction ───────────────────────

def test_redact_camera_secrets_masks_stream_url_credentials():
    """A credentialed ``stream_url`` (accepted at save time by
    ``validate_camera_stream_source``) must never reach a viewer-role
    response with its userinfo intact: ``redact_camera_secrets`` strips the
    password field AND masks embedded URL credentials."""
    camera_config = importlib.import_module('app.camera_config')
    out = camera_config.redact_camera_secrets({
        'id': 'front', 'name': 'Front', 'username': 'svc', 'password': 'hunter2',
        'stream_url': 'rtsp://svc:hunter2@cam.lan:8554/stream1',
    })
    assert 'password' not in out
    assert out['has_password'] is True
    assert out['has_stream_url_credentials'] is True
    assert 'hunter2' not in out['stream_url']
    assert '@' not in out['stream_url']
    assert out['stream_url'] == 'rtsp://cam.lan:8554/stream1'
    # Non-secret identity fields survive (consumers render camera names).
    assert out['id'] == 'front'
    assert out['name'] == 'Front'


def test_redact_camera_secrets_leaves_clean_stream_url_unchanged():
    camera_config = importlib.import_module('app.camera_config')
    plain = 'rtsp://cam.lan/stream1'
    out = camera_config.redact_camera_secrets({'id': 'front', 'stream_url': plain})
    assert out['stream_url'] == plain
    assert out['has_stream_url_credentials'] is False
    assert out['has_password'] is False


def test_redact_alerts_block_strips_smtp_and_ntfy_secrets():
    """``GET /api/config`` serves ``config['alerts']`` to viewer-role users,
    so the SMTP and ntfy credentials an operator bootstrapped via config.yaml
    must be replaced with configured/not-configured hints."""
    admin_router = importlib.import_module('app.api.admin_router')
    alerts = {
        'enabled': True,
        'email': {'enabled': True, 'host': 'smtp.example', 'username': 'alerts@example', 'password': 'smtp-secret'},
        'push_notification': {'enabled': True, 'server_url': 'https://ntfy.sh', 'username': 'user', 'password': 'ntfy-secret'},
        'rules': [{'id': 'r1'}],
    }
    out = admin_router._redact_alerts_block(alerts)
    assert out['email']['password'] == ''
    assert out['email']['has_password'] is True
    assert out['email']['username'] == ''
    assert out['email']['has_username'] is True
    assert out['push_notification']['password'] == ''
    assert out['push_notification']['has_password'] is True
    assert out['push_notification']['username'] == ''
    # Non-secret alert config is preserved for consumers.
    assert out['email']['host'] == 'smtp.example'
    assert out['push_notification']['server_url'] == 'https://ntfy.sh'
    assert out['rules'] == [{'id': 'r1'}]
    # The input dict is not mutated.
    assert alerts['email']['password'] == 'smtp-secret'


def test_form_data_rejects_oversized_preauth_body():
    """``POST /login`` and ``POST /setup`` are pre-auth: their form body must
    be capped so an unauthenticated client cannot force an unbounded memory
    allocation (streaming cap)."""
    helpers = importlib.import_module('app.request_helpers')

    async def stream(chunks):
        for chunk in chunks:
            yield chunk

    over_cap = [b'a' * (helpers.MAX_FORM_BYTES // 2)] * 3  # 1.5x the cap
    request = SimpleNamespace(stream=lambda: stream(over_cap), headers={})
    with pytest.raises(HTTPException) as error:
        asyncio.run(helpers.form_data(request))
    assert error.value.status_code == 413


def test_form_data_rejects_oversized_content_length_upfront():
    helpers = importlib.import_module('app.request_helpers')

    async def stream():
        yield b'x'  # must never be read
        raise AssertionError('stream must not be consumed when Content-Length already exceeds the cap')

    request = SimpleNamespace(stream=stream, headers={'content-length': str(helpers.MAX_FORM_BYTES + 1)})
    with pytest.raises(HTTPException) as error:
        asyncio.run(helpers.form_data(request))
    assert error.value.status_code == 413


def test_form_data_still_parses_small_bodies():
    helpers = importlib.import_module('app.request_helpers')

    async def stream():
        yield b'username=admin&password=Admin123%21'

    request = SimpleNamespace(stream=stream, headers={'content-length': '35'})
    data = asyncio.run(helpers.form_data(request))
    assert data == {'username': 'admin', 'password': 'Admin123!'}
