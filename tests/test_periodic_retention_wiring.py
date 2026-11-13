"""The periodic maintenance tick runs the session and retention purges.

Also covers the retention policy's handling of non-positive bounds, which the
settings API clamps to >= 1 but a hand-edited YAML / restored database can
still carry.
"""

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


class _RecordingDatabase:
    def __init__(self) -> None:
        self.purge_kwargs: dict = {}
        self.event_cutoffs: list = []

    def purge_recordings(self, **kwargs):
        self.purge_kwargs = kwargs
        return []

    def purge_events_without_recordings(self, _event_ids):
        return []

    def purge_expired_events_without_recordings(self, older_than):
        self.event_cutoffs.append(older_than)
        return []


def _run_policy(monkeypatch, recording_settings):
    import app.backup as backup
    import app.state as state

    database = _RecordingDatabase()
    monkeypatch.setattr(backup, 'effective_recording_config', lambda: recording_settings)
    monkeypatch.setattr(state, 'database', database)
    monkeypatch.setattr(backup, 'delete_recording_files', lambda _rows: None)
    monkeypatch.setattr(backup, 'delete_snapshot_files', lambda _rows: (0, 0))
    backup.purge_recordings_by_policy()
    return database


def test_non_positive_retention_bounds_mean_unlimited(monkeypatch):
    """A hand-edited ``0`` must not mean "cutoff now, cap zero bytes".

    Taken literally, ``retention_days: 0`` computes a cutoff of the current
    instant and ``max_storage_gb: 0`` a zero-byte cap, so one sweep would
    delete every recording. The settings API clamps both to >= 1, but this
    policy also reads config YAML and a restored database, where 0 is how the
    rest of the codebase spells "no limit" (storage_metrics reports a missing
    cap as None for ``max_gb <= 0``; face-recognition ``retention_days`` is
    documented as "0 = keep indefinitely").
    """
    database = _run_policy(monkeypatch, {
        'retention_days': 0, 'max_storage_gb': 0, 'auto_purge_enabled': True,
    })
    assert database.purge_kwargs['older_than'] is None
    assert database.purge_kwargs['max_storage_bytes'] is None
    # No age bound means no age sweep at all (a NULL cutoff matches nothing).
    assert database.event_cutoffs == []


def test_negative_retention_bounds_mean_unlimited(monkeypatch):
    database = _run_policy(monkeypatch, {
        'retention_days': -5, 'max_storage_gb': -1, 'auto_purge_enabled': True,
    })
    assert database.purge_kwargs['older_than'] is None
    assert database.purge_kwargs['max_storage_bytes'] is None


def test_positive_retention_bounds_are_passed_through(monkeypatch):
    database = _run_policy(monkeypatch, {
        'retention_days': 7, 'max_storage_gb': 5, 'auto_purge_enabled': True,
    })
    assert database.purge_kwargs['max_storage_bytes'] == 5 * 1024 ** 3
    assert database.purge_kwargs['older_than'] is not None
    assert len(database.event_cutoffs) == 1


def _stub_retention(monkeypatch, calls, *, record_paths=None):
    import app.backup as backup
    import app.recording_files as recording_files

    class _Database(_RecordingDatabase):
        def recording_file_paths(self):
            return record_paths or set()

    class _Service:
        recordings_dir = 'unused-root'

    monkeypatch.setattr(backup, 'effective_recording_config', lambda: {
        'retention_days': 7, 'max_storage_gb': 5, 'auto_purge_enabled': True,
    })
    monkeypatch.setattr(backup._state, 'database', _Database())
    monkeypatch.setattr(backup._state, 'recording_service', _Service(), raising=False)
    monkeypatch.setattr(backup, 'delete_recording_files', lambda _rows: None)
    monkeypatch.setattr(backup, 'delete_snapshot_files', lambda _rows: (0, 0))
    monkeypatch.setattr(
        recording_files, 'sweep_orphaned_recording_artifacts',
        lambda root, **kwargs: calls.append((root, kwargs)) or {
            'files_deleted': 0, 'bytes_deleted': 0,
        },
    )
    monkeypatch.setattr(backup, '_orphan_sweep_last_started', None)
    return backup


def test_retention_reclaims_files_whose_unlink_failed(monkeypatch):
    """A file locked during purge must be reclaimed without a restart."""
    calls = []
    backup = _stub_retention(monkeypatch, calls, record_paths={'/rec/kept.mp4'})
    backup.purge_recordings_by_policy()
    assert calls and calls[0][0] == 'unused-root'
    assert calls[0][1]['referenced_paths'] == {'/rec/kept.mp4'}


def test_the_after_retention_sweep_is_throttled(monkeypatch):
    calls = []
    backup = _stub_retention(monkeypatch, calls)
    clock = {'now': 100.0}
    monkeypatch.setattr(backup.time, 'monotonic', lambda: clock['now'])
    backup._sweep_orphans_after_retention()
    backup._sweep_orphans_after_retention()
    assert len(calls) == 1
    clock['now'] += backup._ORPHAN_SWEEP_INTERVAL_SECONDS
    backup._sweep_orphans_after_retention()
    assert len(calls) == 2
