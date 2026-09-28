"""Integration tests for hardened auth edge cases.

Covers every fix applied in the auth-hardening conversation:

1. **Stale-CSRF logout resilience** - POST /logout with a wrong or missing
   CSRF token must still delete the session and return ``{'ok': True}``
   instead of 403.

2. **Session deletion is permanent** - After a stale-CSRF logout, the old
   session cookie must not authenticate subsequent API calls.

3. **Normal logout still works** - POST /logout with a valid CSRF token
   behaves identically.

Tests start a real uvicorn server via the shared ``tests/support.py``
harness (``_load_app`` + ``_server`` + ``LocalClient``).
"""

from __future__ import annotations

import pytest

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin


# ── Auth edge case tests ───────────────────────────────────────────────


class TestStaleCsrfLogoutResilience:
    """POST /logout must be resilient to stale or missing CSRF tokens.

    The fix in auth_router.py replaced a hard 403 with graceful session
    deletion when the CSRF token doesn't match. These tests verify that
    behaviour at the HTTP level.
    """

    def test_logout_with_valid_csrf_succeeds(self, tmp_path, monkeypatch):
        """Logout with a correct CSRF token works as expected."""
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            csrf = _login(client)

            status, _headers, payload = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": csrf}
            )
            assert status == 200
            assert payload["ok"] is True

            # Session should be gone - next API call gets 401.
            status, _headers, _body = client.request("/api/status")
            assert status == 401
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_logout_with_wrong_csrf_returns_ok(self, tmp_path, monkeypatch):
        """Logout with a WRONG CSRF token deletes the session and returns 200.

        This is the primary resilience fix: a stale token must not prevent
        the user from logging out.
        """
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            # Send a deliberately wrong CSRF token.
            status, _headers, payload = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "this-is-wrong"}
            )
            assert status == 200, (
                f"Expected 200 with stale CSRF, got {status}: {payload}"
            )
            assert payload["ok"] is True

            # Session must be deleted - subsequent API call gets 401.
            status, _headers, _body = client.request("/api/status")
            assert status == 401
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_logout_with_missing_csrf_header_returns_ok(self, tmp_path, monkeypatch):
        """Logout with NO CSRF header deletes the session and returns 200.

        The edge case: when window.daygleAuth.csrfToken is null (cleared by
        a concurrent handleSessionLoss), the frontend sends POST /logout
        with an empty token header. The server must accept this.
        """
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            # Logout WITHOUT an X-CSRF-Token header.
            status, _headers, payload = client.request(
                "/logout", method="POST"
            )
            assert status == 200, (
                f"Expected 200 with missing CSRF header, got {status}: {payload}"
            )
            assert payload["ok"] is True

            # Session must be deleted.
            status, _headers, _body = client.request("/api/status")
            assert status == 401
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_logout_with_wrong_csrf_still_clears_cookies(self, tmp_path, monkeypatch):
        """After stale-CSRF logout, the session cookie is gone."""
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            session_before = client.cookie("daygle_session")
            assert session_before is not None

            status, _headers, payload = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "stale-token"}
            )
            assert status == 200
            assert payload["ok"] is True

            # The session cookie should be cleared (deleted or empty).
            session_after = client.cookie("daygle_session")
            assert session_after is None or session_after == "", (
                f"Session cookie should be cleared after stale-CSRF logout, "
                f"got: {session_after!r}"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_double_logout_does_not_crash(self, tmp_path, monkeypatch):
        """Two rapid POST /logout calls with the same session must not 500.

        This guards against a rare race: the frontend dispatches two logout
        requests in quick succession (e.g., the nav.js click handler fires
        twice due to an event-duplication bug). Both should return 200.
        """
        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            # First logout - succeeds.
            status1, _headers, payload1 = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "stale-token"}
            )
            assert status1 == 200
            assert payload1["ok"] is True

            # Second logout with the same session cookie (which was deleted
            # above). The middleware returns 401 before reaching the handler.
            status2, _headers, _body = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "stale-token"}
            )
            # 401 is acceptable - the session is already gone, no crash.
            assert status2 in (200, 401), (
                f"Rapid second logout should not 500, got {status2}"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_stale_csrf_logout_after_session_timeout(self, tmp_path, monkeypatch):
        """Simulate session expiry, then logout with stale token.

        When the session has expired server-side but the client still has the
        old cookie, a stale-CSRF logout should be handled gracefully (not
        crash with 500 or reveal internal errors).
        """
        app, database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)
            _login(client)

            # Manually expire the session in the database to simulate
            # timeout while the user still has the old cookie.
            import sqlite3
            with sqlite3.connect(database_path) as db:
                db.execute("UPDATE user_sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
                db.commit()

            # Now logout with what looks like a valid token but the session
            # is expired server-side.
            status, _headers, payload = client.request(
                "/logout", method="POST", headers={"X-CSRF-Token": "any-token"}
            )
            # The middleware sees the now-expired session and returns 401
            # before the logout handler runs. The frontend's handleSessionLoss
            # would have already redirected, so this is fine - no crash.
            assert status in (200, 401), (
                f"Logout after session expiry should not crash, got {status}: {payload}"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=5)


# ── Username-enumeration timing equaliser (Bug B) ────────────────────────


class TestUsernameEnumerationTimingEqualiser:
    """``AuthService.authenticate`` must cost-equalise the unknown-username
    path with the known-username / wrong-password path.

    Without the equaliser, an attacker can time login responses to
    enumerate valid usernames: ``row is None`` short-circuits the
    ``verify_password`` call (no bcrypt work), but a known user with a
    wrong password pays the full ``bcrypt.checkpw`` round. The fix calls
    ``_equalize_password_timing`` on the missing-row path so both paths
    pay equivalent CPU.

    The fix-level test below patches ``bcrypt.checkpw`` to count calls
    and verifies that authenticate on an unknown username invokes
    ``bcrypt.checkpw`` at least once (the dummy equaliser hit).
    """

    def test_authenticate_unknown_user_pays_full_password_verify_cost(
        self, tmp_path, monkeypatch
    ):
        """An unknown-username login attempt must invoke ``bcrypt.checkpw`` at
        least once via the timing equaliser.

        Guards the Bug-B fix: without the equaliser, the unknown-user path
        short-circuits before ``verify_password`` and an attacker can
        differentiate unknown vs known usernames from response latency.
        """
        from app import auth as auth_module
        from app import rate_limiter as rl_module

        # Reset the in-memory rate limiter so this single test attempt isn't
        # affected by earlier login attempts sharing the same peer IP.
        rl_module.login_limiter.clear()

        app, _database_path = _load_app(tmp_path, monkeypatch)
        server, thread, base_url = _server(app)
        client = LocalClient(base_url)
        try:
            _setup_admin(client)

            # Patch bcrypt.checkpw to count invocations.
            if auth_module.bcrypt is None:
                pytest.skip("bcrypt not installed in this environment")
            original = auth_module.bcrypt.checkpw
            call_count = {"count": 0}

            def counting(*args, **kwargs):
                call_count["count"] += 1
                return original(*args, **kwargs)

            monkeypatch.setattr(auth_module.bcrypt, "checkpw", counting)

            # Harvest a fresh CSRF token for the login submission.
            status, _headers, _body = client.request("/login")
            assert status == 200
            csrf = client.cookie("daygle_csrf")
            assert csrf, "POST /login requires a daygle_csrf cookie value"

            # Reset the counter so we only count this attempt's calls.
            call_count["count"] = 0

            # Submit a login attempt with a username that cannot exist.
            status, _headers, body = client.request(
                "/login",
                method="POST",
                form={
                    "username": "this-user-definitely-does-not-exist-xyz123",
                    "password": "SomeRandomPassword!",
                    "csrf_token": csrf or "",
                },
                follow_redirects=False,
            )

            # The request must NOT 500 (the dummy equaliser never raises) and
            # must surface the standard "invalid credentials" error.
            assert status == 200, (
                f"Unknown-username login should return 200 error page, got "
                f"{status}: {body!r}"
            )
            body_text = body if isinstance(body, str) else str(body)
            assert "Invalid username or password" in body_text, (
                f"Unknown-username error message missing: {body_text!r}"
            )

            # The Bug-B fix: at least one bcrypt.checkpw call (the equaliser
            # dummy) must have occurred.
            assert call_count["count"] >= 1, (
                f"authenticate on unknown user must invoke bcrypt.checkpw at "
                f"least once for timing equalisation; got {call_count['count']} "
                f"calls. Username-enumeration timing oracle still present."
            )
        finally:
            server.should_exit = True
            thread.join(timeout=5)
