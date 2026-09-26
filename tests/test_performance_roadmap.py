from __future__ import annotations

import threading
from types import SimpleNamespace

from app.camera_config import normalize_camera_settings
from app.camera_policy import camera_policy, clear_camera_policy_cache
from app.database import EventDatabase
from app.runtime_config import cached_snapshot, clear_runtime_config_cache


def test_runtime_config_cache_uses_generation_and_defensive_copies() -> None:
    class SettingsStore:
        _settings_cache_gen = 3

    store = SettingsStore()
    clear_runtime_config_cache()
    calls = 0

    def build() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {'nested': {'value': 1}}

    first = cached_snapshot(store, 'ai', build)
    first['nested']['value'] = 99  # type: ignore[index]
    second = cached_snapshot(store, 'ai', build)

    assert calls == 1
    assert second == {'nested': {'value': 1}}

    store._settings_cache_gen += 1
    assert cached_snapshot(store, 'ai', build) == {'nested': {'value': 1}}
    assert calls == 2
    clear_runtime_config_cache()


def test_compiled_camera_policy_normalizes_and_excludes_disabled_zones() -> None:
    clear_camera_policy_cache()
    settings = normalize_camera_settings({
        'id': 'front',
        'detection': {
            'object_labels': [' Human ', 'PERSON'],
            'zones': [
                {
                    'id': 'driveway',
                    'name': 'Driveway',
                    'x': 0.1,
                    'y': 0.1,
                    'width': 0.4,
                    'height': 0.4,
                    'object_labels': [' Car '],
                    'object_rules': [
                        {'label': 'Car', 'enabled': True},
                        {'label': 'person', 'enabled': False},
                    ],
                },
                {
                    'id': 'disabled',
                    'enabled': False,
                    'x': 0,
                    'y': 0,
                    'width': 1,
                    'height': 1,
                    'object_rules': [{'label': 'person'}],
                },
            ],
        },
    })

    policy = camera_policy(settings)

    assert policy.camera_labels == frozenset({'person'})
    assert len(policy.zones) == 1
    assert policy.zones[0].object_labels == frozenset({'car', 'person'})
    assert len(policy.zones[0].rules) == 1
    assert policy.zones[0].bounds == (0.1, 0.1, 0.4, 0.4)


def test_compiled_zone_broad_phase_preserves_detection_boundaries() -> None:
    from app.zone_detection import detection_matches_zone, filter_detections_for_camera_zones

    settings = normalize_camera_settings({
        'id': 'front',
        'detection': {
            'object_labels': ['person'],
            'zones': [{
                'id': 'left',
                'name': 'Left',
                'x': 0,
                'y': 0,
                'width': 0.5,
                'height': 0.5,
                'object_labels': ['person'],
                'object_rules': [{'label': 'person', 'min_confidence': 0.5}],
            }],
        },
    })

    inside = {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.2, 'y': 0.2, 'width': 0.1, 'height': 0.1}}
    outside = {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.6, 'y': 0.6, 'width': 0.1, 'height': 0.1}}

    assert detection_matches_zone(inside, settings['detection']['zones'][0])
    assert not detection_matches_zone(outside, settings['detection']['zones'][0])
    filtered = filter_detections_for_camera_zones(
        [inside, outside], settings, zone_monitor_key='monitor_objects',
    )
    assert [detection['label'] for detection in filtered] == ['person']
    assert filtered[0]['box'] == inside['box']


def test_event_alert_insert_is_atomic_and_recording_link_propagates(tmp_path) -> None:
    database = EventDatabase(str(tmp_path / 'events.sqlite3'))
    event_id = database.add_event_with_alerts(
        created_at='2026-01-01T00:00:00+00:00',
        source='behaviour',
        snapshot_path=None,
        detections=[],
        alerts=[{
            'created_at': '2026-01-01T00:00:00+00:00',
            'rule_name': 'Gate',
            'label': 'person',
            'confidence': 0.9,
            'message': 'Person crossed',
        }],
        alert_triggered=True,
        metadata={},
    )
    recording_id = database.add_recording(
        event_id=event_id,
        camera_id='front',
        started_at='2026-01-01T00:00:00+00:00',
        ended_at='2026-01-01T00:00:10+00:00',
        duration_seconds=10,
        file_path='missing.mp4',
        thumbnail_path=None,
        source='rtsp',
        created_at='2026-01-01T00:00:10+00:00',
    )

    assert database.set_event_recording(event_id, recording_id)
    with database.connect() as connection:
        row = connection.execute(
            'SELECT e.recording_id AS event_recording, ah.recording_id AS alert_recording '
            'FROM events e JOIN alert_history ah ON ah.event_id = e.id WHERE e.id = ?',
            (event_id,),
        ).fetchone()
    assert row['event_recording'] == recording_id
    assert row['alert_recording'] == recording_id

    with database.connect() as connection:
        before_invalid = {
            table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            for table in ('events', 'detections', 'alert_history')
        }
    invalid_event = {
        'created_at': '2026-01-01T00:01:00+00:00',
        'source': 'behaviour',
        'snapshot_path': None,
        'detections': [{
            'label': 'person',
            'confidence': 'not-a-number',
            'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1},
        }],
        'alerts': [{
            'rule_name': 'Broken',
            'label': 'person',
            'confidence': 0.9,
            'message': 'must roll back',
        }],
        'metadata': {},
    }
    try:
        database.add_event_with_alerts(**invalid_event)
    except (TypeError, ValueError):
        pass
    else:
        raise AssertionError('invalid detection confidence should fail')

    with database.connect() as connection:
        after_invalid = {
            table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            for table in ('events', 'detections', 'alert_history')
        }
    assert after_invalid == before_invalid


def test_automatic_recording_retention_is_coalesced_off_calling_thread(monkeypatch) -> None:
    import app.backup as backup

    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    calls: list[int] = []

    def fake_purge() -> None:
        calls.append(1)
        started.set()
        release.wait(timeout=2)
        completed.set()

    monkeypatch.setattr(backup, 'purge_recordings_by_policy', fake_purge)
    monkeypatch.setattr(backup, '_automatic_retention_last_started', None)
    monkeypatch.setattr(backup, '_automatic_retention_running', False)

    assert backup.schedule_recordings_retention()
    assert started.wait(timeout=1)
    assert calls == [1]
    assert not backup.schedule_recordings_retention()

    release.set()
    assert completed.wait(timeout=1)
    # A completed sweep still coalesces new event/chunk triggers for its window.
    assert not backup.schedule_recordings_retention()


def test_size_retention_streams_compact_rows_and_skips_age_expired_file_stats(tmp_path, monkeypatch) -> None:
    import importlib

    # Other API tests reload app.* modules during collection/execution. Resolve
    # the DB facade and repository helper from the same current app namespace so
    # the monkeypatch intercepts the implementation this EventDatabase uses.
    recordings_repo = importlib.import_module('app.db.recordings')
    current_database = importlib.import_module('app.database').EventDatabase
    database = current_database(str(tmp_path / 'retention.sqlite3'))
    recordings = [
        ('2020-01-01T00:00:00+00:00', 'expired.mp4', 100),
        ('2026-01-01T00:00:00+00:00', 'first.mp4', 8),
        ('2026-01-02T00:00:00+00:00', 'second.mp4', 8),
    ]
    file_sizes = {name: size for _, name, size in recordings}
    statted: list[str] = []

    class _FakePath:
        def __init__(self, name: str):
            self.name = name

        def stat(self):
            statted.append(self.name)
            return SimpleNamespace(st_size=file_sizes[self.name])

    monkeypatch.setattr(
        recordings_repo,
        'safe_storage_path',
        lambda raw_path, **_kwargs: _FakePath(str(raw_path).rsplit('/', 1)[-1]),
    )
    ids = []
    for started_at, name, _size in recordings:
        ids.append(database.add_recording(
            event_id=None, camera_id='front', started_at=started_at,
            ended_at=started_at, duration_seconds=1, file_path=f'/recordings/{name}',
            thumbnail_path=None, source='camera', created_at=started_at,
        ))

    purged = database.purge_recordings(
        older_than='2025-01-01T00:00:00+00:00', max_storage_bytes=10,
    )

    assert {row['id'] for row in purged} == {ids[0], ids[1]}
    assert statted == ['second.mp4', 'first.mp4']


def test_database_maintenance_schema_has_target_indexes_and_wal(tmp_path) -> None:
    database = EventDatabase(str(tmp_path / 'maintenance.sqlite3'))
    expected_indexes = {
        'idx_events_dismissed_created',
        'idx_detections_event_label',
        'idx_alert_history_event_id',
        'idx_alert_history_recording',
        'idx_alert_history_dismissed_created',
        'idx_recordings_camera_started',
        'idx_recording_labels_label_recording',
        'idx_unknown_faces_status_created',
    }

    database.maintain()
    with database.connect() as connection:
        indexes = {
            row['name']
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
        journal_mode = connection.execute('PRAGMA journal_mode').fetchone()[0]
        alert_plan = connection.execute(
            'EXPLAIN QUERY PLAN SELECT * FROM alert_history '
            'WHERE dismissed = 0 ORDER BY created_at DESC LIMIT 25',
        ).fetchall()
        recording_plan = connection.execute(
            'EXPLAIN QUERY PLAN SELECT * FROM recordings '
            'WHERE camera_id = ? ORDER BY started_at DESC, id DESC LIMIT 25',
            ('front',),
        ).fetchall()

    assert expected_indexes <= indexes
    assert journal_mode.lower() == 'wal'
    assert any('idx_alert_history_dismissed_created' in row[3] for row in alert_plan)
    assert any('idx_recordings_camera_started' in row[3] for row in recording_plan)
