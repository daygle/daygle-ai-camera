"""Regression tests for the face-recognition, behaviour and database audit fixes.

* Face alert rules match the enrolled person by ``person_id``: a rename keeps
  the rule working, a namesake does not trigger it, and nobody can match the
  unknown-person system rule by being named after it.
* Deleting a person also erases the reviewed unknown-face captures that were
  assigned to them (embedding + face thumbnail).
* Renaming a person rebuilds the live matcher, which caches names.
* Crops handed to background face jobs are independent copies.
* PTZ requests XML-escape the profile token returned by the camera.
* The audit-log resource filter treats ``_`` and ``%`` literally.
* The camera-diagnostics ring buffer still keeps exactly the newest rows.
"""
from __future__ import annotations

import importlib
import json

import pytest

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin

from app.database import EventDatabase


@pytest.fixture
def face_rules(monkeypatch):
    fdr = importlib.import_module('app.face_detection_rules')

    def _use(*rules):
        class _Db:
            def get_setting(self, key):
                return {'rules': list(rules)} if key == 'face_detection_rules' else None

        monkeypatch.setattr(fdr._state, 'database', _Db())
        monkeypatch.setattr(fdr, 'effective_face_recognition_config', lambda: {'enabled': True})
        fdr._face_rule_cooldowns.clear()
        return fdr

    return _use


def _face(person_id, name, track_id=1):
    return {
        'label': 'face', 'recognized': True, 'track_id': track_id,
        'person_id': person_id, 'person_name': name, 'confidence': 0.9,
    }


def _rule(**overrides):
    rule = {
        'id': 'person_1', 'person_id': '1', 'name': 'Alice', 'enabled': True,
        'email_enabled': True, 'push_enabled': False, 'email_recipients': '',
        'cooldown_minutes': 5, 'min_confidence': None,
    }
    rule.update(overrides)
    return rule


def test_face_rule_survives_a_rename(face_rules):
    fdr = face_rules(_rule(name='Alice'))
    alerts = fdr.known_face_rules_for_camera('cam', [_face(1, 'Alicia')])
    assert [alert['face_rule_id'] for alert in alerts] == ['person_1']


def test_face_rule_ignores_a_namesake(face_rules):
    fdr = face_rules(_rule(name='Alice'))
    assert fdr.known_face_rules_for_camera('cam', [_face(2, 'Alice')]) == []


def test_recognised_face_never_matches_the_unknown_rule(face_rules):
    fdr = face_rules({
        'id': '_unknown', 'person_id': None, 'name': 'Unknown Person', 'enabled': True,
        'email_enabled': True, 'push_enabled': False, 'email_recipients': '',
        'cooldown_minutes': 5, 'min_confidence': None,
    })
    assert fdr.known_face_rules_for_camera('cam', [_face(3, 'Unknown Person')]) == []


def test_legacy_rule_without_person_id_still_matches_by_name(face_rules):
    fdr = face_rules(_rule(person_id=None, name='Alice'))
    assert len(fdr.known_face_rules_for_camera('cam', [_face(1, 'alice')])) == 1


def test_delete_person_erases_assigned_unknown_face_captures(tmp_path):
    db = EventDatabase(str(tmp_path / 'people.sqlite3'))
    other = db.add_person('Bob')
    kept = db.store_unknown_face(camera_id='cam', embedding=b'\0' * 8, dim=2, model='arcface')
    doomed = db.store_unknown_face(camera_id='cam', embedding=b'\1' * 8, dim=2, model='arcface')
    result = db.assign_unknown_face_with_embedding(doomed, person_name='Alice')
    db.assign_unknown_face_with_embedding(kept, person_id=other)

    assert db.delete_person(int(result['person_id'])) is True

    assert db.get_unknown_face(doomed) is None
    assert db.get_unknown_face(kept) is not None


def test_renaming_a_person_refreshes_the_matcher(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    persons_router = importlib.import_module('app.api.persons_router')
    refreshes: list[int] = []
    monkeypatch.setattr(persons_router, 'refresh_face_recognition_matcher', lambda: refreshes.append(1))
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}
        status, _h, created = client.request(
            '/api/persons', method='POST', data=json.dumps({'name': 'Alex'}).encode(), headers=headers,
        )
        assert status == 200
        status, _h, _b = client.request(
            f"/api/persons/{created['id']}", method='PATCH',
            data=json.dumps({'notes': 'no rename'}).encode(), headers=headers,
        )
        assert status == 200 and refreshes == []
        status, _h, _b = client.request(
            f"/api/persons/{created['id']}", method='PATCH',
            data=json.dumps({'name': 'Alexis'}).encode(), headers=headers,
        )
        assert status == 200 and refreshes == [1]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_background_face_jobs_receive_a_copy_of_the_crop():
    np = pytest.importorskip('numpy')
    face_identity = importlib.import_module('app.face_identity')
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    crop = frame[2:6, 2:6]
    detached = face_identity._detached(crop)
    frame[:] = 255
    assert int(detached.max()) == 0
    assert detached.base is None


def test_ptz_escapes_the_camera_profile_token(monkeypatch):
    ptz = importlib.import_module('app.ptz')
    sent: list[str] = []
    monkeypatch.setattr(ptz, '_get_profile_token', lambda *_a: 'a<b>&"c')
    monkeypatch.setattr(ptz, '_soap', lambda _url, body, *_a: sent.append(body) or '')

    ptz.send_ptz_command_onvif('10.0.0.5', 80, 'stop', 4, 'admin', 'pw')

    assert '<tptz:ProfileToken>a&lt;b&gt;&amp;' in sent[0]
    assert 'a<b>' not in sent[0]


def test_audit_resource_filter_treats_wildcards_literally(tmp_path):
    db = EventDatabase(str(tmp_path / 'audit.sqlite3'))
    for resource in ('settings_ai', 'settingsXai', 'settings.ai'):
        db.add_audit_log(
            created_at='2026-09-28T00:00:00+00:00', user_id=1, username='admin',
            action='update', resource=resource,
        )
    found = {row['resource'] for row in db.list_audit_logs(resource='settings_')}
    assert found == {'settings_ai'}
    assert db.count_audit_logs(resource='settings_') == 1
    assert db.count_audit_logs(resource='settings') == 3


def test_camera_diagnostics_ring_buffer_keeps_newest_rows(tmp_path, monkeypatch):
    db = EventDatabase(str(tmp_path / 'diag.sqlite3'))
    monkeypatch.setattr(type(db), 'CAMERA_DIAGNOSTICS_MAX_ROWS', 3)
    for index in range(6):
        db.add_camera_diagnostic(
            created_at=f'2026-09-28T00:00:0{index}+00:00', camera_id='cam',
            camera_name='Cam', event_type='test', message=str(index),
        )
    messages = sorted(row['message'] for row in db.list_camera_diagnostics(limit=50))
    assert messages == ['3', '4', '5']
