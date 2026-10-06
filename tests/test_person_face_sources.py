"""Where a person's enrolled faces came from, and their pictures.

A person's face list mixes photos uploaded on the People card, captures
assigned from Review, and embeddings auto-enrichment learned from confident
live matches. Review assignments and auto-learned faces used to be stored
without a picture, so the People card showed them as blanks with no way to
tell what they were. These tests pin the picture, the source label, and the
bulk removal of auto-learned faces.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin

EMBEDDING = b'\x00' * 16


def _db(tmp_path):
    from app.database import EventDatabase

    return EventDatabase(str(tmp_path / 'faces.sqlite3'))


def _unknown(db, thumbnail=b'jpeg-bytes'):
    return db.store_unknown_face(camera_id='cam', embedding=EMBEDDING, dim=4, model='m', thumbnail=thumbnail)


def test_assigning_from_review_keeps_the_picture(tmp_path):
    db = _db(tmp_path)
    person = db.add_person('Alice')
    db.assign_unknown_face_with_embedding(_unknown(db), person_id=person)
    [face] = db.list_person_faces(person)
    assert face['source'] == 'review'
    assert face['has_thumbnail'] is True
    assert db.get_person_face_thumbnail(face['id']) == b'jpeg-bytes'


def test_an_older_review_assignment_borrows_the_capture_picture(tmp_path):
    # Assigned before the picture was copied across: the face row has none,
    # but the reviewed capture is still stored, so show its picture.
    db = _db(tmp_path)
    person = db.add_person('Alice')
    capture = _unknown(db, thumbnail=b'old-capture')
    face_id = db.add_person_face(person, embedding=EMBEDDING, dim=4, model='m', source_snapshot=f'unknown-face:{capture}')
    [face] = db.list_person_faces(person)
    assert face['has_thumbnail'] is True
    assert db.get_person_face_thumbnail(face_id) == b'old-capture'
    db.delete_unknown_face(capture)
    assert db.list_person_faces(person)[0]['has_thumbnail'] is False
    assert db.get_person_face_thumbnail(face_id) is None


def test_faces_report_their_source_and_auto_learned_ones_can_be_cleared(tmp_path):
    db = _db(tmp_path)
    person = db.add_person('Alice')
    other = db.add_person('Bob')
    db.add_person_face(person, embedding=EMBEDDING, dim=4, model='m', thumbnail=b'photo')
    db.assign_unknown_face_with_embedding(_unknown(db), person_id=person)
    for track in range(3):
        db.add_person_face(person, embedding=EMBEDDING, dim=4, model='m', source_snapshot=f'auto-enrich:cam=cam,track={track}')
    db.add_person_face(other, embedding=EMBEDDING, dim=4, model='m', source_snapshot='auto-enrich:cam=cam,track=9')
    assert sorted(face['source'] for face in db.list_person_faces(person)) == ['auto', 'auto', 'auto', 'enrolled', 'review']
    assert db.delete_person_faces_by_source(person, 'auto') == 3
    assert sorted(face['source'] for face in db.list_person_faces(person)) == ['enrolled', 'review']
    assert len(db.list_person_faces(other)) == 1, "another person's faces are untouched"


def test_auto_enrichment_stores_the_crop_it_learned_from(monkeypatch):
    import app.state as state
    from app import face_identity

    added = {}
    fake_db = SimpleNamespace(
        list_person_faces=lambda _pid: [],
        add_person_face=lambda person_id, **kwargs: added.update(person_id=person_id, **kwargs),
    )
    monkeypatch.setattr(state, 'database', fake_db)
    monkeypatch.setattr('app.face_recognition_service.refresh_face_recognition_matcher', lambda: None)
    service = SimpleNamespace(model_id='m', embed_face=lambda _crop: np.ones(4, dtype=np.float32))
    crop = np.full((40, 40, 3), 128, dtype=np.uint8)
    face_identity._store_enriched_embedding(7, 'cam', 3, {}, crop, service)
    assert added['person_id'] == 7
    assert added['source_snapshot'].startswith('auto-enrich:')
    assert added['thumbnail'], 'the learned crop is kept as the face picture'


def test_api_removes_auto_learned_faces(tmp_path, monkeypatch):
    app, _ = _load_app(tmp_path, monkeypatch)
    import app.main as main

    server, thread, base = _server(app)
    client = LocalClient(base)
    try:
        _setup_admin(client)
        csrf = _login(client)
        person = main.database.add_person('Alice')
        main.database.add_person_face(person, embedding=EMBEDDING, dim=4, model='m', thumbnail=b'photo')
        main.database.add_person_face(person, embedding=EMBEDDING, dim=4, model='m', source_snapshot='auto-enrich:cam=c,track=1')
        status, _h, body = client.request(f'/api/persons/{person}/faces?source=auto', method='DELETE', headers={'X-CSRF-Token': csrf})
        assert status == 200 and body['removed'] == 1
        assert [face['source'] for face in body['faces']] == ['enrolled']
        status, _h, _body = client.request(f'/api/persons/{person}/faces?source=everything', method='DELETE', headers={'X-CSRF-Token': csrf})
        assert status == 400
        status, _h, _body = client.request('/api/persons/9999/faces?source=auto', method='DELETE', headers={'X-CSRF-Token': csrf})
        assert status == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)
