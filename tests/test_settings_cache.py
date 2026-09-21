"""Regression tests for the per-instance settings read cache.

``get_setting`` is on the detection hot path (every camera cycle reads
``live`` / ``objects`` / ``ai``), and each read otherwise opens a fresh SQLite
connection. The cache removes that churn while keeping identical semantics:
every read returns a fresh, independently-mutable object, and any write is
reflected immediately.
"""
from __future__ import annotations

from app.auth import utc_now
from app.database import EventDatabase


def _db(tmp_path) -> EventDatabase:
    return EventDatabase(str(tmp_path / 'settings.sqlite3'))


def test_get_setting_reflects_writes_immediately(tmp_path):
    db = _db(tmp_path)
    assert db.get_setting('objects') is None
    db.set_setting('objects', {'default_mode': 'moving'}, utc_now())
    assert db.get_setting('objects') == {'default_mode': 'moving'}
    # A second write must be seen on the next read (cache invalidated on write).
    db.set_setting('objects', {'default_mode': 'any'}, utc_now())
    assert db.get_setting('objects') == {'default_mode': 'any'}


def test_cached_read_returns_independent_object(tmp_path):
    db = _db(tmp_path)
    db.set_setting('objects', {'default_mode': 'moving', 'labels': {}}, utc_now())
    first = db.get_setting('objects')
    first['default_mode'] = 'MUTATED'
    first['labels']['car'] = 'still'
    # Mutating a returned value must not corrupt the cache for the next reader.
    assert db.get_setting('objects') == {'default_mode': 'moving', 'labels': {}}


def test_invalidate_setting_cache_forces_reread(tmp_path):
    db = _db(tmp_path)
    db.set_setting('storage', {'data_dir': '/a'}, utc_now())
    assert db.get_setting('storage') == {'data_dir': '/a'}  # populates cache
    # Simulate a write that bypasses set_setting (e.g. the restore path).
    with db.connect() as conn:
        import json
        conn.execute(
            "UPDATE app_settings SET value = ? WHERE key = 'storage'",
            (json.dumps({'data_dir': '/b'}),),
        )
    db.invalidate_setting_cache('storage')
    assert db.get_setting('storage') == {'data_dir': '/b'}


def test_absent_key_is_cached_as_none(tmp_path):
    db = _db(tmp_path)
    assert db.get_setting('missing') is None
    assert db.get_setting('missing') is None  # served from cache, still None
    db.set_setting('missing', {'x': 1}, utc_now())
    assert db.get_setting('missing') == {'x': 1}  # write invalidates the None entry
