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
