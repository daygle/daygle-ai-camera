"""Concurrency regressions for bootstrap and last-admin authorization checks."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.auth import AuthError, AuthService


def _auth(path):
    return AuthService(str(path), {})


def test_concurrent_initial_admin_bootstrap_creates_exactly_one_admin(tmp_path):
    database = tmp_path / 'auth.sqlite3'
    services = [_auth(database), _auth(database)]
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def create(service, username):
        barrier.wait(timeout=5)
        try:
            result = service.create_initial_admin(username, 'Str0ng-Pass!')
            outcome = ('created', result['username'])
        except AuthError:
            outcome = ('rejected', username)
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=create, args=(services[0], 'admin-one')),
        threading.Thread(target=create, args=(services[1], 'admin-two')),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(kind == 'created' for kind, _ in outcomes) == 1
    assert sum(kind == 'rejected' for kind, _ in outcomes) == 1
    assert len(services[0].list_users()) == 1
    assert services[0].list_users()[0]['role'] == 'admin'


def test_concurrent_last_admin_demotions_cannot_remove_all_admins(tmp_path):
    database = tmp_path / 'auth.sqlite3'
    auth = _auth(database)
    admin_one = auth.create_user('admin-one', 'Str0ng-Pass!', role='admin')
    admin_two = auth.create_user('admin-two', 'Str0ng-Pass!', role='admin')
    services = [_auth(database), _auth(database)]
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def demote(service, user_id):
        barrier.wait(timeout=5)
        try:
            service.update_user(user_id, role='viewer')
            outcome = 'demoted'
        except AuthError:
            outcome = 'rejected'
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=demote, args=(services[0], int(admin_one['id']))),
        threading.Thread(target=demote, args=(services[1], int(admin_two['id']))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert outcomes.count('demoted') == 1
    assert outcomes.count('rejected') == 1
    active_admins = [
        user for user in auth.list_users()
        if user['role'] == 'admin' and user['is_active']
    ]
    assert len(active_admins) == 1


@pytest.mark.parametrize('operation', ['login', 'password', 'profile'])
def test_password_reset_invalidates_inflight_verification(tmp_path, monkeypatch, operation):
    auth = _auth(tmp_path / 'auth.sqlite3')
    user = auth.create_user('viewer', 'Old-Pass1!', role='viewer')
    verified = threading.Event()
    resume = threading.Event()
    verify = auth.verify_password

    def pause_after_verify(password, password_hash):
        result = verify(password, password_hash)
        verified.set()
        if not resume.wait(10):
            raise TimeoutError('verification was not resumed')
        return result

    monkeypatch.setattr(auth, 'verify_password', pause_after_verify)

    def attempt():
        if operation == 'login':
            return auth.authenticate('viewer', 'Old-Pass1!', '10.0.0.1')
        if operation == 'password':
            return auth.change_password(user['id'], 'Old-Pass1!', 'Attacker-Pass1!')
        return auth.update_profile(user['id'], email='attacker@example.com', current_password='Old-Pass1!')

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(attempt)
        try:
            assert verified.wait(10)
            auth.update_user(user['id'], password='New-Pass2!')
        finally:
            resume.set()
        with pytest.raises(AuthError):
            future.result(timeout=10)

    with auth.connect() as db:
        row = db.execute('SELECT password_hash, email FROM users WHERE id = ?', (user['id'],)).fetchone()
        assert verify('New-Pass2!', row['password_hash'])
        assert row['email'] == ''
        assert db.execute('SELECT COUNT(*) FROM user_sessions WHERE user_id = ?', (user['id'],)).fetchone()[0] == 0


@pytest.mark.parametrize('field', ['email', 'username'])
def test_profile_identity_rechecked_before_write(tmp_path, monkeypatch, field):
    auth = _auth(tmp_path / 'auth.sqlite3')
    user = auth.create_user('viewer', 'Old-Pass1!', role='viewer', email='old@example.com')
    read = threading.Event()
    resume = threading.Event()
    get_user = auth.get_user

    def pause_after_read(user_id):
        result = get_user(user_id)
        if not read.is_set():
            read.set()
            if not resume.wait(10):
                raise TimeoutError('profile read was not resumed')
        return result

    monkeypatch.setattr(auth, 'get_user', pause_after_read)
    old_value = user[field]
    new_value = 'new@example.com' if field == 'email' else 'renamed'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(auth.update_profile, user['id'], **{field: old_value})
        try:
            assert read.wait(10)
            auth.update_user(user['id'], **{field: new_value})
        finally:
            resume.set()
        with pytest.raises(AuthError, match='Current password'):
            future.result(timeout=10)
    assert get_user(user['id'])[field] == new_value
