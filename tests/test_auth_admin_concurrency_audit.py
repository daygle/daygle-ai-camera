"""Concurrency regressions for bootstrap and last-admin authorization checks."""
from __future__ import annotations

import threading

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
