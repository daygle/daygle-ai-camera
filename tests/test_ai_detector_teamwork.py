"""The object detector and the vision model working together: one tag
vocabulary, per-detection verdicts, the combined check-and-describe request,
tags as evidence, AI insights and the AI near-miss check."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from tests.support import add_event


def _iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


def _det(label, confidence=0.8, x=0.1, zone=None):
    return {'label': label, 'confidence': confidence, 'zone_name': zone,
            'box': {'x': x, 'y': 0.1, 'width': 0.2, 'height': 0.3}}


@pytest.fixture
def db(tmp_path):
    from app.database import EventDatabase

    return EventDatabase(str(tmp_path / 'db.sqlite3'))


def _event(db, detections, *, hours_ago=1.0, metadata=None, snapshot='snap.jpg'):
    return add_event(db, created_at=_iso(hours_ago), source='rtsp', snapshot_path=snapshot,
                     detections=detections, metadata=metadata or {'camera_id': 'cam-1', 'camera_name': 'Gate'})


# ---------------------------------------------------------------------------
# One vocabulary
# ---------------------------------------------------------------------------

def test_model_words_map_onto_detector_classes():
    vocab = importlib.import_module('app.label_vocabulary')
    assert vocab.canonical_label('Kitten') == 'cat'
    assert vocab.canonical_label('kittens') == 'cat'
    assert vocab.canonical_label('delivery driver') == 'person'
    assert vocab.canonical_label('ute') == 'truck'
    assert vocab.canonical_label('ladder') is None
    assert vocab.canonical_tag('Sedan') == 'car' and vocab.canonical_tag('Ladder') == 'ladder'
    assert vocab.family('puppy') == 'animal' and vocab.family('ladder') is None


def test_ai_tags_are_stored_in_the_detectors_words():
    av = importlib.import_module('app.ai_verification')
    record = av._description_record('A kitten by a ladder.', ['kitten', 'ladder', 'puppy'],
                                    [{'label': 'dog'}], {}, 0.0)
    # "puppy" repeats the dog detection; "kitten" becomes "cat".
    assert record['tags'] == ['cat', 'ladder']
    # Tag alert rules still see the model's own words as well.
    assert {'kitten', 'cat', 'puppy', 'dog'} <= set(record['_model_tags'])


# ---------------------------------------------------------------------------
# Per-detection verdicts
# ---------------------------------------------------------------------------

def test_answers_become_per_detection_verdicts_including_groups():
    av = importlib.import_module('app.ai_verification')
    verdicts = av.detection_verdicts(
        {'animal': {'present': True, 'reason': 'a cat'},
         'person': {'present': False, 'reason': 'a coat', 'instead': 'none'},
         'car': {'present': None, 'unsure': True},
         'truck': {'present': None, 'error': 'timeout'}},
        [{'label': 'cat'}, {'label': 'dog'}, {'label': 'person'}, {'label': 'car'}, {'label': 'truck'},
         {'label': 'motion'}],
    )
    assert verdicts['cat']['status'] == verdicts['dog']['status'] == 'confirmed'
    assert verdicts['person']['status'] == 'rejected' and 'looks like none' in verdicts['person']['reason']
    assert verdicts['car']['status'] == 'unsure'
    assert 'truck' not in verdicts and 'motion' not in verdicts


def test_detection_verdicts_are_stored_and_reclassification_wins(db):
    event_id = _event(db, [_det('dog'), _det('car', x=0.6)])
    db.set_detection_verdicts(event_id, {'dog': {'status': 'rejected', 'reason': 'a cat'},
                                         'car': {'status': 'confirmed'}}, source='check')
    db.reclassify_event_detections(event_id, {'dog': 'cat'}, reason='Reclassified dog as cat.')
    # A later verdict for the new label never overwrites the reclassification.
    db.set_detection_verdicts(event_id, {'cat': {'status': 'rejected'}}, source='verification')
    by_label = {d['label']: d for d in db.get_event(event_id)['detections']}
    assert by_label['cat']['ai_status'] == 'reclassified'
    assert by_label['cat']['original_label'] == 'dog'
    assert (by_label['car']['ai_status'], by_label['car']['ai_source']) == ('confirmed', 'check')


def test_ai_check_filter_finds_events_by_detection_verdict(db):
    confirmed = _event(db, [_det('person')])
    rejected = _event(db, [_det('person')])
    _event(db, [_det('person')])
    db.set_detection_verdicts(confirmed, {'person': {'status': 'confirmed'}}, source='check')
    db.set_detection_verdicts(rejected, {'person': {'status': 'rejected'}}, source='check')
    ids = {e['id'] for e in db.search_events_page(ai_check='object_confirmed')[0]}
    assert ids == {confirmed}
    assert {e['id'] for e in db.search_events_page(ai_check='object_rejected')[0]} == {rejected}


# ---------------------------------------------------------------------------
# One request per event; tags as evidence
# ---------------------------------------------------------------------------

@pytest.fixture
def fp(monkeypatch, db):
    module = importlib.import_module('app.false_positives')
    av = importlib.import_module('app.ai_verification')
    monkeypatch.setattr(module._state, 'database', db)
    monkeypatch.setattr(av._state, 'database', db)
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')
    monkeypatch.setattr(av, 'focus_image', lambda image, *_a, **_k: image)
    monkeypatch.setattr(module, 'effective_false_positive_settings',
                        lambda: {**module.DEFAULT_FALSE_POSITIVE_SETTINGS, 'ai_check': True})
    return module


class CombinedVerifier:
    """Answers the check and the description in one request."""

    def __init__(self, verdicts, text='A cat on the path.', tags=('cat',)):
        self.verdicts, self.text, self.tags = verdicts, text, list(tags)
        self.requests = 0
        self.candidates = {}

    def check(self, _image, labels, _camera='', *, content=None, candidates=None, **_kw):
        self.requests += 1
        self.candidates.update(candidates or {})
        return {label: self.verdicts[label] for label in labels}, ((self.text, self.tags) if content else None)

    def ask(self, _image, label, _camera='', candidates=None, **_kw):
        self.requests += 1
        self.candidates[label] = candidates
        return self.verdicts[label]


def test_check_and_describe_share_one_request(fp, db):
    av = importlib.import_module('app.ai_verification')
    event_id = _event(db, [_det('dog')])
    event = db.get_event(event_id)
    verifier = CombinedVerifier({'dog': (False, 'a cat', 'cat')})
    record, description = fp.check_and_describe_event(
        event, av.DEFAULT_AI_VERIFICATION_SETTINGS, verifier=verifier, content='both')
    assert verifier.requests == 1
    assert record['status'] == 'reclassified'
    assert description['text'] == 'A cat on the path.'


def test_describing_an_event_runs_its_check_in_the_same_request(fp, db, monkeypatch):
    av = importlib.import_module('app.ai_verification')
    event_id = _event(db, [_det('dog')])
    verifier = CombinedVerifier({'dog': (False, 'a cat', 'cat')})
    monkeypatch.setattr(av, 'VisionVerifier', lambda *_a, **_k: verifier)
    monkeypatch.setattr(av, 'describe_event', lambda *_a, **_k: pytest.fail('described twice'))
    text = av._describe_and_store_claimed(db.get_event(event_id), event_id, av.DEFAULT_AI_VERIFICATION_SETTINGS,
                                          'Gate', evaluate_rules=False, background=True, content='both')
    assert text == 'A cat on the path.' and verifier.requests == 1
    stored = db.get_event(event_id)
    assert stored['false_positive'] == 0 and stored['detections'][0]['label'] == 'cat'
    assert stored['metadata']['ai_description']['text'] == 'A cat on the path.'


def test_combined_check_only_for_unjudged_live_object_events(fp, db):
    event = db.get_event(_event(db, [_det('dog')]))
    assert fp.combined_check_applies(event)
    assert not fp.combined_check_applies({**event, 'false_positive': 0})
    assert not fp.combined_check_applies({**event, 'source': 'upload'})
    assert not fp.combined_check_applies({**event, 'detections': [_det('motion')]})


def test_contradicting_tags_flag_the_event_and_are_offered_first(fp, db):
    assert fp.tag_contradictions([_det('dog'), _det('car')], ['Kitten', 'ladder']) == {'dog': 'cat'}
    # A dog and a car in one picture contradict nothing.
    assert fp.tag_contradictions([_det('dog')], ['car', 'puppy']) == {}
    flagged = _event(db, [_det('dog')], hours_ago=2)
    plain = _event(db, [_det('person')], hours_ago=1)
    assert fp.note_tag_evidence(flagged, db.get_event(flagged), ['kitten']) == {'dog': 'cat'}
    fp._attempted.clear()
    first = fp._next_candidate(fp.effective_false_positive_settings(), datetime.now(UTC))
    assert first['id'] == flagged  # older, but its tags contradict the detection
    assert fp._next_candidate(fp.effective_false_positive_settings(), datetime.now(UTC))['id'] == plain
    verifier = CombinedVerifier({'dog': (False, 'a cat', 'cat')})
    fp.check_event(db.get_event(flagged), importlib.import_module('app.ai_verification').DEFAULT_AI_VERIFICATION_SETTINGS,
                   verifier=verifier)
    assert verifier.candidates['dog'][0] == 'cat'


# ---------------------------------------------------------------------------
# AI insights
# ---------------------------------------------------------------------------

def test_floor_suggestion_needs_mostly_rejections_below_and_few_confirmations_lost():
    insights = importlib.import_module('app.ai_insights')
    judged = [(0.40 + i * 0.01, 'rejected') for i in range(10)] + [(0.70 + i * 0.01, 'confirmed') for i in range(10)]
    found = insights.suggest_floor(judged)
    assert found['min_confidence'] == 0.5
    assert (found['rejected_below'], found['confirmed_lost']) == (10, 0)
    # Rejections spread over every confidence: no floor helps.
    mixed = [(0.4 + (i % 10) * 0.05, 'rejected' if i % 2 else 'confirmed') for i in range(30)]
    assert insights.suggest_floor(mixed) is None
    assert insights.suggest_floor(judged[:5]) is None  # too few verdicts


def test_insights_report_raises_and_frequent_untracked_tags(db, monkeypatch):
    insights = importlib.import_module('app.ai_insights')
    monkeypatch.setattr(insights._state, 'database', db)
    camera = {'id': 'cam-1', 'name': 'Driveway', 'detection': {'zones': [
        {'name': 'Drive', 'object_rules': [{'label': 'person', 'min_confidence': 0.4}],
         'ai_tags': {'enabled': True, 'tags': ['ladder']}},
    ]}}
    monkeypatch.setattr(insights, '_cameras', lambda: [camera])
    for i in range(10):
        event_id = _event(db, [_det('person', 0.42 + i * 0.005, zone='Drive')],
                          metadata={'camera_id': 'cam-1', 'ai_description': {'tags': ['parcel', 'ladder', 'kitten']}})
        db.set_detection_verdicts(event_id, {'person': {'status': 'rejected'}}, source='check')
        event_id = _event(db, [_det('person', 0.75, zone='Drive')], metadata={'camera_id': 'cam-1'})
        db.set_detection_verdicts(event_id, {'person': {'status': 'confirmed'}}, source='check')
    tuning = insights.tuning_suggestions()
    assert len(tuning) == 1
    assert (tuning[0]['camera_name'], tuning[0]['label'], tuning[0]['zones']) == ('Driveway', 'person', ['Drive'])
    assert tuning[0]['current_min_confidence'] == 0.4 and tuning[0]['min_confidence'] >= 0.5
    tags = insights.frequent_tags()
    # "ladder" is already watched and "kitten" is a detector class (cat).
    assert tags == [{'camera_id': 'cam-1', 'camera_name': 'Driveway', 'tags': [{'tag': 'parcel', 'count': 10}]}]


def _fake_camera_store(monkeypatch, insights, cameras):
    """Back ``insights._update_camera`` with an in-memory camera list; returns
    the list of saved snapshots."""
    import copy as _copy

    store = {'cameras': cameras}
    saved = []

    class _Db:
        def get_setting(self, _key):
            return None

        def set_setting(self, key, value, _now):
            assert key == 'cameras'
            store['cameras'] = value
            saved.append(value)

    monkeypatch.setattr(insights, '_cameras', lambda: _copy.deepcopy(store['cameras']))
    monkeypatch.setattr('app.payload_validators.validate_camera_settings',
                        lambda edited, current=None, index=None: edited)
    monkeypatch.setattr('app.profile_presets.link_camera_profiles', lambda cams, _presets: (cams, False))
    monkeypatch.setattr(insights._state, 'database', _Db())
    return saved


def test_applying_a_suggestion_raises_only_lower_rules(monkeypatch):
    insights = importlib.import_module('app.ai_insights')
    camera = {'id': 'cam-1', 'detection': {'zones': [
        {'name': 'A', 'object_rules': [{'label': 'person', 'min_confidence': 0.4}]},
        {'name': 'B', 'object_rules': [{'label': 'person', 'min_confidence': 0.7}, {'label': 'car', 'min_confidence': 0.3}]},
    ]}}
    saved = _fake_camera_store(monkeypatch, insights, [camera])
    assert insights.apply_min_confidence('cam-1', 'person', 0.55, lambda *_: None) == 1
    assert insights.apply_min_confidence('cam-1', 'person', 0.55, lambda *_: None) == 0
    assert len(saved) == 1  # nothing to raise: nothing written
    rules = [rule for zone in saved[-1][0]['detection']['zones'] for rule in zone['object_rules']]
    assert [r['min_confidence'] for r in rules] == [0.55, 0.7, 0.3]
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        insights.apply_min_confidence('cam-1', 'person', 1.5, lambda *_: None)
    with pytest.raises(HTTPException):
        insights.apply_min_confidence('missing', 'person', 0.5, lambda *_: None)


# ---------------------------------------------------------------------------
# AI near-miss check
# ---------------------------------------------------------------------------

class Detector:
    """Always unsure: the crop re-check never clears the threshold."""

    def detect_frame(self, _frame, confidence=0.0):
        return [{'label': 'person', 'confidence': 0.3, 'box': {'x': 0.2, 'y': 0.2, 'width': 0.3, 'height': 0.5}}]


def test_second_look_rejects_go_to_the_model_and_a_confirmation_passes_next_cycle(monkeypatch):
    near = importlib.import_module('app.ai_near_miss')
    second_look = importlib.import_module('app.second_look')
    near.reset()
    queued = []

    class Pool:
        def submit(self, fn, *args, priority=None, block=None, label=None):
            queued.append((fn, args))
            return True

    monkeypatch.setattr('app.postprocess_pool.verification_pool', lambda: Pool())
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    det = {'label': 'person', 'confidence': 0.35, 'box': {'x': 0.3, 'y': 0.3, 'width': 0.1, 'height': 0.2}}
    kwargs = {
        'threshold': 0.5,
        'ai_confirmed': lambda d: near.is_confirmed('cam-1', d),
        'ai_submit': lambda d: near.submit('cam-1', 'Gate', frame, d),
    }
    assert second_look.confirm_borderline_detections(Detector(), frame, [det], **kwargs) == []
    assert len(queued) == 1
    # The same object is not asked about twice while the answer is pending.
    second_look.confirm_borderline_detections(Detector(), frame, [det], **kwargs)
    assert len(queued) == 1

    class Yes:
        def ask(self, *_a, **_k):
            return True, 'a person in the dark'

    monkeypatch.setattr('app.ai_verification.effective_ai_verification_settings',
                        lambda: {'server_url': 'http://model', 'model': 'm'})
    monkeypatch.setattr('app.ai_near_miss.log_ai_diagnostic', lambda *_a, **_k: None, raising=False)
    fn, args = queued[0]
    fn(*args, verifier=Yes())
    moved = {**det, 'box': {'x': 0.31, 'y': 0.3, 'width': 0.1, 'height': 0.2}}
    kept = second_look.confirm_borderline_detections(Detector(), frame, [moved], **kwargs)
    assert len(kept) == 1 and kept[0]['ai_near_miss'] and kept[0]['confidence'] == 0.5
    assert kept[0]['detector_confidence'] == 0.35
    # A different label in the same place is not confirmed.
    assert not near.is_confirmed('cam-1', {**moved, 'label': 'dog'})
    near.reset()


def test_near_miss_check_needs_second_look_and_its_own_switch():
    near = importlib.import_module('app.ai_near_miss')
    assert not near.enabled({'object_detection_ai_near_miss': True})
    assert not near.enabled({'object_detection_second_look': True})
    assert near.enabled({'object_detection_second_look': True, 'object_detection_ai_near_miss': True})


# ---------------------------------------------------------------------------
# Per-camera near-miss check; one request for alert events
# ---------------------------------------------------------------------------

def test_camera_profiles_can_turn_the_near_miss_check_on_or_off():
    settings = importlib.import_module('app.recording_settings')
    assert 'object_detection_ai_near_miss' in settings.CAMERA_MOTION_PROFILE_FIELDS
    assert settings._normalize_profile_value('object_detection_ai_near_miss', 'false') is False
    assert settings._normalize_profile_value('object_detection_ai_near_miss', 'true') is True


def test_alert_verification_offers_every_class_only_with_the_check_on(fp, monkeypatch):
    rules = {'dog': ['cat']}
    widened, asked = fp.widen_relabel_candidates(rules, ['dog', 'animal'])
    assert asked
    # The zone's own rule labels stay first; the label itself and a group's
    # members are never offered.
    assert widened['dog'][0] == 'cat' and 'dog' not in widened['dog'] and 'person' in widened['dog']
    assert 'cat' not in widened['animal'] and 'person' in widened['animal']
    monkeypatch.setattr(fp, 'effective_false_positive_settings', lambda: dict(fp.DEFAULT_FALSE_POSITIVE_SETTINGS))
    assert fp.widen_relabel_candidates(rules, ['dog']) == (rules, False)


def test_a_bare_no_from_a_full_alert_check_is_reused_not_asked_again(fp, db):
    av = importlib.import_module('app.ai_verification')
    metadata = {'camera_id': 'cam-1', 'ai_verification': {'asked_instead': True, 'labels': {
        'person': {'present': False, 'reason': 'a coat on a hook'},
    }}}
    event = db.get_event(_event(db, [_det('person')], metadata=metadata))
    verifier = CombinedVerifier({})
    record = fp.check_event(event, av.DEFAULT_AI_VERIFICATION_SETTINGS, verifier=verifier)
    assert verifier.requests == 0
    assert record['status'] == 'false_positive'
    # Without the full list, the same "no" is asked again.
    metadata['ai_verification'].pop('asked_instead')
    event = db.get_event(_event(db, [_det('person')], metadata=metadata))
    verifier = CombinedVerifier({'person': (False, 'a coat', None)})
    fp.check_event(event, av.DEFAULT_AI_VERIFICATION_SETTINGS, verifier=verifier)
    assert verifier.requests == 1


def test_a_relabel_to_a_class_without_a_rule_raises_no_alert(monkeypatch):
    relabel = importlib.import_module('app.ai_relabel')
    monkeypatch.setattr('app.event_presence.claim_presence', lambda *_a, **_k: True)
    triggered = [{'label': 'dog', 'rule_name': 'Dog rule', 'confidence': 0.6}]
    rules = [{'name': 'Dog rule', 'object': 'dog', 'zone_id': 'z1', 'email_enabled': True}]
    record = {'labels': {'dog': {'present': False, 'instead': 'horse'}}}
    assert relabel.relabel_alerts(triggered, record, rules, {'detections': [_det('dog')]}, 1, 'cam-1') == []


# ---------------------------------------------------------------------------
# Ignore areas
# ---------------------------------------------------------------------------

def _row(status, cx, cy, event_id=1):
    return {'ai_status': status, 'x': cx - 0.02, 'y': cy - 0.02, 'width': 0.04, 'height': 0.04,
            'event_id': event_id, 'created_at': f'2030-01-0{event_id % 9 + 1}'}


def test_areas_are_suggested_where_rejections_cluster_without_confirmations():
    insights = importlib.import_module('app.ai_insights')
    bush = [_row('rejected', 0.12 + i * 0.005, 0.70, event_id=i) for i in range(8)]
    elsewhere = [_row('confirmed', 0.6, 0.4) for _ in range(5)]
    found = insights.suggest_areas(bush + elsewhere)
    assert len(found) == 1
    area = found[0]['area']
    assert area['x'] < 0.12 < area['x'] + area['width'] and area['y'] < 0.70 < area['y'] + area['height']
    assert (found[0]['rejected'], found[0]['confirmed']) == (8, 0)
    # A real dog that often walks there too: not suggested.
    assert insights.suggest_areas(bush + [_row('confirmed', 0.13, 0.70) for _ in range(3)]) == []
    assert insights.suggest_areas(bush[:4]) == []  # too few rejections


def test_an_ignore_area_stops_only_that_label_matching_only_inside_it():
    schema = importlib.import_module('app.zone_schema')
    zd = importlib.import_module('app.zone_detection')
    zone = schema.normalize_monitoring_zones([{
        'id': 'yard', 'name': 'Yard', 'x': 0, 'y': 0, 'width': 1, 'height': 1,
        'object_rules': [{'label': 'dog'}, {'label': 'person'}],
        'ignore_areas': [{'label': 'Dog', 'x': 0.1, 'y': 0.6, 'width': 0.1, 'height': 0.2, 'source': 'ai'},
                         {'label': 'cat', 'x': 'bad'}],
    }])[0]
    assert zone['ignore_areas'] == [{'label': 'dog', 'x': 0.1, 'y': 0.6, 'width': 0.1, 'height': 0.2, 'source': 'ai'}]
    in_bush = {'label': 'dog', 'box': {'x': 0.12, 'y': 0.65, 'width': 0.04, 'height': 0.04}}
    assert not zd.detection_matches_zone(in_bush, zone)
    assert zd.detection_matches_zone({**in_bush, 'label': 'person'}, zone)
    assert zd.detection_matches_zone({**in_bush, 'box': {'x': 0.5, 'y': 0.2, 'width': 0.04, 'height': 0.04}}, zone)


def test_applying_and_removing_an_ignore_area(monkeypatch):
    insights = importlib.import_module('app.ai_insights')
    camera = {'id': 'cam-1', 'detection': {'zones': [
        {'id': 'yard', 'name': 'Yard', 'x': 0, 'y': 0, 'width': 0.5, 'height': 1, 'object_rules': [{'label': 'dog'}]},
        {'id': 'road', 'name': 'Road', 'x': 0.5, 'y': 0, 'width': 0.5, 'height': 1, 'object_rules': [{'label': 'dog'}]},
        {'id': 'porch', 'name': 'Porch', 'x': 0, 'y': 0, 'width': 0.5, 'height': 1, 'object_rules': [{'label': 'person'}]},
    ]}}
    saved = _fake_camera_store(monkeypatch, insights, [camera])
    area = {'x': 0.1, 'y': 0.6, 'width': 0.1, 'height': 0.2}
    assert insights.apply_ignore_area('cam-1', 'dog', area, None) == 1
    yard = saved[-1][0]['detection']['zones'][0]
    assert yard['ignore_areas'] == [{'label': 'dog', **area, 'source': 'ai'}]
    assert 'ignore_areas' not in saved[-1][0]['detection']['zones'][1]  # the area is not in Road
    assert 'ignore_areas' not in saved[-1][0]['detection']['zones'][2]  # Porch has no dog rule
    assert insights.remove_ignore_area('cam-1', 'yard', 0, None)
    assert saved[-1][0]['detection']['zones'][0]['ignore_areas'] == []
    from fastapi import HTTPException

    for bad in ({'x': 2, 'y': 0, 'width': 0.1, 'height': 0.1}, {'x': 0.1}, 'nope'):
        with pytest.raises(HTTPException):
            insights.apply_ignore_area('cam-1', 'dog', bad, None)
    with pytest.raises(HTTPException):
        insights.remove_ignore_area('cam-1', 'yard', 5, None)
