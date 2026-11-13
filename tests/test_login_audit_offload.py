"""``/login`` writes its audit rows off the event loop.

A database write can wait on the shared write slots and SQLite's busy timeout
(up to 30 s). ``/login`` is unauthenticated, so writing its audit rows inline
on the event loop let a burst of login attempts - or one attempt during a long
write - stall every stream and request. Each login audit write (success,
failure and rate-limited) must run in the threadpool, where no asyncio loop is
running.
"""

from __future__ import annotations

import asyncio

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin


def _post_login(client: LocalClient, password: str) -> int:
    client.request('/login')
    status, _headers, _body = client.request(
        '/login',
        method='POST',
        form={'username': 'admin', 'password': password, 'csrf_token': client.cookie('daygle_csrf')},
    )
    return status


def test_login_audit_writes_run_off_the_event_loop(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    # tests.support re-imports the app.* namespace per test, so read the
    # live state module only after _load_app.
    import app.state as _state

    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    calls: list[tuple[str, str, bool]] = []
    try:
        _setup_admin(client)
        database_cls = type(_state.database)
        original = database_cls.add_audit_log

        def recording_add_audit_log(self, **kwargs):
            try:
                asyncio.get_running_loop()
                on_event_loop = True
            except RuntimeError:
                on_event_loop = False
            if kwargs.get('action') == 'login':
                calls.append((kwargs['resource'], kwargs['status'], on_event_loop))
            return original(self, **kwargs)

        monkeypatch.setattr(database_cls, 'add_audit_log', recording_add_audit_log)

        _login(client)
        _post_login(LocalClient(base_url), 'wrong-password')
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    assert ('session', 'success', False) in calls
    assert ('session', 'failed', False) in calls
    assert not [call for call in calls if call[2]], f'login audit written on the event loop: {calls}'
