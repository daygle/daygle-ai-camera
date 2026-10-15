"""The periodic maintenance tick runs the session and retention purges."""

from tests.support import _load_app, _server


def test_maintenance_tick_runs_session_and_retention_cleanup(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    import app.state as state

    captured = {}
    monkeypatch.setattr(
        state.database, 'start_maintenance',
        lambda interval_seconds=900.0, callback=None: captured.setdefault('callback', callback),
    )
    server, thread, _base_url = _server(app)
    try:
        assert captured.get('callback') is not None
        calls = []
        monkeypatch.setattr(state.auth, 'cleanup_expired_sessions', lambda: calls.append('auth'))
        monkeypatch.setattr('app.backup.purge_recordings_by_policy', lambda: calls.append('recordings'))
        captured['callback']()
        assert calls == ['recordings', 'auth']
    finally:
        server.should_exit = True
        thread.join(timeout=5)
