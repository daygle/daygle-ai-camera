"""Tests for the face-recognition enrolment repo (app/db/persons.py)."""
from __future__ import annotations

import numpy as np

from app.database import EventDatabase
from app.face_recognition import embedding_to_bytes


def _db(tmp_path) -> EventDatabase:
    return EventDatabase(str(tmp_path / 'persons.sqlite3'))


def _emb(vec) -> bytes:
    return embedding_to_bytes(np.asarray(vec, dtype=np.float32))


def test_add_and_get_person(tmp_path):
    db = _db(tmp_path)
    pid = db.add_person('Alex', notes='household')
    person = db.get_person(pid)
    assert person is not None
    assert person['name'] == 'Alex'
    assert person['notes'] == 'household'
    assert person['face_count'] == 0
    assert person['created_at'] and person['updated_at']


def test_list_persons_sorted_with_face_counts(tmp_path):
    db = _db(tmp_path)
    sam = db.add_person('Sam')
    db.add_person('alex')  # lowercase to exercise NOCASE ordering
    db.add_person_face(sam, embedding=_emb([1, 0, 0, 0]), dim=4, model='arcface')
    people = db.list_persons()
    assert [p['name'] for p in people] == ['alex', 'Sam']
    counts = {p['name']: p['face_count'] for p in people}
    assert counts == {'alex': 0, 'Sam': 1}


def test_update_person_notes_preserved_on_rename(tmp_path):
    db = _db(tmp_path)
    pid = db.add_person('Alex', notes='keep me')
    assert db.update_person(pid, name='Alexis') is True
    person = db.get_person(pid)
    assert person['name'] == 'Alexis'
    assert person['notes'] == 'keep me'  # rename must not wipe notes


def test_update_person_no_fields_is_noop(tmp_path):
    db = _db(tmp_path)
    pid = db.add_person('Alex')
    assert db.update_person(pid) is False


def test_delete_person_cascades_faces(tmp_path):
    db = _db(tmp_path)
    pid = db.add_person('Alex')
    db.add_person_face(pid, embedding=_emb([1, 0, 0, 0]), dim=4, model='arcface')
    db.add_person_face(pid, embedding=_emb([0, 1, 0, 0]), dim=4, model='arcface')
    assert db.count_person_faces() == 2
    assert db.delete_person(pid) is True
    assert db.get_person(pid) is None
    # foreign_keys PRAGMA is off, so the repo must remove the faces itself.
    assert db.count_person_faces() == 0


def test_add_list_delete_person_face(tmp_path):
    db = _db(tmp_path)
    pid = db.add_person('Alex')
    fid = db.add_person_face(
        pid, embedding=_emb([1, 0, 0, 0]), dim=4, model='arcface', source_snapshot='snap.jpg'
    )
    faces = db.list_person_faces(pid)
    assert len(faces) == 1
    assert faces[0]['dim'] == 4
    assert faces[0]['model'] == 'arcface'
    assert faces[0]['source_snapshot'] == 'snap.jpg'
    assert 'embedding' not in faces[0]  # blob is not shipped in the listing
    assert db.delete_person_face(fid) is True
    assert db.list_person_faces(pid) == []


def test_person_face_thumbnail_roundtrip(tmp_path):
    db = _db(tmp_path)
    pid = db.add_person('Alex')
    jpeg = b'\xff\xd8\xff\xe0stub-jpeg-bytes'
    with_thumb = db.add_person_face(
        pid, embedding=_emb([1, 0, 0, 0]), dim=4, model='arcface', thumbnail=jpeg
    )
    without_thumb = db.add_person_face(
        pid, embedding=_emb([0, 1, 0, 0]), dim=4, model='arcface'
    )

    faces = {f['id']: f for f in db.list_person_faces(pid)}
    # The listing exposes only a boolean flag, never the blob itself.
    assert faces[with_thumb]['has_thumbnail'] is True
    assert faces[without_thumb]['has_thumbnail'] is False
    assert 'thumbnail' not in faces[with_thumb]

    # The bytes come back verbatim; a face with no thumbnail returns None.
    assert db.get_person_face_thumbnail(with_thumb) == jpeg
    assert db.get_person_face_thumbnail(without_thumb) is None
    # An unknown face id is None (the API turns that into a 404).
    assert db.get_person_face_thumbnail(999_999) is None


def test_load_face_embeddings_filters_by_model(tmp_path):
    db = _db(tmp_path)
    alex = db.add_person('Alex')
    sam = db.add_person('Sam')
    db.add_person_face(alex, embedding=_emb([1, 0, 0, 0]), dim=4, model='arcface')
    db.add_person_face(sam, embedding=_emb([0, 1, 0, 0]), dim=4, model='arcface')
    db.add_person_face(sam, embedding=_emb(np.ones(8)), dim=8, model='other-model')

    arc = db.load_face_embeddings('arcface')
    assert {row['person_name'] for row in arc} == {'Alex', 'Sam'}
    assert all(row['dim'] == 4 for row in arc)
    assert all('embedding' in row for row in arc)  # matcher needs the bytes

    other = db.load_face_embeddings('other-model')
    assert len(other) == 1
    assert other[0]['dim'] == 8
    assert db.count_person_faces(model='arcface') == 2
    assert db.count_person_faces() == 3


import json


def _event_metadata(db, event_id):
    with db.connect() as conn:
        row = conn.execute("SELECT metadata FROM events WHERE id = ?", (event_id,)).fetchone()
    return json.loads(row['metadata'] or '{}')


def test_purge_face_identities_strips_old_events_only(tmp_path):
    db = _db(tmp_path)
    ids = {'face_identities': {'people': [{'person_id': 1, 'name': 'Alex'}], 'unknown': 0}, 'camera_id': 'c1'}
    old_id = db.add_event(
        created_at='2020-01-01T00:00:00+00:00', source='rtsp', snapshot_path=None,
        detections=[], metadata=dict(ids),
    )
    recent_id = db.add_event(
        created_at='2026-08-19T00:00:00+00:00', source='rtsp', snapshot_path=None,
        detections=[], metadata=dict(ids),
    )
    purged = db.purge_face_identities(older_than='2021-01-01T00:00:00+00:00')
    assert purged == 1
    # Old event lost its identities but kept the rest of the metadata.
    old_meta = _event_metadata(db, old_id)
    assert 'face_identities' not in old_meta
    assert old_meta['camera_id'] == 'c1'
    # Recent event is untouched.
    assert 'face_identities' in _event_metadata(db, recent_id)


def test_purge_face_identities_noop_without_matches(tmp_path):
    db = _db(tmp_path)
    db.add_event(
        created_at='2020-01-01T00:00:00+00:00', source='rtsp', snapshot_path=None,
        detections=[], metadata={'camera_id': 'c1'},  # no face_identities
    )
    assert db.purge_face_identities(older_than='2026-01-01T00:00:00+00:00') == 0


def test_purge_face_identities_by_policy_respects_retention_setting(tmp_path, monkeypatch):
    import app.backup as backup
    import app.state as state

    db = _db(tmp_path)
    old_id = db.add_event(
        created_at='2020-01-01T00:00:00+00:00', source='rtsp', snapshot_path=None,
        detections=[], metadata={'face_identities': {'people': [], 'unknown': 1}},
    )
    monkeypatch.setattr(state, 'database', db)

    # retention_days = 0 -> keep indefinitely (no-op).
    monkeypatch.setattr(backup, 'effective_face_recognition_config', lambda: {'retention_days': 0})
    assert backup.purge_face_identities_by_policy() == 0
    assert 'face_identities' in _event_metadata(db, old_id)

    # retention_days = 1 -> the 2020 event is well past the window and is purged.
    monkeypatch.setattr(backup, 'effective_face_recognition_config', lambda: {'retention_days': 1})
    assert backup.purge_face_identities_by_policy() == 1
    assert 'face_identities' not in _event_metadata(db, old_id)
