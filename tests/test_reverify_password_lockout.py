"""Wrong current-password guesses on signed-in endpoints count toward lockout.

``change_password`` and ``update_profile`` (username/email change) re-verify
the account password. Those guesses previously never touched
``failed_attempts``, so anyone holding a stolen session cookie could
brute-force the password through ``/api/profile/password`` with no limit and
then lock the real owner out. They now share the sign-in lockout counter.
"""
from __future__ import annotations

import pytest

from app.auth import AuthError, AuthService

PASSWORD = 'Str0ng-Pass!'


def _auth(tmp_path, max_attempts: int = 3) -> tuple[AuthService, int]:
    auth = AuthService(str(tmp_path / 'auth.sqlite3'), {'max_login_attempts': max_attempts})
    user = auth.create_user('alex', PASSWORD, role='admin')
    return auth, int(user['id'])


def test_wrong_current_password_on_change_password_increments_and_locks(tmp_path):
    auth, user_id = _auth(tmp_path)
    for expected in (1, 2):
        with pytest.raises(AuthError, match='Current password is incorrect'):
            auth.change_password(user_id, 'Wr0ng-Guess!', 'N3w-Str0ng-Pass!')
        assert auth.get_user(user_id)['failed_attempts'] == expected
        assert auth.get_user(user_id)['locked_until'] is None
    with pytest.raises(AuthError):
        auth.change_password(user_id, 'Wr0ng-Guess!', 'N3w-Str0ng-Pass!')
    assert auth.get_user(user_id)['locked_until'] is not None
    # Once locked, even the correct password is refused on every path.
    with pytest.raises(AuthError, match='temporarily locked'):
        auth.change_password(user_id, PASSWORD, 'N3w-Str0ng-Pass!')
    with pytest.raises(AuthError, match='temporarily locked'):
        auth.authenticate('alex', PASSWORD, '10.0.0.9')


def test_wrong_current_password_on_profile_identity_change_increments(tmp_path):
    auth, user_id = _auth(tmp_path)
    with pytest.raises(AuthError, match='Current password is incorrect'):
        auth.update_profile(user_id, email='new@example.com', current_password='Wr0ng-Guess!')
    assert auth.get_user(user_id)['failed_attempts'] == 1
    assert auth.get_user(user_id)['email'] == ''


def test_successful_password_change_resets_the_counter(tmp_path):
    auth, user_id = _auth(tmp_path)
    with pytest.raises(AuthError):
        auth.change_password(user_id, 'Wr0ng-Guess!', 'N3w-Str0ng-Pass!')
    auth.change_password(user_id, PASSWORD, 'N3w-Str0ng-Pass!')
    assert auth.get_user(user_id)['failed_attempts'] == 0


def test_locked_sign_in_pays_the_same_hashing_cost(tmp_path, monkeypatch):
    # The login page shows a locked account the generic "Invalid username or
    # password" message; the locked branch must not answer measurably faster
    # either, or response time would still reveal that the username exists.
    auth, user_id = _auth(tmp_path, max_attempts=1)
    with pytest.raises(AuthError):
        auth.authenticate('alex', 'Wr0ng-Guess!', '10.0.0.1')
    assert auth.get_user(user_id)['locked_until'] is not None
    calls: list[str] = []
    # Patch the globals ``authenticate`` actually resolves against: other tests
    # reload ``app.auth``, so ``import app.auth`` here can be a newer module
    # object than the one this file's ``AuthService`` was defined in.
    monkeypatch.setitem(AuthService.authenticate.__globals__, '_equalize_password_timing', calls.append)
    with pytest.raises(AuthError, match='temporarily locked'):
        auth.authenticate('alex', PASSWORD, '10.0.0.2')
    assert calls == [PASSWORD]
