"""False positives: marking, the recording rule, list filters, the AI check,
the purge and the API (app.false_positives, app.db.false_positives)."""

from __future__ import annotations

import importlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin, add_event

PERSON = {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.4}}
CAR = {'label': 'car', 'confidence': 0.6, 'box': {'x': 0.5, 'y': 0.5, 'width': 0.3, 'height': 0.2}}
MOTION = {'label': 'motion', 'confidence': 0.5, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}


def _iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def db(tmp_path):
    from app.database import EventDatabase

    return EventDatabase(str(tmp_path / 'db.sqlite3'))


def _recording(db, *, hours_ago: float = 48.0, source: str = 'rtsp', event_id=None, path='/tmp/fp.mp4') -> int:
    started = _iso(hours_ago)
    return db.add_recording(
        event_id=event_id, camera_id='cam-1', started_at=started, ended_at=_iso(hours_ago - 0.01),
        duration_seconds=30, file_path=path, thumbnail_path=None, source=source, created_at=started,
        labels=['person'],
    )


def _event(db, detections, *, hours_ago: float = 48.0, recording_id=None, source='rtsp',
           snapshot='snap.jpg', alerted=False, metadata=None) -> int:
    return add_event(db, created_at=_iso(hours_ago), source=source, snapshot_path=snapshot,
                     detections=detections, alert_triggered=alerted,
                     metadata=metadata or {'camera_id': 'cam-1', 'camera_name': 'Gate'},
                     recording_id=recording_id)


# ---------------------------------------------------------------------------
# Database: marking and the recording rule
# ---------------------------------------------------------------------------

def test_new_events_are_unjudged_and_marking_round_trips(db):
    event_id = _event(db, [PERSON])
    assert db.get_event(event_id)['false_positive'] is None
    assert db.set_event_false_positive(event_id, True, by='user', at=_iso(), reason='  a  shadow ')
    event = db.get_event(event_id)
    assert (event['false_positive'], event['false_positive_by'], event['false_positive_reason']) == (1, 'user', 'a shadow')
    assert db.set_event_false_positive(event_id, None, by='user')
    event = db.get_event(event_id)
    assert event['false_positive'] is None and event['false_positive_by'] is None
    assert not db.set_event_false_positive(999, True, by='user')
    with pytest.raises(ValueError):
        db.set_event_false_positive(event_id, True, by='someone')


def test_ai_never_overrides_a_user_decision(db):
    event_id = _event(db, [PERSON])
    db.set_event_false_positive(event_id, False, by='user')
    assert not db.set_event_false_positive(event_id, True, by='ai', only_if_unjudged=True)
    assert db.get_event(event_id)['false_positive'] == 0


def test_existing_databases_gain_the_columns(tmp_path):
    from app.database import EventDatabase

    path = str(tmp_path / 'old.sqlite3')
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                "source TEXT NOT NULL, snapshot_path TEXT, thumbnail_path TEXT, alert_triggered INTEGER DEFAULT 0, "
                "metadata TEXT DEFAULT '{}')")
    con.commit()
    con.close()
    EventDatabase(path)
    con = sqlite3.connect(path)
    columns = {row[1] for row in con.execute('PRAGMA table_info(events)')}
    con.close()
    assert {'false_positive', 'false_positive_by', 'false_positive_at', 'false_positive_reason'} <= columns


def test_recording_is_a_false_positive_when_its_object_events_are(db):
    recording_id = _recording(db)
    person = _event(db, [PERSON], recording_id=recording_id)
    # A motion blip inside the clip does not need judging.
    _event(db, [MOTION], recording_id=recording_id)
    assert db.get_recording(recording_id)['false_positive'] is None
    db.set_event_false_positive(person, True, by='ai', reason='person: a coat on a hook')
    assert db.get_recording(recording_id)['false_positive'] == {'by': 'ai', 'reason': 'person: a coat on a hook', 'users': []}


@pytest.mark.parametrize('blocker', ['unjudged_object', 'real', 'alerted_motion'])
def test_recording_is_not_a_false_positive_while_another_event_is_real(db, blocker):
    recording_id = _recording(db)
    person = _event(db, [PERSON], recording_id=recording_id)
    db.set_event_false_positive(person, True, by='user')
    if blocker == 'unjudged_object':
        _event(db, [CAR], recording_id=recording_id)
    elif blocker == 'real':
        other = _event(db, [CAR], recording_id=recording_id)
        db.set_event_false_positive(other, False, by='ai')
    else:
        _event(db, [MOTION], recording_id=recording_id, alerted=True)
    assert db.get_recording(recording_id)['false_positive'] is None
    items, _cursor = db.list_recordings_page(false_positive='only')
    assert recording_id not in [item['id'] for item in items]


def test_marking_a_recording_marks_every_event_in_it(db):
    recording_id = _recording(db)
    first = _event(db, [PERSON], recording_id=recording_id)
    second = _event(db, [CAR], recording_id=recording_id)
    unrelated = _event(db, [PERSON])
    assert db.set_recording_false_positive(recording_id, True, by='user') == 2
    assert db.get_event(first)['false_positive'] == db.get_event(second)['false_positive'] == 1
    assert db.get_event(unrelated)['false_positive'] is None
    assert db.get_recording(recording_id)['false_positive']['by'] == 'user'
    assert db.set_recording_false_positive(_recording(db), True, by='user') == 0


def test_list_filters_only_and_hide(db):
    real_recording = _recording(db)
    _event(db, [PERSON], recording_id=real_recording)
    fp_recording = _recording(db)
    fp_event = _event(db, [PERSON], recording_id=fp_recording)
    db.set_event_false_positive(fp_event, True, by='user')

    def recording_ids(mode):
        return {item['id'] for item in db.list_recordings_page(false_positive=mode)[0]}

    def event_ids(mode, snapshots=False):
        page = db.list_snapshots_page(false_positive=mode) if snapshots else db.search_events_page(false_positive=mode)
        return {item['id'] for item in page[0]}

    assert recording_ids('only') == {fp_recording}
    assert fp_recording not in recording_ids('hide') and real_recording in recording_ids('hide')
    assert event_ids('only') == {fp_event} == event_ids('only', snapshots=True)
    assert fp_event not in event_ids('hide') and fp_event in event_ids(None)


# ---------------------------------------------------------------------------
# Database: candidates and purge
# ---------------------------------------------------------------------------

def test_candidates_are_unjudged_live_object_events_with_a_snapshot(db):
    from app.ai_verification import _UNVERIFIABLE_LABELS

    wanted = _event(db, [PERSON], hours_ago=1)
    _event(db, [MOTION], hours_ago=1)  # nothing to ask about
    _event(db, [PERSON], hours_ago=1, snapshot=None)
    _event(db, [PERSON], hours_ago=1, source='upload')
    _event(db, [PERSON], hours_ago=30)  # outside the window
    judged = _event(db, [PERSON], hours_ago=1)
    db.set_event_false_positive(judged, False, by='user')
    other_camera = _event(db, [PERSON], hours_ago=1, metadata={'camera_id': 'cam-2'})
    kwargs = {'since': _iso(24), 'until': _iso(), 'labels_excluded': sorted(_UNVERIFIABLE_LABELS)}
    assert [e['id'] for e in db.false_positive_candidates(**kwargs)] == [other_camera, wanted]
    assert [e['id'] for e in db.false_positive_candidates(camera_ids=['cam-1'], **kwargs)] == [wanted]
    assert db.false_positive_candidates(camera_ids=['cam-1'], **kwargs)[0]['detections'][0]['label'] == 'person'


def test_purge_removes_old_false_positives_and_keeps_everything_else(db):
    old_fp = _recording(db, hours_ago=48)
    old_fp_events = [_event(db, [PERSON], recording_id=old_fp), _event(db, [MOTION], recording_id=old_fp)]
    db.set_event_false_positive(old_fp_events[0], True, by='ai')
    fresh_fp = _recording(db, hours_ago=1)
    db.set_event_false_positive(_event(db, [PERSON], recording_id=fresh_fp, hours_ago=1), True, by='user')
    real = _recording(db, hours_ago=48)
    _event(db, [PERSON], recording_id=real)
    upload = _recording(db, hours_ago=48, source='upload')
    db.set_event_false_positive(_event(db, [PERSON], recording_id=upload), True, by='user')
    snapshot_only = _event(db, [PERSON])
    db.set_event_false_positive(snapshot_only, True, by='user')

    stats = db.false_positive_stats(_iso(24 * 7), older_than=_iso(24))
    assert (stats['purgeable_recordings'], stats['purgeable_events']) == (1, 1)
    recordings, events = db.purge_false_positives(older_than=_iso(24))
    assert [r['id'] for r in recordings] == [old_fp]
    assert sorted(e['id'] for e in events) == sorted([*old_fp_events, snapshot_only])
    assert db.get_recording(old_fp) is None and all(db.get_event(e) is None for e in old_fp_events)
    for kept in (fresh_fp, real, upload):
        assert db.get_recording(kept) is not None


def test_purge_can_skip_ai_marked_false_positives(db):
    recording_id = _recording(db)
    db.set_event_false_positive(_event(db, [PERSON], recording_id=recording_id), True, by='ai')
    assert db.purge_false_positives(older_than=_iso(24), include_ai=False) == ([], [])
    assert len(db.purge_false_positives(older_than=_iso(24), include_ai=True)[0]) == 1


# ---------------------------------------------------------------------------
# The AI check
# ---------------------------------------------------------------------------

@pytest.fixture
def fp(monkeypatch, db):
    module = importlib.import_module('app.false_positives')
    av = importlib.import_module('app.ai_verification')
    monkeypatch.setattr(module._state, 'database', db)
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')
    monkeypatch.setattr(av, 'focus_image', lambda image, *_a, **_k: image)
    return module


class FakeVerifier:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []
        self.candidates = {}

    def ask(self, _image, label, _camera='', candidates=None, **_kw):
        self.asked.append(label)
        self.candidates[label] = candidates
        return self.answers[label]

    def check(self, _image, labels, _camera='', candidates=None, **_kw):
        self.asked.extend(labels)
        self.candidates.update(candidates or {})
        return {label: self.answers[label] for label in labels}, None


def _settings(**overrides):
    av = importlib.import_module('app.ai_verification')
    return {**av.DEFAULT_AI_VERIFICATION_SETTINGS, **overrides}


def test_check_marks_a_false_positive_when_every_object_is_rejected(fp, db):
    event = db.get_event(_event(db, [PERSON, CAR], hours_ago=1))
    verifier = FakeVerifier({'person': (False, 'a coat'), 'car': (False, 'a shed')})
    record = fp.check_event(event, _settings(), verifier=verifier)
    assert record['status'] == 'false_positive'
    assert record['reason'] == 'person: a coat; car: a shed'
    assert fp.store_check(event['id'], record)
    stored = db.get_event(event['id'])
    assert (stored['false_positive'], stored['false_positive_by']) == (1, 'ai')
    assert stored['metadata']['ai_false_positive_check']['status'] == 'false_positive'


@pytest.mark.parametrize(('answers', 'status', 'value'), [
    ({'person': (False, 'a coat'), 'car': (True, 'a car')}, 'real', 0),
    ({'person': (False, 'a coat'), 'car': ('unsure', 'too dark')}, 'unsure', 0),
])
def test_check_keeps_events_with_a_real_or_unclear_object(fp, db, answers, status, value):
    event = db.get_event(_event(db, [PERSON, CAR], hours_ago=1))
    record = fp.check_event(event, _settings(), verifier=FakeVerifier(answers))
    assert record['status'] == status
    fp.store_check(event['id'], record)
    assert db.get_event(event['id'])['false_positive'] == value


def test_check_reuses_settled_alert_verification_answers(fp, db):
    # A confirmation, or a rejection that named what it is, settles a label;
    # a bare "no" is asked again with the "what is it instead" question.
    metadata = {'camera_id': 'cam-1', 'ai_verification': {'labels': {
        'person': {'present': True, 'reason': 'a person'},
        'car': {'present': False, 'reason': 'a shed'},
    }}}
    event = db.get_event(_event(db, [PERSON, CAR], hours_ago=1, metadata=metadata))
    verifier = FakeVerifier({'car': (False, 'a shed', None)})
    record = fp.check_event(event, _settings(), verifier=verifier)
    assert verifier.asked == ['car']
    assert record['status'] == 'real' and record['labels']['person']['reused']


def test_a_group_rules_verdict_covers_its_member(fp, db):
    # An "animal" rule verifies the group: its "no" settles the cat it saw,
    # and a confirmation that named another member reads as that member.
    cat = {**PERSON, 'label': 'cat'}
    metadata = {'camera_id': 'cam-1', 'ai_verification': {'asked_instead': True, 'labels': {
        'animal': {'present': False, 'reason': 'a bag on the mat'},
    }}}
    event = db.get_event(_event(db, [cat], hours_ago=1, metadata=metadata))
    verifier = FakeVerifier({})
    record = fp.check_event(event, _settings(), verifier=verifier)
    assert verifier.asked == []
    assert record['status'] == 'false_positive' and record['labels']['cat']['reused']
    metadata['ai_verification']['labels'] = {'animal': {'present': True, 'reason': 'a dog', 'kind': 'dog'}}
    event = db.get_event(_event(db, [cat], hours_ago=1, metadata=metadata))
    record = fp.check_event(event, _settings(), verifier=verifier)
    assert verifier.asked == []
    assert record['status'] == 'reclassified' and record['reclassified'] == {'cat': 'dog'}


def test_a_recheck_yes_against_alert_verifications_no_is_unsure(fp, db):
    # Verification filtered the alert ("no cat"); a later "yes" from the
    # same model on the same snapshot must not put a ✓ beside the Filtered
    # badge: the two answers disagree, so the cat is marked unsure.
    cat = {**PERSON, 'label': 'cat'}
    metadata = {'camera_id': 'cam-1', 'ai_verification': {'status': 'filtered', 'labels': {
        'cat': {'present': False, 'reason': 'a dark bag'},
    }}}
    event_id = _event(db, [cat], hours_ago=1, metadata=metadata)
    verifier = FakeVerifier({'cat': (True, 'a cat by the door')})
    record = fp.check_event(db.get_event(event_id), _settings(), verifier=verifier)
    assert verifier.asked == ['cat']
    assert record['status'] == 'unsure'
    assert record['labels']['cat']['unsure'] and record['labels']['cat']['conflict']
    assert 'a dark bag' in record['labels']['cat']['reason']
    fp.store_check(event_id, record)
    stored = db.get_event(event_id)
    assert stored['detections'][0]['ai_status'] == 'unsure'
    assert stored['false_positive'] == 0


def test_alert_verifications_relabel_is_reused(fp, db):
    metadata = {'camera_id': 'cam-1', 'ai_verification': {'labels': {
        'dog': {'present': False, 'reason': 'a cat', 'instead': 'cat'},
    }}}
    dog = {**PERSON, 'label': 'dog'}
    event = db.get_event(_event(db, [dog], hours_ago=1, metadata=metadata))
    verifier = FakeVerifier({})
    record = fp.check_event(event, _settings(), verifier=verifier)
    assert verifier.asked == []
    assert record['status'] == 'reclassified' and record['reclassified'] == {'dog': 'cat'}


def test_a_cat_detected_as_a_dog_is_reclassified_never_a_false_positive(fp, db):
    dog = {**PERSON, 'label': 'dog', 'confidence': 0.62}
    # The clip was started by the dog event; another event in it saw a person.
    recording_id = _recording(db)
    event_id = _event(db, [dog, MOTION], hours_ago=1, recording_id=recording_id)
    _event(db, [PERSON], hours_ago=1, recording_id=recording_id)
    db.add_recording_labels(recording_id, ['dog'], confidences={'dog': 0.62})
    db.update_recording_trigger(recording_id, trigger_type='object', trigger_label='dog')
    event = db.get_event(event_id)
    verifier = FakeVerifier({'dog': (False, 'it is a cat', 'cat')})
    record = fp.check_event(event, _settings(), verifier=verifier)
    # The model is offered what it might be instead, never the label itself.
    assert 'cat' in verifier.candidates['dog'] and 'dog' not in verifier.candidates['dog']
    assert record['status'] == 'reclassified'
    assert record['reason'] == 'Reclassified dog as cat.'
    assert fp.store_check(event_id, record)
    stored = db.get_event(event_id)
    assert (stored['false_positive'], stored['false_positive_by']) == (0, 'ai')
    assert sorted(d['label'] for d in stored['detections']) == ['cat', 'motion']
    assert stored['metadata']['ai_false_positive_check']['reclassified'] == {'dog': 'cat'}
    recording = db.get_recording(recording_id)
    assert 'cat' in recording['labels'] and 'dog' not in recording['labels']
    assert recording['label_confidences']['cat'] == pytest.approx(0.62)
    assert recording['trigger_label'] == 'cat'
    assert recording['false_positive'] is None
    assert db.purge_false_positives(older_than=_iso(0)) == ([], [])


def test_reclassifying_keeps_a_label_another_event_in_the_clip_still_has(fp, db):
    dog = {**PERSON, 'label': 'dog'}
    recording_id = _recording(db)
    event_id = _event(db, [dog], hours_ago=1, recording_id=recording_id)
    _event(db, [dog], hours_ago=1, recording_id=recording_id)
    db.add_recording_labels(recording_id, ['dog'])
    assert db.reclassify_event_detections(event_id, {'dog': 'cat'}) == 1
    labels = db.get_recording(recording_id)['labels']
    assert 'dog' in labels and 'cat' in labels


def test_a_real_animal_without_a_class_is_real_and_keeps_its_label(fp, db):
    dog = {**PERSON, 'label': 'dog'}
    event_id = _event(db, [dog], hours_ago=1)
    record = fp.check_event(db.get_event(event_id), _settings(),
                            verifier=FakeVerifier({'dog': (False, 'a fox', 'another animal')}))
    assert record['status'] == 'real' and 'reclassified' not in record
    fp.store_check(event_id, record)
    stored = db.get_event(event_id)
    assert stored['false_positive'] == 0 and stored['detections'][0]['label'] == 'dog'


def test_reclassification_wins_over_another_rejected_label(fp, db):
    dog = {**PERSON, 'label': 'dog'}
    event = db.get_event(_event(db, [dog, CAR], hours_ago=1))
    verifier = FakeVerifier({'dog': (False, 'a cat', 'cat'), 'car': (False, 'a shed', None)})
    record = fp.check_event(event, _settings(), verifier=verifier)
    assert record['status'] == 'reclassified' and record['reclassified'] == {'dog': 'cat'}


def test_a_users_decision_is_never_relabelled(fp, db):
    dog = {**PERSON, 'label': 'dog'}
    event_id = _event(db, [dog], hours_ago=1)
    db.set_event_false_positive(event_id, True, by='user')
    record = fp.check_event(db.get_event(event_id), _settings(),
                            verifier=FakeVerifier({'dog': (False, 'a cat', 'cat')}))
    assert not fp.store_check(event_id, record)
    assert db.get_event(event_id)['detections'][0]['label'] == 'dog'


def test_reclassified_reason_reads_naturally(fp):
    assert fp.reclassified_reason({'dog': 'cat'}) == 'Reclassified dog as cat.'
    assert fp.reclassified_reason({'fire_hydrant': 'person'}) == 'Reclassified fire hydrant as person.'
    assert 'deer' not in fp.REAL_OBJECT_CANDIDATES


def test_upgrade_reopens_ai_false_positives_judged_without_the_relabel_question(db):
    old_ai = _event(db, [PERSON], metadata={'ai_false_positive_check': {'status': 'false_positive'}})
    db.set_event_false_positive(old_ai, True, by='ai', reason='person: a coat')
    new_ai = _event(db, [PERSON], metadata={'ai_false_positive_check': {'status': 'false_positive',
                                                                       'asked_instead': True}})
    db.set_event_false_positive(new_ai, True, by='ai')
    user = _event(db, [PERSON])
    db.set_event_false_positive(user, True, by='user')
    db.init()
    assert db.get_event(old_ai)['false_positive'] is None
    assert db.get_event(new_ai)['false_positive'] == 1
    assert db.get_event(user)['false_positive'] == 1


def test_check_never_calls_more_objects_than_it_can_ask_a_false_positive(fp, db, monkeypatch):
    av = importlib.import_module('app.ai_verification')
    monkeypatch.setattr(av, 'MAX_LABELS_PER_EVENT', 1)
    event = db.get_event(_event(db, [PERSON, CAR], hours_ago=1))
    record = fp.check_event(event, _settings(), verifier=FakeVerifier({'person': (False, 'a coat')}))
    assert record['status'] == 'unsure'


def test_run_checks_once_checks_new_events_only_when_enabled(fp, db, monkeypatch):
    event_id = _event(db, [PERSON], hours_ago=1)
    verifier = FakeVerifier({'person': (False, 'a coat')})
    settings = {**fp.DEFAULT_FALSE_POSITIVE_SETTINGS}
    monkeypatch.setattr(fp, 'effective_false_positive_settings', lambda: dict(settings))
    av = importlib.import_module('app.ai_verification')
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: _settings())
    fp._attempted.clear()
    assert fp.run_checks_once(verifier=verifier, ignore_busy=True) == 0
    settings['ai_check'] = True
    assert fp.run_checks_once(verifier=verifier, ignore_busy=True) == 1
    assert db.get_event(event_id)['false_positive'] == 1
    assert fp.run_checks_once(verifier=verifier, ignore_busy=True) == 0


def test_model_outage_leaves_the_event_for_a_later_round(fp, db, monkeypatch):
    av = importlib.import_module('app.ai_verification')
    event_id = _event(db, [PERSON], hours_ago=1)
    monkeypatch.setattr(fp, 'effective_false_positive_settings',
                        lambda: {**fp.DEFAULT_FALSE_POSITIVE_SETTINGS, 'ai_check': True})
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: _settings())

    class Down:
        def ask(self, *_a, **_k):
            raise av.ModelUnavailableError('model unreachable')

    fp._attempted.clear()
    assert fp.run_checks_once(verifier=Down(), ignore_busy=True) == 0
    assert db.get_event(event_id)['false_positive'] is None
    assert fp.run_checks_once(verifier=FakeVerifier({'person': (True, 'a person')}), ignore_busy=True) == 1
    assert db.get_event(event_id)['false_positive'] == 0


def test_settings_validation(fp):
    from fastapi import HTTPException

    settings = fp.validate_false_positive_settings({'purge_enabled': 'on', 'purge_after_hours': 6,
                                                    'ai_check_camera_ids': [' cam-1 ', 'cam-1']})
    assert settings['purge_enabled'] is True and settings['purge_after_hours'] == 6
    assert settings['ai_check_camera_ids'] == ['cam-1']
    for bad in ({'purge_after_hours': 0}, {'purge_after_hours': True}, {'ai_check': 'maybe'}, {'nope': 1}):
        with pytest.raises(HTTPException):
            fp.validate_false_positive_settings(bad)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_api_marks_lists_and_purges(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        client = LocalClient(base_url)
        _setup_admin(client)
        csrf = _login(client)
        headers = {'X-CSRF-Token': csrf}
        state = importlib.import_module('app.state')
        database = state.database
        recording_id = _recording(database, path=str(tmp_path / 'data' / 'recordings' / 'clip.mp4'))
        event_id = _event(database, [PERSON], recording_id=recording_id, snapshot=None)
        empty_recording = _recording(database)

        status, _h, body = client.request('/api/settings/false-positives')
        assert status == 200 and body['purge_enabled'] is False and body['purge_after_hours'] == 24
        status, _h, body = client.request('/api/settings/false-positives', method='PUT', headers=headers,
                                          json_body={'purge_after_hours': 0})
        assert status == 400
        status, _h, body = client.request('/api/settings/false-positives', method='PUT', headers=headers,
                                          json_body={'purge_after_hours': 12, 'ai_check': True})
        assert status == 200 and body['purge_after_hours'] == 12 and body['ai_check'] is True

        status, _h, body = client.request(f'/api/events/{event_id}/false-positive', method='POST',
                                          headers=headers, json_body={'false_positive': 'yes'})
        assert status == 400
        status, _h, body = client.request(f'/api/events/{event_id}/false-positive', method='POST',
                                          headers=headers, json_body={'false_positive': True})
        assert status == 200 and body['false_positive'] == 1 and body['false_positive_by'] == 'user'
        status, _h, body = client.request('/api/events?false_positive=only&since=2000-01-01T00:00:00Z')
        assert status == 200 and [item['id'] for item in body['items']] == [event_id]
        status, _h, body = client.request('/api/recordings?false_positive=only&started_after=2000-01-01T00:00:00Z')
        assert status == 200 and [item['id'] for item in body['items']] == [recording_id]
        assert body['items'][0]['false_positive']['by'] == 'user'

        status, _h, body = client.request(f'/api/recordings/{recording_id}/false-positive', method='POST',
                                          headers=headers, json_body={'false_positive': False})
        assert status == 200 and body['events_updated'] == 1 and body['false_positive'] is None
        status, _h, _body = client.request(f'/api/recordings/{empty_recording}/false-positive', method='POST',
                                           headers=headers, json_body={'false_positive': True})
        assert status == 409
        status, _h, _body = client.request('/api/recordings/99999/false-positive', method='POST',
                                           headers=headers, json_body={'false_positive': True})
        assert status == 404

        client.request(f'/api/recordings/{recording_id}/false-positive', method='POST',
                       headers=headers, json_body={'false_positive': True})
        status, _h, body = client.request('/api/settings/false-positives/stats')
        assert status == 200 and body['purgeable_recordings'] == 1
        status, _h, body = client.request('/api/settings/false-positives/purge', method='POST', headers=headers)
        assert status == 200 and body['recordings_purged'] == 1 and body['events_purged'] == 1
        assert database.get_recording(recording_id) is None and database.get_event(event_id) is None

        # Viewers may mark (audit-logged as them) but not change the
        # settings, read the stats or purge.
        status, _h, _body = client.request('/api/users', method='POST', headers=headers,
                                           json_body={'username': 'viewer1', 'password': 'Viewer123!', 'role': 'viewer'})
        assert status == 200
        viewer = LocalClient(base_url)
        viewer_headers = {'X-CSRF-Token': _login(viewer, 'viewer1', 'Viewer123!')}
        other_event = _event(database, [PERSON], snapshot=None)
        status, _h, body = viewer.request(f'/api/events/{other_event}/false-positive', method='POST',
                                          headers=viewer_headers, json_body={'false_positive': True})
        assert status == 200 and body['false_positive'] == 1 and body['false_positive_by'] == 'user'
        assert (body['false_positive_user'], body['false_positive_role']) == ('viewer1', 'viewer')
        logged = database.list_audit_logs(username='viewer1', action='false_positive')
        assert [entry['resource_id'] for entry in logged] == [str(other_event)]
        for path, method in (('/api/settings/false-positives', 'PUT'),
                             ('/api/settings/false-positives/stats', 'GET'),
                             ('/api/settings/false-positives/purge', 'POST')):
            status, _h, _body = viewer.request(path, method=method, headers=viewer_headers,
                                               json_body={} if method != 'GET' else None)
            assert status == 403, path
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_periodic_purge_only_runs_when_enabled(fp, db, monkeypatch):
    recording_id = _recording(db)
    db.set_event_false_positive(_event(db, [PERSON], recording_id=recording_id), True, by='user')
    settings = {**fp.DEFAULT_FALSE_POSITIVE_SETTINGS}
    monkeypatch.setattr(fp, 'effective_false_positive_settings', lambda: dict(settings))
    assert fp.purge_false_positives_by_policy()['recordings_purged'] == 0
    settings['purge_enabled'] = True
    assert fp.purge_false_positives_by_policy()['recordings_purged'] == 1
    assert db.get_recording(recording_id) is None


def test_marks_by_hand_name_who_made_them_and_viewer_marks_can_be_kept(fp, db, monkeypatch):
    viewer_clip = _recording(db)
    viewer_event = _event(db, [PERSON], recording_id=viewer_clip)
    admin_clip = _recording(db)
    _event(db, [PERSON], recording_id=admin_clip)
    old_clip = _recording(db)
    old_event = _event(db, [PERSON], recording_id=old_clip)
    lone_event = _event(db, [PERSON])
    db.set_event_false_positive(viewer_event, True, by='user', user='sam', role='viewer')
    db.set_recording_false_positive(admin_clip, True, by='user', user='alex', role='admin')
    db.set_event_false_positive(old_event, True, by='user')  # a mark from before roles were stored
    db.set_event_false_positive(lone_event, True, by='user', user='sam', role='viewer')
    event = db.get_event(viewer_event)
    assert (event['false_positive_user'], event['false_positive_role']) == ('sam', 'viewer')
    assert db.get_recording(admin_clip)['false_positive']['users'] == ['alex']
    assert db.get_recording(old_clip)['false_positive']['users'] == []
    # The AI never records a person, and clearing a mark forgets who made it.
    db.set_event_false_positive(viewer_event, None, by='user', user='sam', role='viewer')
    assert db.get_event(viewer_event)['false_positive_user'] is None
    db.set_event_false_positive(viewer_event, True, by='ai', user='sam', role='viewer')
    assert db.get_event(viewer_event)['false_positive_user'] is None
    db.set_event_false_positive(viewer_event, True, by='user', user='sam', role='viewer')

    settings = {**fp.DEFAULT_FALSE_POSITIVE_SETTINGS, 'purge_enabled': True, 'purge_include_viewers': False}
    monkeypatch.setattr(fp, 'effective_false_positive_settings', lambda: dict(settings))
    stats = fp.false_positive_stats()
    assert (stats['purgeable_recordings'], stats['purgeable_events']) == (2, 0)
    result = fp.purge_false_positives_by_policy()
    assert (result['recordings_purged'], result['events_purged']) == (2, 2)
    assert db.get_recording(viewer_clip) is not None and db.get_event(lone_event) is not None
    assert db.get_recording(admin_clip) is None and db.get_recording(old_clip) is None
    settings['purge_include_viewers'] = True
    result = fp.purge_false_positives_by_policy()
    assert (result['recordings_purged'], result['events_purged']) == (1, 2)


def test_viewer_purge_setting_is_validated(fp):
    from fastapi import HTTPException

    assert fp.validate_false_positive_settings({'purge_include_viewers': 'false'})['purge_include_viewers'] is False
    with pytest.raises(HTTPException):
        fp.validate_false_positive_settings({'purge_include_viewers': 'maybe'})
