"""Storage places unset subdirectories under ``data_dir``.

``recordings_dir`` always fell back to ``data_dir / 'recordings'``, but
``snapshots_dir`` and ``events_dir`` fell back to a working-directory-relative
``data/...``, so a config with a custom ``data_dir`` and no explicit
subdirectories split its files across two trees.
"""

from __future__ import annotations

from app.storage import Storage


def test_unset_subdirectories_follow_data_dir(tmp_path):
    data_dir = tmp_path / 'custom'
    storage = Storage({'storage': {'data_dir': str(data_dir)}})
    assert storage.snapshots_dir == data_dir / 'snapshots'
    assert storage.events_dir == data_dir / 'events'
    assert storage.recordings_dir == data_dir / 'recordings'
    for directory in (storage.snapshots_dir, storage.events_dir, storage.recordings_dir):
        assert directory.is_dir()


def test_empty_subdirectories_follow_data_dir(tmp_path):
    data_dir = tmp_path / 'custom'
    storage = Storage({'storage': {
        'data_dir': str(data_dir), 'snapshots_dir': '', 'events_dir': None, 'recordings_dir': '',
    }})
    assert storage.snapshots_dir == data_dir / 'snapshots'
    assert storage.events_dir == data_dir / 'events'
    assert storage.recordings_dir == data_dir / 'recordings'


def test_explicit_subdirectories_are_kept(tmp_path):
    storage = Storage({'storage': {
        'data_dir': str(tmp_path / 'data'),
        'snapshots_dir': str(tmp_path / 'snaps'),
        'events_dir': str(tmp_path / 'evts'),
        'recordings_dir': str(tmp_path / 'recs'),
    }})
    assert storage.snapshots_dir == tmp_path / 'snaps'
    assert storage.events_dir == tmp_path / 'evts'
    assert storage.recordings_dir == tmp_path / 'recs'
