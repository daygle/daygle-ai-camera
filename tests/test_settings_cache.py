"""Regression tests for the per-instance settings read cache.

``get_setting`` is on the detection hot path (every camera cycle reads
``live`` / ``objects`` / ``ai``), and each read otherwise opens a fresh SQLite
connection. The cache removes that churn while keeping identical semantics:
every read returns a fresh, independently-mutable object, and any write is
reflected immediately.
"""
from __future__ import annotations

import sqlite3

import pytest

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


def test_multi_setting_write_is_atomic_and_invalidates_cache(tmp_path):
    db = _db(tmp_path)
    db.set_setting('cloudflare_tunnel', {'tunnel_loopback_only': False}, utc_now())
    db.set_setting('auth', {'trusted_proxies': []}, utc_now())
    assert db.get_setting('cloudflare_tunnel')['tunnel_loopback_only'] is False
    assert db.get_setting('auth')['trusted_proxies'] == []
    with db.connect() as conn:
        conn.execute("CREATE TRIGGER reject_auth BEFORE UPDATE ON app_settings "
                     "WHEN NEW.key = 'auth' BEGIN SELECT RAISE(ABORT, 'rejected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='rejected'):
        db.set_settings({'cloudflare_tunnel': {'tunnel_loopback_only': True},
                         'auth': {'trusted_proxies': ['127.0.0.1']}}, utc_now())
    db.invalidate_setting_cache()
    assert db.get_setting('cloudflare_tunnel') == {'tunnel_loopback_only': False}
    assert db.get_setting('auth') == {'trusted_proxies': []}
    with db.connect() as conn:
        conn.execute('DROP TRIGGER reject_auth')
    db.set_settings({'cloudflare_tunnel': {'tunnel_loopback_only': True},
                     'auth': {'trusted_proxies': ['127.0.0.1']}}, utc_now())
    assert db.get_setting('cloudflare_tunnel') == {'tunnel_loopback_only': True}
    assert db.get_setting('auth') == {'trusted_proxies': ['127.0.0.1']}


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


def test_entry_stored_after_a_concurrent_write_is_not_served(tmp_path):
    """A read that races a write must never leave a permanently stale entry.

    ``get_setting`` reads the generation, queries, then stores. If a writer
    invalidates between the store decision and the store itself, a value read
    before that write would otherwise be memoised forever (nothing else clears
    it until the next write). Entries are therefore tagged with the generation
    they were read under and rejected once that generation has moved on.
    """
    db = _db(tmp_path)
    db.set_setting('objects', {'default_mode': 'moving'}, utc_now())
    db.get_setting('objects')  # populate the cache at the current generation

    # Reproduce the interleaving exactly: the entry lands in the dict tagged
    # with a generation that a writer has since superseded. ``invalidate``
    # popped the key in that window only if the store happened first -- the
    # bug was the store happening *after* the pop, so the entry survives.
    cache = db._settings_cache()
    stale_gen, _raw = cache['objects']
    db.__dict__['_settings_cache_gen'] = stale_gen + 1
    cache['objects'] = (stale_gen, '{"default_mode": "STALE"}')

    # Without generation tagging this returns STALE and keeps doing so until
    # the next write, because nothing else ever clears the entry.
    assert db.get_setting('objects') == {'default_mode': 'moving'}
