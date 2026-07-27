"""Regression tests for last-active-admin protection in ``AuthService.update_user``.

Demoting or deactivating the only remaining active administrator would lock
every admin-only function (user management, settings, updates) out of the whole
deployment with no UI recovery path. ``update_user`` now refuses that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.auth import AuthError, AuthService  # noqa: E402


def _auth(tmp_path) -> AuthService:
    return AuthService(str(tmp_path / "auth.sqlite3"), {})


def test_cannot_demote_last_admin(tmp_path):
    auth = _auth(tmp_path)
    admin = auth.create_user("admin", "Str0ng-Pass!", role="admin")
    with pytest.raises(AuthError, match="last active administrator"):
        auth.update_user(int(admin["id"]), role="viewer")
    # Still an admin.
    assert auth.get_user(int(admin["id"]))["role"] == "admin"


def test_cannot_deactivate_last_admin(tmp_path):
    auth = _auth(tmp_path)
    admin = auth.create_user("admin", "Str0ng-Pass!", role="admin")
    with pytest.raises(AuthError, match="last active administrator"):
        auth.update_user(int(admin["id"]), is_active=False)
    assert bool(auth.get_user(int(admin["id"]))["is_active"]) is True


def test_can_demote_admin_when_another_active_admin_exists(tmp_path):
    auth = _auth(tmp_path)
    admin1 = auth.create_user("admin1", "Str0ng-Pass!", role="admin")
    auth.create_user("admin2", "Str0ng-Pass!", role="admin")
    # Two active admins -> demoting one is allowed.
    updated = auth.update_user(int(admin1["id"]), role="viewer")
    assert updated["role"] == "viewer"


def test_password_only_update_on_sole_admin_is_allowed(tmp_path):
    auth = _auth(tmp_path)
    admin = auth.create_user("admin", "Str0ng-Pass!", role="admin")
    # A password change does not touch role/is_active, so it must not trip the guard.
    updated = auth.update_user(int(admin["id"]), password="An0ther-Pass!")
    assert updated["role"] == "admin"


def test_deactivating_admin_when_other_admin_is_inactive_is_blocked(tmp_path):
    """Only the last *active* admin counts: an inactive second admin does not
    satisfy the guard."""
    auth = _auth(tmp_path)
    admin1 = auth.create_user("admin1", "Str0ng-Pass!", role="admin")
    admin2 = auth.create_user("admin2", "Str0ng-Pass!", role="admin")
    # Deactivate admin2 first (admin1 still active -> allowed).
    auth.update_user(int(admin2["id"]), is_active=False)
    # Now admin1 is the only ACTIVE admin -> cannot deactivate.
    with pytest.raises(AuthError, match="last active administrator"):
        auth.update_user(int(admin1["id"]), is_active=False)
