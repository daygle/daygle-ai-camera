"""The filters behind the shared library filter bar (web/library_filters.js).

Events, Snapshots and Recordings share one filter bar, so their list queries
must agree on what each filter means: keyword search, camera, the since/until
window, label, recognised face, alerted-only and newest/oldest order. The DB
tests pin each filter; the API test checks the routers forward them and that
/api/library/facets fills the Label and Face dropdowns for a time window.
"""

from __future__ import annotations

from tests.support import (
    TEST_IMAGE_PNG,
    LocalClient,
    _load_app,
    _login,
    _server,
    _setup_admin,
)


def _det(label, confidence=0.9, zone=None):
    detection = {'label': label, 'confidence': confidence, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}
    if zone:
        detection['zone_name'] = zone
    return detection


def _seed(db):
    """Three events on two cameras across two days; ids returned by name."""
    ids = {}
    ids['person_front'] = db.add_event(
        created_at='2026-06-01T08:00:00+00:00', source='rtsp', snapshot_path='a.jpg',
        detections=[_det('person', zone='Driveway')],
        metadata={
            'camera_id': 'front', 'camera_name': 'Front Door',
            'ai_description': {'text': 'A man in a red jacket walks up the path.', 'tags': ['delivery']},
            'face_identities': {'people': [{'person_id': 7, 'name': 'Alice'}], 'unknown': 0},
        },
    )
    ids['car_back'] = db.add_event(
        created_at='2026-06-02T09:00:00+00:00', source='rtsp', snapshot_path='b.jpg',
        detections=[_det('car')],
        metadata={'camera_id': 'back', 'camera_name': 'Back Yard', 'face_identities': {'people': [], 'unknown': 2}},
    )
    ids['motion_front'] = db.add_event(
        created_at='2026-06-02T10:00:00+00:00', source='rtsp', snapshot_path=None,
        detections=[_det('motion')],
        metadata={'camera_id': 'front', 'camera_name': 'Front Door'},
    )
    return ids


def _event_ids(db, **kwargs):
    items, _cursor = db.search_events_page(limit=50, **kwargs)
    return [item['id'] for item in items]


def test_event_keyword_search_matches_labels_zones_cameras_ai_text_and_faces(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    ids = _seed(db)
    assert _event_ids(db, query='person') == [ids['person_front']]
    assert _event_ids(db, query='driveway') == [ids['person_front']], 'zone names are searchable'
    assert _event_ids(db, query='back yard') == [ids['car_back']], 'every word must match (camera name)'
    assert _event_ids(db, query='RED jacket') == [ids['person_front']], 'AI description, case-insensitive'
    assert _event_ids(db, query='delivery') == [ids['person_front']], 'AI tags'
    assert _event_ids(db, query='alice') == [ids['person_front']], 'recognised face names'
    assert _event_ids(db, query='person car') == [], 'words AND together'
    # LIKE wildcards in the query are literal, not patterns.
    assert _event_ids(db, query='%') == []
    # "camera" must not match every row through the metadata key names.
    assert _event_ids(db, query='camera') == []


def test_event_filters_camera_window_face_and_sort(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    ids = _seed(db)
    assert _event_ids(db, camera_id='front') == [ids['motion_front'], ids['person_front']]
    assert _event_ids(db, until='2026-06-01T23:59:59Z') == [ids['person_front']]
    assert _event_ids(db, since='2026-06-02T00:00:00Z', until='2026-06-02T09:30:00Z') == [ids['car_back']]
    assert _event_ids(db, face='id:7') == [ids['person_front']]
    assert _event_ids(db, face='unknown') == [ids['car_back']]
    assert sorted(_event_ids(db, face='any')) == sorted([ids['person_front'], ids['car_back']])
    assert _event_ids(db, face='name:nobody') == []
    assert _event_ids(db, sort='oldest') == [ids['person_front'], ids['car_back'], ids['motion_front']]


def test_oldest_first_cursor_pages_through_every_row_once(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    ids = _seed(db)
    seen = []
    cursor = None
    while True:
        items, cursor = db.search_events_page(limit=1, sort='oldest', cursor=cursor)
        seen.extend(item['id'] for item in items)
        if cursor is None:
            break
    assert seen == [ids['person_front'], ids['car_back'], ids['motion_front']]


def test_snapshot_list_takes_the_same_filters(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    ids = _seed(db)

    def snapshot_ids(**kwargs):
        items, _cursor = db.list_snapshots_page(limit=50, **kwargs)
        return [item['id'] for item in items]

    # The motion event saved no frame, so it never appears here.
    assert snapshot_ids() == [ids['car_back'], ids['person_front']]
    assert snapshot_ids(label='car') == [ids['car_back']]
    assert snapshot_ids(camera_id='front') == [ids['person_front']]
    assert snapshot_ids(query='jacket') == [ids['person_front']]
    assert snapshot_ids(sort='oldest') == [ids['person_front'], ids['car_back']]


def test_malformed_metadata_never_breaks_the_filter_sql(tmp_path):
    """json_extract raises on a malformed blob; the filters guard every call so
    one bad row cannot fail a whole search."""
    from app.database import EventDatabase
    from app.db.library_filters import event_camera_condition, event_face_condition, event_query_condition

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    ids = _seed(db)
    with db.connect() as conn:
        conn.execute("UPDATE events SET metadata = '{not json' WHERE id = ?", (ids['car_back'],))

        def matching(condition):
            sql, params = condition
            return [row['id'] for row in conn.execute(f'SELECT e.id FROM events e WHERE {sql} ORDER BY e.id', params)]

        assert matching(event_query_condition('e', 'car')) == [ids['car_back']], 'the detection label still matches'
        assert matching(event_camera_condition('e', 'back')) == []
        assert ids['car_back'] not in matching(event_face_condition('e', 'any'))


def test_recording_keyword_and_face_filters_follow_linked_events(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    ids = _seed(db)

    def add_recording(event_id, camera_id, started):
        return db.add_recording(
            event_id=event_id, camera_id=camera_id, started_at=started, ended_at=started,
            duration_seconds=10, file_path=str(tmp_path / f'{event_id}.mp4'), thumbnail_path=None,
            source='rtsp', created_at=started, trigger_type='object',
        )

    front = add_recording(ids['person_front'], 'front', '2026-06-01T08:00:00+00:00')
    back = add_recording(ids['car_back'], 'back', '2026-06-02T09:00:00+00:00')

    def recording_ids(**kwargs):
        items, _cursor = db.list_recordings_page(limit=50, **kwargs)
        return [item['id'] for item in items]

    assert recording_ids(query='jacket') == [front], 'searches the linked event'
    assert recording_ids(query='back') == [back], 'searches the camera id'
    assert recording_ids(face='id:7') == [front]
    assert recording_ids(face='unknown') == [back]


def test_facets_list_labels_and_faces_for_the_window(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    _seed(db)
    facets = db.library_facets('events')
    assert {item['value']: item['count'] for item in facets['labels']} == {'car': 1, 'motion': 1, 'person': 1}
    assert facets['faces']['people'] == [{'value': 'id:7', 'name': 'Alice', 'count': 1}]
    assert facets['faces']['unknown'] == 1
    day_two = db.library_facets('events', since='2026-06-02T00:00:00Z')
    assert {item['value'] for item in day_two['labels']} == {'car', 'motion'}
    assert day_two['faces']['people'] == []
    snapshots = db.library_facets('snapshots')
    assert {item['value'] for item in snapshots['labels']} == {'car', 'person'}


def test_api_forwards_filters_and_serves_facets(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    import app.main as main

    server, thread, base_url = _server(app)
    admin = LocalClient(base_url)
    try:
        _setup_admin(admin)
        _login(admin)
        snapshot_path = main.storage.save_image_snapshot(TEST_IMAGE_PNG, 'snap.png')
        person = main.database.add_event(
            created_at='2026-06-01T08:00:00+00:00', source='rtsp', snapshot_path=snapshot_path,
            detections=[_det('person')], metadata={'camera_id': 'front', 'camera_name': 'Front Door'},
        )
        car = main.database.add_event(
            created_at='2026-06-02T08:00:00+00:00', source='rtsp', snapshot_path=snapshot_path,
            detections=[_det('car')], metadata={'camera_id': 'back', 'camera_name': 'Back Yard'},
        )

        status, _h, page = admin.request('/api/events?q=front%20door')
        assert status == 200 and [item['id'] for item in page['items']] == [person]
        status, _h, page = admin.request('/api/events?camera_id=back&until=2026-06-03T00:00:00Z')
        assert status == 200 and [item['id'] for item in page['items']] == [car]
        status, _h, page = admin.request('/api/snapshots?label=person&sort=oldest')
        assert status == 200 and [item['id'] for item in page['items']] == [person]

        # A cursor is bound to its sort: an oldest-first cursor walks forward.
        status, _h, first = admin.request('/api/snapshots?sort=oldest&limit=1')
        assert status == 200 and [item['id'] for item in first['items']] == [person]
        status, _h, second = admin.request(f"/api/snapshots?sort=oldest&limit=1&cursor={first['next_cursor']}")
        assert status == 200 and [item['id'] for item in second['items']] == [car]
        status, _h, _body = admin.request(f"/api/snapshots?sort=newest&limit=1&cursor={first['next_cursor']}")
        assert status == 400

        status, _h, facets = admin.request('/api/library/facets?kind=snapshots&since=2026-06-02T00:00:00Z')
        assert status == 200
        assert [item['value'] for item in facets['labels']] == ['car']
        status, _h, _body = admin.request('/api/library/facets?kind=bogus')
        assert status == 422
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_alerted_recording_ids_follow_the_alerted_only_rule(tmp_path):
    """The Timeline's Alert Only filter reads a per-clip flag built from the
    same rule as /api/recordings?alerted_only: an alert on the clip or on its
    triggering event."""
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'ev.sqlite3'))
    ids = _seed(db)

    def add_recording(event_id, name):
        return db.add_recording(
            event_id=event_id, camera_id='front', started_at='2026-06-01T08:00:00+00:00',
            ended_at='2026-06-01T08:00:10+00:00', duration_seconds=10, file_path=str(tmp_path / f'{name}.mp4'),
            thumbnail_path=None, source='rtsp', created_at='2026-06-01T08:00:00+00:00', trigger_type='object',
        )

    via_event = add_recording(ids['person_front'], 'a')
    via_clip = add_recording(None, 'b')
    quiet = add_recording(ids['car_back'], 'c')
    db.add_alert('2026-06-01T08:00:00+00:00', 'rule', ids['person_front'], 'person', 0.9, 'm')
    db.add_alert('2026-06-01T08:00:00+00:00', 'rule', ids['motion_front'], 'motion', 0.9, 'm', recording_id=via_clip)
    assert db.alerted_recording_ids([via_event, via_clip, quiet]) == {via_event, via_clip}
    assert db.alerted_recording_ids([]) == set()
