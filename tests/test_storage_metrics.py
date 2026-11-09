"""Unit tests for ``app.storage_metrics`` (disk space and media usage)."""

from __future__ import annotations

import os
from collections import namedtuple

import pytest

import app.storage_metrics as storage_metrics


@pytest.fixture(autouse=True)
def _reset_usage_cache():
    storage_metrics.reset_usage_cache()
    yield
    storage_metrics.reset_usage_cache()


def _write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size)


def test_directory_size_counts_nested_files(tmp_path):
    _write(tmp_path / 'a.mp4', 100)
    _write(tmp_path / 'cam' / 'b.mp4', 50)
    _write(tmp_path / 'cam' / 'deep' / 'c.mp4', 25)
    assert storage_metrics.directory_size(tmp_path) == 175


def test_directory_size_skips_hidden_working_dirs_and_symlinks(tmp_path):
    _write(tmp_path / 'clip.mp4', 100)
    _write(tmp_path / '.prebuffer' / 'seg.ts', 1000)
    (tmp_path / 'link.mp4').symlink_to(tmp_path / 'clip.mp4')
    assert storage_metrics.directory_size(tmp_path) == 100
    assert storage_metrics.directory_size(tmp_path, skip_hidden=False) == 1100


def test_directory_size_of_missing_folder_is_zero(tmp_path):
    assert storage_metrics.directory_size(tmp_path / 'nope') == 0


def test_database_size_includes_wal_and_shm(tmp_path):
    db = tmp_path / 'daygle.sqlite3'
    _write(db, 4096)
    _write(tmp_path / 'daygle.sqlite3-wal', 1024)
    assert storage_metrics.database_size(db) == 5120


StatVfs = namedtuple('StatVfs', 'f_frsize f_bsize f_blocks f_bfree f_bavail')


def test_volume_usage_matches_df(monkeypatch):
    # 1000 blocks of 4 KiB; 400 free, of which 350 usable by non-root.
    monkeypatch.setattr(os, 'statvfs', lambda _p: StatVfs(4096, 4096, 1000, 400, 350), raising=False)
    volume = storage_metrics.volume_usage('/data')
    assert volume == {
        'total_bytes': 1000 * 4096,
        'used_bytes': 600 * 4096,
        'free_bytes': 350 * 4096,
        'percent': round(600 / 950 * 100, 1),
    }


def test_volume_usage_failure_returns_none(monkeypatch):
    def _boom(_path):
        raise OSError('gone')

    monkeypatch.setattr(os, 'statvfs', _boom, raising=False)
    assert storage_metrics.volume_usage('/missing') is None


def test_storage_status_groups_folders_on_one_volume(tmp_path):
    recordings = tmp_path / 'recordings'
    snapshots = tmp_path / 'snapshots'
    _write(recordings / 'clip.mp4', 300)
    _write(snapshots / 'shot.jpg', 20)
    db = tmp_path / 'db.sqlite3'
    _write(db, 10)
    status = storage_metrics.storage_status(
        {'recordings': recordings, 'snapshots': snapshots, 'events': tmp_path / 'events-not-created'},
        db,
        retention={'max_storage_gb': 20, 'retention_days': 14, 'auto_purge_enabled': True},
    )
    assert len(status['volumes']) == 1
    assert status['volumes'][0]['holds'] == ['recordings', 'snapshots', 'events', 'database']
    assert status['usage'] == {
        'recordings_bytes': 300,
        'snapshots_bytes': 20,
        'events_bytes': 0,
        'database_bytes': 10,
    }
    assert status['retention'] == {
        'max_storage_bytes': 20 * 1024 ** 3,
        'retention_days': 14,
        'auto_purge_enabled': True,
    }


def test_storage_status_caches_directory_walks(tmp_path, monkeypatch):
    _write(tmp_path / 'clip.mp4', 100)
    calls = []
    real = storage_metrics.directory_size
    monkeypatch.setattr(storage_metrics, 'directory_size', lambda p, **kw: calls.append(p) or real(p, **kw))
    for _ in range(3):
        storage_metrics.storage_status({'recordings': tmp_path}, None)
    assert len(calls) == 1

    # A new set of folders is a different cache entry.
    storage_metrics.storage_status({'recordings': tmp_path, 'snapshots': tmp_path}, None)
    assert len(calls) == 3


def test_storage_status_without_retention_cap(tmp_path):
    status = storage_metrics.storage_status({'recordings': tmp_path}, None, retention={'max_storage_gb': 0})
    assert status['retention']['max_storage_bytes'] is None
