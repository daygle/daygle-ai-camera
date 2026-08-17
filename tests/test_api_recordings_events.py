"""API integration tests: recordings/events/alerts listing, labels, retention, timeline, and stats endpoints.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_alerted_only_event_and_recording_queries(tmp_path):
    from app.database import EventDatabase

    database = EventDatabase(str(tmp_path / 'events.sqlite3'))
    now = '2026-06-06T00:00:00+00:00'
    events = [
        database.add_event(
            created_at=f'2026-06-06T00:0{index}:00+00:00',
            source='camera',
            snapshot_path=None,
            detections=[{'label': label, 'confidence': 0.9, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
            alert_triggered=has_alert,
        )
        for index, (label, has_alert) in enumerate([('cat', False), ('dog', True), ('person', True)], start=1)
    ]
    for event_id, label, has_alert in zip(events, ['cat', 'dog', 'person'], [False, True, True]):
        database.add_recording(
            event_id=event_id,
            camera_id='front',
            started_at=f'2026-06-06T00:1{event_id}:00+00:00',
            ended_at=f'2026-06-06T00:1{event_id}:05+00:00',
            duration_seconds=5,
            file_path=str(tmp_path / f'{label}.mp4'),
            thumbnail_path=None,
            source='camera',
            created_at=now,
            trigger_type='object',
            trigger_label=label,
        )
        if has_alert:
            database.add_alert(now, f'zone__obj__{label}', event_id, label, 0.9, f'{label} matched')

    assert [event['id'] for event in database.search_events()] == list(reversed(events))
    assert [event['id'] for event in database.search_events(alerted_only=True)] == [events[2], events[1]]
    assert database.search_events(label='cat', alerted_only=True) == []
    assert [event['id'] for event in database.search_events(label='dog', alerted_only=True)] == [events[1]]
    assert [recording['event_id'] for recording in database.list_recordings(alerted_only=True)] == [events[2], events[1]]
    assert [recording['event_id'] for recording in database.list_recordings(label='person', alerted_only=True)] == [events[2]]


def test_alerts_endpoint_exposes_event_id_for_grouping(tmp_path):
    """The /api/alerts payload must include event_id on every row so the
    dashboard can collapse multiple rules that fired for the same event into
    a single card with a label chip set."""
    from app.database import EventDatabase

    database = EventDatabase(str(tmp_path / 'alerts.sqlite3'))
    event_id = database.add_event(
        created_at='2026-06-07T00:00:00+00:00',
        source='camera',
        snapshot_path=None,
        detections=[{'label': 'cat', 'confidence': 0.9, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
        alert_triggered=True,
    )
    # Two alert rules fire for the same event (cat + person).
    for label in ('cat', 'person'):
        database.add_alert(
            created_at='2026-06-07T00:00:01+00:00',
            rule_name=f'Front Door / Zone / {label}',
            event_id=event_id,
            label=label,
            confidence=0.9,
            message=f'{label} matched',
        )

    # Mirror the join the /api/alerts endpoint performs.
    with database.connect() as db:
        rows = db.execute(
            "SELECT ah.*, r.id AS recording_id FROM alert_history ah "
            "LEFT JOIN recordings r ON r.id = ah.recording_id "
            "ORDER BY ah.created_at DESC LIMIT 25"
        ).fetchall()
    alerts = [dict(row) for row in rows]
    assert len(alerts) == 2
    for alert in alerts:
        assert alert['event_id'] == event_id, 'alerts must carry event_id for frontend grouping'

    # Frontend grouping: collapse by event_id, collect unique labels.
    groups = {}
    for alert in alerts:
        groups.setdefault(alert['event_id'], set()).add(alert['label'])
    assert groups == {event_id: {'cat', 'person'}}


def test_event_recordings_include_alert_history_recording_links(tmp_path):
    """A sound event can extend an existing RTSP recording instead of owning a
    new recording row. The alert history row still carries the recording_id, so
    event APIs must surface that clip as linked footage for the event.
    """
    from app.database import EventDatabase

    database = EventDatabase(str(tmp_path / 'sound-alert-recording.sqlite3'))
    now = '2026-06-07T00:00:00+00:00'
    original_event_id = database.add_event(
        created_at=now,
        source='rtsp',
        snapshot_path=None,
        detections=[{'label': 'person', 'confidence': 0.9, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
        alert_triggered=True,
    )
    recording_id = database.add_recording(
        event_id=original_event_id,
        camera_id='driveway',
        started_at=now,
        ended_at='2026-06-07T00:00:30+00:00',
        duration_seconds=30,
        file_path=str(tmp_path / 'active-recording.mp4'),
        thumbnail_path=None,
        source='rtsp',
        created_at=now,
        trigger_type='alert',
        trigger_label='person',
        labels=['person'],
    )
    sound_event_id = database.add_event(
        created_at='2026-06-07T00:00:10+00:00',
        source='sound',
        snapshot_path=None,
        detections=[],
        alert_triggered=True,
        metadata={'label': 'cat_meow', 'class_label': 'Cat Meow', 'camera_id': 'driveway'},
    )
    database.add_alert(
        created_at='2026-06-07T00:00:10+00:00',
        rule_name='Cat Meow',
        event_id=sound_event_id,
        label='cat_meow',
        confidence=0.98,
        message='Cat Meow detected (98% confidence)',
        recording_id=recording_id,
    )

    sound_event = database.get_event(sound_event_id)
    assert sound_event is not None
    assert sound_event['recording_status'] == 'linked'
    assert [recording['id'] for recording in sound_event['recordings']] == [recording_id]

    events_with_recordings = database.search_events(with_recording=True)
    assert sound_event_id in [event['id'] for event in events_with_recordings]


def test_recording_labels_join_table_round_trip(tmp_path):
    from app.database import EventDatabase

    database = EventDatabase(str(tmp_path / 'events.sqlite3'))
    now = '2026-06-06T00:00:00+00:00'
    # A single event whose detections include BOTH cat and person. The
    # recording's trigger_label is the first alert-triggered detection (cat),
    # but recording_labels must carry every label that appeared inside the clip.
    event_id = database.add_event(
        created_at=now,
        source='camera',
        snapshot_path=None,
        detections=[
            {'label': 'cat', 'confidence': 0.9, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}},
            {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.1, 'y': 0.1, 'width': 1, 'height': 1}},
        ],
        alert_triggered=True,
    )
    recording_id = database.add_recording(
        event_id=event_id,
        camera_id='front',
        started_at='2026-06-06T00:10:00+00:00',
        ended_at='2026-06-06T00:10:05+00:00',
        duration_seconds=5,
        file_path=str(tmp_path / 'cat-person.mp4'),
        thumbnail_path=None,
        source='camera',
        created_at=now,
        trigger_type='alert',
        trigger_label='cat',
        labels=['cat', 'person'],
    )

    # Multi-label set is returned via the list endpoint.
    recordings = database.list_recordings()
    assert len(recordings) == 1
    assert recordings[0]['trigger_label'] == 'cat'
    assert recordings[0]['labels'] == ['cat', 'person']

    # The Object Label filter should now match a recording on EITHER of its
    # labels, not just the trigger_label.
    assert [r['id'] for r in database.list_recordings(label='cat')] == [recording_id]
    assert [r['id'] for r in database.list_recordings(label='person')] == [recording_id]
    assert database.list_recordings(label='dog') == []

    # add_recording_labels merges (no duplicates) and tracks the source.
    new_total = database.add_recording_labels(recording_id, ['person', 'dog', '  Cat  '], source='extension')
    assert new_total == 1  # 'dog' was new; 'person' and 'cat' (re-cased) were already present
    recordings = database.list_recordings()
    assert recordings[0]['labels'] == ['cat', 'dog', 'person']

    # Deleting the recording cascades to recording_labels.
    database.delete_recording(recording_id)
    assert database.list_recordings() == []


def test_recording_labels_api_filter_matches_any_recorded_label(tmp_path, monkeypatch):
    """Confirm /api/recordings?label=... matches any label persisted in recording_labels,
    not just the single trigger_label column."""
    app, database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.recording_extension as _re

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None
        def detect_image(self, _image_bytes, confidence=None):
            return []

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        _login(admin)
        event_time = datetime.now(timezone.utc).isoformat()
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'test.png')
        # Footage-style event: detections include BOTH cat and person, but the
        # recording's trigger_label is 'cat' (first alert-triggered detection).
        detections = [
            {'label': 'cat', 'confidence': 0.9, 'alert_triggered': True, 'box': {'x': 0.0, 'y': 0.0, 'width': 0.5, 'height': 0.5}},
            {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.2, 'y': 0.2, 'width': 0.5, 'height': 0.5}},
        ]
        event_id = main.database.add_event(
            created_at=event_time,
            source='motion',
            snapshot_path=snapshot_path,
            detections=detections,
            alert_triggered=True,
            metadata={'camera_id': 'front', 'camera_name': 'Front'},
        )
        recording_id = _re.attach_event_recording(event_id, event_time, 'upload', detections)
        assert recording_id is not None

        # The recording was tagged 'cat' as the trigger, but the join table
        # also contains 'person'. The /api/recordings?label= filter must
        # surface the recording when filtering by EITHER label.
        status, _, all_recordings = admin.request('/api/recordings')
        assert status == 200
        assert len(all_recordings) == 1
        assert all_recordings[0]['trigger_label'] == 'cat'
        assert sorted(all_recordings[0]['labels']) == ['cat', 'person']

        status, _, cat_filter = admin.request('/api/recordings?label=cat')
        assert status == 200
        assert [r['id'] for r in cat_filter] == [recording_id]

        status, _, person_filter = admin.request('/api/recordings?label=person')
        assert status == 200
        assert [r['id'] for r in person_filter] == [recording_id]

        status, _, dog_filter = admin.request('/api/recordings?label=dog')
        assert status == 200
        assert dog_filter == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_event_snapshot_endpoint_serves_annotated_image(tmp_path, monkeypatch):
    """GET /api/events/{id}/snapshot returns the saved frame as a JPEG (with
    detection boxes), and events advertise availability via has_snapshot."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        _login(admin)
        event_time = datetime.now(timezone.utc).isoformat()
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'snap.png')
        event_id = main.database.add_event(
            created_at=event_time,
            source='motion',
            snapshot_path=snapshot_path,
            detections=[{'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.5, 'height': 0.5}}],
            alert_triggered=True,
            metadata={'camera_id': 'front'},
        )

        status, _, events = admin.request('/api/events')
        assert status == 200
        listed = next(event for event in events if event['id'] == event_id)
        assert listed['has_snapshot'] is True

        status, headers, body = admin.request(f'/api/events/{event_id}/snapshot')
        assert status == 200
        assert LocalClient.header(headers, 'Content-Type') == 'image/jpeg'
        assert isinstance(body, (bytes, bytearray)) and len(body) > 0

        # A frameless (sound) event has no snapshot: has_snapshot False + 404.
        sound_id = main.database.add_event(
            created_at=event_time, source='sound', snapshot_path=None, detections=[],
        )
        status, _, events2 = admin.request('/api/events')
        sound_event = next(event for event in events2 if event['id'] == sound_id)
        assert sound_event['has_snapshot'] is False
        status, _, _ = admin.request(f'/api/events/{sound_id}/snapshot')
        assert status == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_recording_labels_backfill_seeds_existing_recordings(tmp_path):
    from app.database import EventDatabase

    # Build a DB that mimics a pre-multi-label install: detection rows but no
    # recording_labels entries. EventDatabase.init() should backfill them from
    # the detections + trigger_label columns on first open.
    database = EventDatabase(str(tmp_path / 'legacy.sqlite3'))
    event_id = database.add_event(
        created_at='2026-06-06T00:00:00+00:00',
        source='camera',
        snapshot_path=None,
        detections=[
            {'label': 'cat', 'confidence': 0.9, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}},
            {'label': 'person', 'confidence': 0.85, 'box': {'x': 0.1, 'y': 0.1, 'width': 1, 'height': 1}},
        ],
        alert_triggered=True,
    )
    # Mimic an old install by inserting the recording row without labels, then
    # nuking any auto-created recording_labels so the backfill has work to do.
    recording_id = database.add_recording(
        event_id=event_id,
        camera_id='front',
        started_at='2026-06-06T00:10:00+00:00',
        ended_at='2026-06-06T00:10:05+00:00',
        duration_seconds=5,
        file_path=str(tmp_path / 'legacy.mp4'),
        thumbnail_path=None,
        source='camera',
        created_at='2026-06-06T00:00:00+00:00',
        trigger_type='alert',
        trigger_label='cat',
    )
    with database.connect() as db:
        db.execute("DELETE FROM recording_labels WHERE recording_id = ?", (recording_id,))

    # Re-open the database - init() should re-seed recording_labels from the
    # existing detections and trigger_label.
    reopened = EventDatabase(str(tmp_path / 'legacy.sqlite3'))
    recording = reopened.list_recordings()[0]
    assert recording['labels'] == ['cat', 'person']


def test_event_linked_recording_metadata_listing_stream_and_delete_permissions(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.recording_extension as _re

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image_bytes, confidence=None):
            return [{'label': 'cat', 'confidence': 0.91, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        admin_csrf = _login(admin)
        status, _headers, viewer = admin.request(
            '/api/users',
            method='POST',
            json_body={'username': 'clipviewer', 'password': 'Viewer123!', 'role': 'viewer'},
            headers={'X-CSRF-Token': admin_csrf},
        )
        assert status == 200

        detections = [{'label': 'cat', 'confidence': 0.91, 'alert_triggered': True, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]
        event_time = datetime.now(timezone.utc).isoformat()
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'test.png')
        event_id = main.database.add_event(
            created_at=event_time,
            source='motion',
            snapshot_path=snapshot_path,
            detections=detections,
            alert_triggered=False,
            metadata={},
        )
        recording_id = _re.attach_event_recording(event_id, event_time, 'upload', detections)
        assert recording_id is not None

        status, _headers, recordings = admin.request('/api/recordings')
        assert status == 200
        assert recordings[0]['id'] == recording_id
        assert recordings[0]['event_id'] == event_id
        assert recordings[0]['detections']
        assert recordings[0]['source'] == 'upload'
        assert recordings[0]['trigger_type'] in {'motion', 'human', 'object', 'continuous', 'alert'}
        media_path = Path(recordings[0]['file_path'])
        metadata_path = media_path.with_name(f'{media_path.name}.meta.json')
        assert media_path.exists() or metadata_path.exists()

        label = recordings[0]['detections'][0]['label']
        status, _headers, filtered = admin.request(f'/api/recordings?label={label}')
        assert status == 200
        assert any(recording['id'] == recording_id for recording in filtered)

        status, _headers, detail = admin.request(f"/api/recordings/{recording_id}")
        assert status == 200
        assert detail['event']['id'] == event_id
        event = admin.request(f"/api/events/{event_id}")[2]
        assert event['recording_status'] == 'linked'
        assert event['recordings'][0]['id'] == recording_id

        status, headers, stream_body = admin.request(f"/api/recordings/{recording_id}/stream")
        if media_path.exists():
            assert status == 200
            assert headers['content-type'].startswith('video/mp4')
        else:
            assert status == 404
            assert stream_body['detail'] == 'Recording media file not found'

        viewer_client = LocalClient(base_url)
        viewer_csrf = _login(viewer_client, viewer['username'], 'Viewer123!')
        assert viewer_client.request('/api/recordings')[0] == 200
        status, _headers, denied = viewer_client.request(
            f"/api/recordings/{recording_id}", method='DELETE', headers={'X-CSRF-Token': viewer_csrf}
        )
        assert status == 403
        assert denied['detail'] == 'Admin access required'

        status, _headers, deleted = admin.request(
            f"/api/recordings/{recording_id}", method='DELETE', headers={'X-CSRF-Token': admin_csrf}
        )
        assert status == 200
        assert deleted['ok'] is True
        assert admin.request(f"/api/recordings/{recording_id}")[0] == 404
        assert not media_path.exists()
        assert not metadata_path.exists()
        with sqlite3.connect(database_path) as db:
            count = db.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]
        assert count == 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_recording_retention_purge_deletes_metadata_and_files(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.recording_extension as _re

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _image_bytes, confidence=None):
            return [{'label': 'cat', 'confidence': 0.91, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        admin_csrf = _login(admin)
        detections = [{'label': 'cat', 'confidence': 0.91, 'alert_triggered': True, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]
        event_time = datetime.now(timezone.utc).isoformat()
        event_id = main.database.add_event(
            created_at=event_time,
            source='motion',
            snapshot_path=None,
            detections=detections,
            alert_triggered=False,
            metadata={},
        )
        recording_id = _re.attach_event_recording(event_id, event_time, 'upload', detections)
        assert recording_id is not None
        recording = admin.request(f"/api/recordings/{recording_id}")[2]
        file_path = Path(recording['file_path'])
        metadata_path = file_path.with_name(f'{file_path.name}.meta.json')
        assert file_path.exists() or metadata_path.exists()
        expected_media_files_deleted = int(file_path.exists())

        old_started = '2000-01-01T00:00:00+00:00'
        with sqlite3.connect(database_path) as db:
            db.execute("UPDATE recordings SET started_at = ?, ended_at = ? WHERE id = ?", (old_started, old_started, recording_id))
            db.commit()

        status, _headers, purged = admin.request('/api/recordings/purge', method='POST', headers={'X-CSRF-Token': admin_csrf})
        assert status == 200
        assert purged['purged'] == 1
        assert purged['files_deleted'] == expected_media_files_deleted
        assert not file_path.exists()
        assert not metadata_path.exists()
        assert admin.request(f"/api/recordings/{recording_id}")[0] == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_recordings_timeline_returns_camera_day_segments(tmp_path, monkeypatch):
    app, database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        target_day = '2026-06-07'
        started_at = f'{target_day}T08:15:00+00:00'
        ended_at = f'{target_day}T08:15:12+00:00'
        _login(admin)

        import app.main as main_module

        file_path = tmp_path / 'data' / 'recordings' / 'timeline-test.mp4'
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b'not-a-real-video')

        event_id = main_module.database.add_event(
            created_at=started_at,
            source='camera',
            snapshot_path=None,
            detections=[{'label': 'person', 'confidence': 0.99, 'box': {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.4}}],
            metadata={'camera_id': 'camera-1', 'camera_name': 'Primary Camera'},
        )
        recording_id = main_module.database.add_recording(
            event_id=event_id,
            camera_id='camera-1',
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=12.0,
            file_path=str(file_path),
            thumbnail_path=None,
            source='camera',
            created_at=started_at,
            trigger_type='human',
            trigger_label='person',
        )

        motion_started_at = f'{target_day}T08:30:00+00:00'
        motion_ended_at = f'{target_day}T08:30:10+00:00'
        motion_event_id = main_module.database.add_event(
            created_at=motion_started_at,
            source='camera',
            snapshot_path=None,
            detections=[{'label': 'person', 'confidence': 0.88, 'box': {'x': 0.15, 'y': 0.25, 'width': 0.2, 'height': 0.25}}],
            metadata={'camera_id': 'camera-1', 'camera_name': 'Primary Camera'},
        )
        motion_recording_id = main_module.database.add_recording(
            event_id=motion_event_id,
            camera_id='camera-1',
            started_at=motion_started_at,
            ended_at=motion_ended_at,
            duration_seconds=10.0,
            file_path=str(file_path.with_name('timeline-motion-test.mp4')),
            thumbnail_path=None,
            source='camera',
            created_at=motion_started_at,
            trigger_type='motion',
            trigger_label='person',
        )
        Path(str(file_path.with_name('timeline-motion-test.mp4'))).write_bytes(b'not-a-real-video')

        status, _headers, payload = admin.request(f'/api/recordings/timeline?camera_id=camera-1&day={target_day}')
        assert status == 200
        assert payload['camera']['id'] == 'camera-1'
        assert payload['day'] == target_day
        assert payload['cameras']
        assert len(payload['recordings']) == 2

        segment = next(recording for recording in payload['recordings'] if recording['id'] == recording_id)
        assert segment['id'] == recording_id
        assert segment['timeline_start_seconds'] == 8 * 3600 + 15 * 60
        assert segment['timeline_end_seconds'] == 8 * 3600 + 15 * 60 + 12
        assert segment['timeline_duration_seconds'] == 12
        assert segment['color_key'] == 'person'
        assert segment['event']['metadata']['camera_id'] == 'camera-1'

        motion_segment = next(recording for recording in payload['recordings'] if recording['id'] == motion_recording_id)
        assert motion_segment['color_key'] == 'motion'
        assert motion_segment['color_label'] == 'motion'

        status, _headers, local_payload = admin.request(
            f'/api/recordings/timeline?camera_id=camera-1&day={target_day}&tz_offset_minutes=-120'
        )
        assert status == 200
        local_segment = next(recording for recording in local_payload['recordings'] if recording['id'] == recording_id)
        assert local_segment['timeline_start_seconds'] == 10 * 3600 + 15 * 60
        assert local_payload['timeline_timezone_offset_minutes'] == -120

        status, _headers, empty_payload = admin.request('/api/recordings/timeline?camera_id=camera-1&day=2026-06-08')
        assert status == 200
        assert empty_payload['recordings'] == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_empty_detection_track_is_marker_only(tmp_path, monkeypatch):
    """An all-empty baked track marks the clip as analyzed (so it is not re-decoded)
    but must load as None so playback falls back to the static event box."""
    _load_app(tmp_path, monkeypatch)
    import app.recording_extension as _rex

    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'')
    _rex.write_recording_detection_track(clip, [{'t': 0.0, 'detections': []}, {'t': 0.2, 'detections': []}])
    assert _rex.recording_track_sidecar_path(clip).exists()
    assert _rex.load_recording_detection_track(clip) is None

    _rex.write_recording_detection_track(clip, [
        {'t': 0.0, 'detections': []},
        {'t': 0.2, 'detections': [{'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.4}}]},
    ])
    loaded = _rex.load_recording_detection_track(clip)
    assert loaded is not None and len(loaded) == 2


def test_build_track_from_live_history_slices_capture_window(tmp_path, monkeypatch):
    """Recording tracks are sliced from the live monitor's in-memory detection
    history - no clip decoding, no re-inference - with timestamps rebased onto
    the capture window."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.detection_state as _ds
    from collections import deque

    now = time.time()
    box = {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.4}
    main._state.live_detection_history['camera-1'] = deque(
        [
            (now - 10.0, [{'label': 'person', 'confidence': 0.9, 'box': box}]),   # before window
            (now - 4.0, [{'label': 'person', 'confidence': 0.91, 'box': box}]),
            (now - 2.0, []),                                                       # empty cycle inside window
            (now + 5.0, [{'label': 'cat', 'confidence': 0.8, 'box': box}]),        # after window
        ],
        maxlen=1200,
    )

    track = _ds.build_track_from_live_history('camera-1', now - 5.0, now)
    assert track is not None
    assert [sample['t'] for sample in track] == [1.0, 3.0]
    assert track[0]['detections'][0]['label'] == 'person'
    # Empty cycles are kept so playback clears boxes after the object leaves.
    assert track[1]['detections'] == []

    assert _ds.build_track_from_live_history('camera-1', now + 100, now + 110) is None
    assert _ds.build_track_from_live_history('other-camera', now - 5.0, now) is None
    assert _ds.build_track_from_live_history(None, now - 5.0, now) is None


def test_live_monitor_populates_detection_history(tmp_path, monkeypatch):
    """Every live monitor cycle must append its detections to the per-camera
    history that recording tracks are sliced from."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.live_monitor as _lm

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return [{'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.4}}]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    # The frame fails the motion gate (diff None -> 'still'); keep the
    # historical any default so the history-tracking logic is what's tested.
    main.database.set_setting('objects', {'default_mode': 'any', 'labels': {}, 'still_alerts': {}}, main.utc_now())
    main._state.live_detection_last_checked.clear()
    main.alerts.last_triggered.clear()

    settings = _zone_camera_settings([
        {'label': 'person', 'record_on_detect': True, 'alert_on_detect': True, 'min_confidence': 0.5, 'cooldown_seconds': 0},
    ])
    before = time.time()
    _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)

    history = main._state.live_detection_history.get('camera-1')
    assert history, 'monitor cycle must be recorded in the detection history'
    sample_ts, sample_detections = history[-1]
    assert sample_ts >= before
    assert sample_detections[0]['label'] == 'person'
    assert sample_detections[0]['box']['width'] == pytest.approx(0.2)


@pytest.mark.parametrize('has_history_coverage,expect_track', [
    (True, True),
    (False, False),
])
def test_recording_detail_track_backfill(tmp_path, monkeypatch, has_history_coverage, expect_track):
    """When live history covers a recording's window, the detail view backfills
    a track sidecar synchronously. Without coverage, no track is generated."""
    # Migrated from direct ``main.recording_detail(...)`` to LocalClient: a
    # FastAPI ``Depends`` default only resolves inside the request lifecycle,
    # so calling the handler directly leaves ``db`` as a raw Depends sentinel.
    app, _db_path = _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.recording_extension as _rex
    from collections import deque
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        clip = tmp_path / 'data' / 'recordings' / ('event_backfill.mp4' if has_history_coverage else 'event_no_history.mp4')
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b'not-decoded')

        started = datetime.now(timezone.utc) - timedelta(seconds=12)
        ended = started + timedelta(seconds=8)
        box = {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.4}

        if has_history_coverage:
            main._state.live_detection_history['camera-1'] = deque(
                [(started.timestamp() + 2.0, [{'label': 'person', 'confidence': 0.9, 'box': box}])],
                maxlen=1200,
            )

        event_id = main.database.add_event(
            created_at=main.utc_now(), source='rtsp', snapshot_path=None,
            detections=[{'label': 'person', 'confidence': 0.9, 'box': box}],
            alert_triggered=True, metadata={},
        )
        recording_id = main.database.add_recording(
            event_id=event_id, camera_id='camera-1',
            started_at=started.isoformat(), ended_at=ended.isoformat(), duration_seconds=8.0,
            file_path=str(clip), thumbnail_path=None, source='rtsp',
            created_at=main.utc_now(), trigger_type='object', trigger_label='person',
        )

        status, _headers, detail = client.request(f'/api/recordings/{recording_id}')
        assert status == 200, f'recording_detail must succeed, got status {status}'
        if expect_track:
            assert _rex.recording_track_sidecar_path(clip).exists(), 'backfill must write the track sidecar'
            assert detail['track'], 'detail view must return the backfilled track'
            assert detail['track'][0]['t'] == pytest.approx(2.0, abs=0.01)
            assert detail['track'][0]['detections'][0]['label'] == 'person'
        else:
            assert detail['track'] is None
            assert not _rex.recording_track_sidecar_path(clip).exists()

        # Repeat views stay cheap and consistent.
        status, _headers, detail = client.request(f'/api/recordings/{recording_id}')
        assert status == 200
        if expect_track:
            assert detail['track']
        else:
            assert detail['track'] is None
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_delete_event_endpoint(tmp_path, monkeypatch):
    """DELETE /api/events/{event_id} removes an event."""
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        status, _headers, payload = client.request(
            "/api/detect/frame",
            method="POST",
            data=TEST_IMAGE_PNG,
            headers={"Content-Type": "image/png", "X-CSRF-Token": csrf},
        )
        assert status == 200
        event_id = payload.get("event_id")
        if event_id:
            status, _headers, deleted = client.request(
                f"/api/events/{event_id}",
                method="DELETE",
                headers={"X-CSRF-Token": csrf},
            )
            assert status == 200
            assert deleted.get("ok") is True
            status, _headers, events = client.request("/api/events")
            assert status == 200
            assert all(e["id"] != event_id for e in events)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_multi_object_recording_labels_and_trigger_type(tmp_path, monkeypatch):
    """Verify a recording with 3+ diverse object detections stores ALL labels
    in recording_labels, returns them in list/detail/timeline API responses,
    and maintains the correct trigger_type after the 'object' type change."""
    app, database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.recording_extension as _re

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None
        def detect_image(self, _image_bytes, confidence=None):
            return []

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        _login(admin)

        # Create event with 3 diverse object detections
        detections = [
            {'label': 'person', 'confidence': 0.92, 'alert_triggered': True, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.4}},
            {'label': 'cat', 'confidence': 0.78, 'alert_triggered': True, 'box': {'x': 0.5, 'y': 0.5, 'width': 0.2, 'height': 0.2}},
            {'label': 'dog', 'confidence': 0.45, 'alert_triggered': True, 'box': {'x': 0.3, 'y': 0.3, 'width': 0.25, 'height': 0.3}},
        ]
        event_time = datetime.now(timezone.utc).isoformat()
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'test.png')
        event_id = main.database.add_event(
            created_at=event_time,
            source='motion',
            snapshot_path=snapshot_path,
            detections=detections,
            alert_triggered=True,
            metadata={'camera_id': 'front', 'camera_name': 'Front'},
        )

        # Attach recording - should store ALL labels in recording_labels
        recording_id = _re.attach_event_recording(event_id, event_time, 'upload', detections)
        assert recording_id is not None

        # Verify recording list endpoint
        status, _, recordings = admin.request('/api/recordings')
        assert status == 200
        assert len(recordings) >= 1
        recording = next(r for r in recordings if r['id'] == recording_id)

        # trigger_label should be the first alert-triggered detection (person)
        assert recording['trigger_label'] == 'person', f'Expected person, got {recording["trigger_label"]}'
        # trigger_type is 'alert' since recordings are gated per-rule via alert_triggered
        assert recording['trigger_type'] == 'alert', f'Expected alert, got {recording["trigger_type"]}'
        # labels must contain ALL non-generic detections
        assert sorted(recording['labels']) == ['cat', 'dog', 'person'], f'Got {sorted(recording["labels"])}'
        # The list endpoint must expose a confidence for EVERY detected object so
        # secondary objects render a real percentage instead of a misleading 0%.
        list_confidences = recording.get('label_confidences') or {}
        assert round(list_confidences.get('person'), 2) == 0.92
        assert round(list_confidences.get('cat'), 2) == 0.78
        assert round(list_confidences.get('dog'), 2) == 0.45

        # Verify recording detail endpoint
        status, _, detail = admin.request(f'/api/recordings/{recording_id}')
        assert status == 200
        assert sorted(detail['labels']) == ['cat', 'dog', 'person'], f'Got {sorted(detail["labels"])}'
        detail_confidences = detail.get('label_confidences') or {}
        assert round(detail_confidences.get('dog'), 2) == 0.45

        # Verify filtering by EACH label works
        for label in ('cat', 'dog', 'person'):
            status, _, filtered = admin.request(f'/api/recordings?label={label}')
            assert status == 200
            assert any(r['id'] == recording_id for r in filtered), f'Recording should match label={label}'

        # Verify filtering by non-existent label returns empty
        status, _, unknown_filter = admin.request('/api/recordings?label=elephant')
        assert status == 200
        assert not any(r['id'] == recording_id for r in unknown_filter)

        # Verify timeline endpoint returns correct color_key
        target_day = event_time[:10]
        status, _, timeline = admin.request(f'/api/recordings/timeline?camera_id=front&day={target_day}')
        assert status == 200
        timeline_segment = next((s for s in timeline.get('recordings', []) if s['id'] == recording_id), None)
        if timeline_segment:
            assert timeline_segment['color_key'] == 'person'
            assert timeline_segment['color_label'] == 'person'

        # Verify extend_active_rtsp_recording adds new labels without duplicates
        now = datetime.now(timezone.utc)
        file_path = tmp_path / 'data' / 'recordings' / 'extend-multi.mp4'
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b'placeholder')

        ext_recording_id = main.database.add_recording(
            event_id=None,
            camera_id='camera-1',
            started_at=(now - timedelta(seconds=5)).isoformat(),
            ended_at=now.isoformat(),
            duration_seconds=5.0,
            file_path=str(file_path),
            thumbnail_path=None,
            source='rtsp',
            created_at=now.isoformat(),
            trigger_type='motion',
            trigger_label='motion',
        )

        with main._state.active_rtsp_recordings_lock:
            main._state.active_rtsp_recordings['camera-1'] = {
                'recording_id': ext_recording_id,
                'start_capture_ts': (now - timedelta(seconds=5)).timestamp(),
                'capture_deadline_ts': now.timestamp(),
                'max_capture_deadline_ts': (now + timedelta(seconds=20)).timestamp(),
            }

        # Extend with new detections that include a NEW label (bicycle) + existing dog
        extended_id = _re.extend_active_rtsp_recording(
            camera_id='camera-1',
            event_time=now.isoformat(),
            recording_config={'extension_step_seconds': 10},
            detections=[
                {'label': 'bicycle', 'confidence': 0.85, 'alert_triggered': True},
                {'label': 'dog', 'confidence': 0.75, 'alert_triggered': True},
            ],
        )
        assert extended_id == ext_recording_id

        updated_ext = main.database.get_recording(ext_recording_id)
        assert updated_ext is not None
        assert 'bicycle' in updated_ext['labels']
        assert 'dog' in updated_ext['labels']
        # Confidence captured during extension must be persisted so the list and
        # timeline can show a percentage for objects added after the trigger.
        ext_confidences = updated_ext.get('label_confidences') or {}
        assert round(ext_confidences.get('bicycle'), 2) == 0.85
        assert round(ext_confidences.get('dog'), 2) == 0.75

        with main._state.active_rtsp_recordings_lock:
            main._state.active_rtsp_recordings.pop('camera-1', None)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_stats_with_since_filter_builds_valid_sql(tmp_path):
    """Regression: /api/stats?since=... must not raise sqlite3.OperationalError.

    Two bugs made ``stats(since=...)`` crash in production:
      1. the ``total_alerts`` query had no WHERE, so the shared ``AND ...``
         since-clause produced ``FROM alert_history ah AND ...`` -> "near
         AND: syntax error";
      2. ``since_clause_det`` referenced a non-existent alias ``e2``, so the
         objects/labels query raised "no such column: e2.created_at".
    Both only fire when ``since`` is provided. This locks down the fix.
    """
    from app.database import EventDatabase

    database = EventDatabase(str(tmp_path / 'stats-since.sqlite3'))
    event_id = database.add_event(
        created_at='2026-07-27T00:00:00+00:00',
        source='camera',
        snapshot_path=None,
        detections=[{'label': 'person', 'confidence': 0.9, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
        alert_triggered=True,
    )
    database.add_alert(
        created_at='2026-07-27T00:00:01+00:00',
        rule_name='Front Door / person',
        event_id=event_id,
        label='person',
        confidence=0.9,
        message='person matched',
    )

    # since set: exercises the previously-broken code paths.
    result = database.stats(since='2026-07-27')
    assert result['total_events'] == 1
    assert result['total_alerts'] == 1
    assert result['objects'] == [{'label': 'person', 'count': 1, 'max_confidence': 0.9}]

    # A since that post-dates the rows must filter them all out (still valid SQL).
    empty = database.stats(since='2026-07-28')
    assert empty['total_events'] == 0
    assert empty['total_alerts'] == 0
    assert empty['objects'] == []

    # No since: the clauses collapse to empty strings.
    unfiltered = database.stats()
    assert unfiltered['total_events'] == 1
    assert unfiltered['total_alerts'] == 1


def test_alerts_events_stats_accept_local_day_start_since_bound(tmp_path):
    """Regression: the alerts page "Today" filter showed only 1 of 6 alerts.

    The frontend sends the START OF THE LOCAL DAY as the `since` bound (e.g.
    '2026-07-31T18:30:00.000Z' for a UTC+5:30 operator at local midnight on
    Aug 1). Rows are stored as canonical UTC '+00:00' timestamps, so the
    bound must be normalised to that same form before SQLite's lexical
    comparison: a raw Z-suffix bound would sort the exact-midnight row
    ('2026-07-31T18:30:00.000000+00:00') BEFORE the bound ('...000Z') --
    '.' (0x2E) sorts before 'Z' (0x5A) -- and silently drop it, the 1-of-6
    symptom. The old frontend also sent the bare UTC date string
    ('2026-08-01'), which misses every alert whose UTC date is still the
    previous day.
    """
    from app.database import EventDatabase

    database = EventDatabase(str(tmp_path / 'daygle-since.sqlite3'))

    # An alert fired exactly at local midnight for a UTC+5:30 operator on
    # Aug 1: the stored UTC instant is 18:30 on Jul 31.
    event_id = database.add_event(
        created_at='2026-07-31T18:30:00+00:00',
        source='camera',
        snapshot_path=None,
        detections=[{'label': 'person', 'confidence': 0.9, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
        alert_triggered=True,
    )
    database.add_alert(
        created_at='2026-07-31T18:30:00.000000+00:00',
        rule_name='Front Door / person',
        event_id=event_id,
        label='person',
        confidence=0.9,
        message='person matched',
    )
    # One second BEFORE local midnight: must stay excluded.
    early_event = database.add_event(
        created_at='2026-07-31T18:29:59+00:00',
        source='camera',
        snapshot_path=None,
        detections=[{'label': 'cat', 'confidence': 0.7, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
        alert_triggered=True,
    )
    database.add_alert(
        created_at='2026-07-31T18:29:59.000000+00:00',
        rule_name='Side Door / cat',
        event_id=early_event,
        label='cat',
        confidence=0.7,
        message='cat matched',
    )

    # The fixed frontend bound (local day start, Z suffix from Date.toISOString()).
    since = '2026-07-31T18:30:00.000Z'
    alerts = database.alerts(limit=10, since=since)
    assert [a['label'] for a in alerts] == ['person'], 'exact-midnight alert must be included'
    events = database.search_events(limit=10, since=since)
    assert [e['id'] for e in events] == [event_id]
    stats = database.stats(since=since)
    assert stats['total_alerts'] == 1
    assert stats['total_events'] == 1

    # The OLD frontend sent the UTC date string; that bound misses the
    # midnight row entirely (its UTC date is still Jul 31) -- the bug.
    assert database.alerts(limit=10, since='2026-08-01') == []
