"""AI tag alerts: zone rules that fire when the vision model names something.

* Rules match the model's tags, its description sentence, or both (per rule),
  with plural forms and multi-word terms.
* A rule applies only to events in its zone (or a full-frame zone), needs an
  email/push channel inside its notify window, and honours its cooldown -
  spent only by an alert that is actually sent.
* A fired rule adds an alert to the described event, flags it alerted and
  queues a notification led by an "unconfirmed" line and the description.
* Cameras with a rule get every event described; backfilled events never alert.
"""
from __future__ import annotations

import importlib

import pytest

from tests.support import _load_app

from app.database import EventDatabase


@pytest.fixture
def ata():
    module = importlib.import_module('app.ai_tag_alerts')
    module._cooldowns.clear()
    return module


def _rule(**overrides):
    rule = {'enabled': True, 'name': 'Ladders', 'tags': ['ladder'], 'match': 'both', 'cooldown_seconds': 300,
            'email_enabled': False, 'push_enabled': True, 'email_recipients': [],
            'notify_start': None, 'notify_end': None}
    rule.update(overrides)
    return rule


def _zone(rule=None, **overrides):
    zone = {'id': 'gate', 'name': 'Gate', 'enabled': True, 'x': 0.5, 'y': 0.5, 'width': 0.3, 'height': 0.3,
            'ai_tags': rule if rule is not None else _rule()}
    zone.update(overrides)
    return zone


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('mode', 'tags', 'text', 'expected'), [
    ('tags', ['ladder'], 'A man walks by.', ['ladder']),
    ('tags', [], 'A man carries a ladder.', []),
    ('description', [], 'A man carries two ladders.', ['ladder']),
    ('description', ['ladder'], 'A man walks by.', []),
    ('both', ['ladders'], '', ['ladder']),
    ('both', [], 'Ladder leaning on the fence.', ['ladder']),
    ('both', [], 'A bladder-shaped balloon.', []),        # whole words only
])
def test_match_modes(ata, mode, tags, text, expected):
    assert ata.matched_terms(_rule(match=mode), tags, text) == expected


def test_multi_word_terms_and_plurals(ata):
    rule = _rule(tags=['hi-vis vest', 'wheelie bin', 'box'], match='description')
    assert ata.matched_terms(rule, [], 'A courier in a hi-vis vest drags two wheelie bins past boxes.') == [
        'hi-vis vest', 'wheelie bin', 'box']
    assert ata.matched_terms(_rule(tags=['battery'], match='tags'), ['batteries'], '') == ['battery']


def test_zone_membership(ata):
    zone = _zone()
    assert ata.event_in_zone({'detections': [{'label': 'person', 'zone_name': 'Gate'}]}, zone)
    assert ata.event_in_zone({'detections': [{'label': 'motion', 'zone_name': 'gate'}]}, zone)
    assert not ata.event_in_zone({'detections': [{'label': 'person', 'zone_name': 'Porch'}]}, zone)
    assert not ata.event_in_zone({'detections': []}, zone)
    full = _zone(x=0, y=0, width=1, height=1)
    assert ata.event_in_zone({'detections': []}, full)


def test_schema_normalises_ai_tag_rules():
    from app.zone_schema import normalize_monitoring_zones

    zone = normalize_monitoring_zones([{'name': 'Gate', 'ai_tags': {
        'tags': 'Ladder, hi-vis vest, <script>, ladder, a b c d', 'match': 'sometimes', 'cooldown_seconds': -5,
        'push_enabled': True, 'notify_start': '22:00', 'notify_end': 'nope'}}])[0]
    rule = zone['ai_tags']
    assert rule['tags'] == ['ladder', 'hi-vis vest']
    assert rule['match'] == 'both' and rule['cooldown_seconds'] == 0
    assert rule['push_enabled'] is True and rule['notify_start'] == '22:00' and rule['notify_end'] is None
    assert 'ai_tags' not in normalize_monitoring_zones([{'name': 'Porch'}])[0]


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------

class _Db:
    def __init__(self):
        self.alerts: list[dict] = []
        self.flagged: list[int] = []

    def add_alert(self, **kwargs):
        self.alerts.append(kwargs)

    def mark_event_alert_triggered(self, event_id):
        self.flagged.append(event_id)
        return True


@pytest.fixture
def fire_env(ata, monkeypatch):
    dispatch = importlib.import_module('app.alert_dispatch')
    db = _Db()
    sent: list[tuple] = []
    monkeypatch.setattr(ata._state, 'database', db)
    monkeypatch.setattr(dispatch, 'submit_alert_notification',
                        lambda _fn, triggered, event_id, rules: sent.append((triggered, event_id, rules)) or True)
    monkeypatch.setattr(dispatch, '_rule_notify_active_now', lambda _rule: True)

    def camera(*zones):
        cam = {'id': 'cam', 'name': 'Front', 'detection': {'zones': list(zones)}}
        monkeypatch.setattr(ata, '_camera_settings', lambda _cid: cam)
        return cam

    return db, sent, camera


EVENT = {'id': 5, 'metadata': {'camera_id': 'cam'}, 'detections': [{'label': 'person', 'zone_name': 'Gate'}]}
RECORD = {'text': 'A man carries a ladder past the gate.', 'tags': ['ladder']}


def test_matching_rule_alerts_and_notifies(ata, fire_env):
    db, sent, camera = fire_env
    camera(_zone())
    assert ata.evaluate_event(5, EVENT, RECORD) == 1
    assert db.flagged == [5]
    assert db.alerts[0]['rule_name'] == 'Gate · Ladders' and db.alerts[0]['label'] == 'ladder'
    (triggered, event_id, rules), = sent
    assert event_id == 5 and rules[0]['name'] == triggered[0]['rule_name'] == 'Gate · Ladders'
    assert triggered[0]['ai_description'].startswith('AI tag alert (unconfirmed): Ladder on Front (Gate).')
    assert triggered[0]['ai_description'].endswith('A man carries a ladder past the gate.')


def test_cooldown_blocks_repeat_but_only_after_a_sent_alert(ata, fire_env):
    db, sent, camera = fire_env
    camera(_zone(_rule(push_enabled=False)))            # no channel: nothing sent
    assert ata.evaluate_event(5, EVENT, RECORD) == 0
    camera(_zone())                                      # now with push
    assert ata.evaluate_event(5, EVENT, RECORD) == 1     # cooldown was not spent above
    assert ata.evaluate_event(6, {**EVENT, 'id': 6}, RECORD) == 0
    assert len(sent) == 1


def test_no_alert_outside_zone_notify_window_or_when_disabled(ata, fire_env, monkeypatch):
    db, sent, camera = fire_env
    camera(_zone())
    assert ata.evaluate_event(5, {**EVENT, 'detections': [{'label': 'person', 'zone_name': 'Porch'}]}, RECORD) == 0
    camera(_zone(_rule(enabled=False)))
    assert ata.evaluate_event(5, EVENT, RECORD) == 0
    dispatch = importlib.import_module('app.alert_dispatch')
    monkeypatch.setattr(dispatch, '_rule_notify_active_now', lambda _rule: False)
    camera(_zone())
    assert ata.evaluate_event(5, EVENT, RECORD) == 0
    assert sent == [] and db.alerts == []


def test_evaluate_never_raises(ata, monkeypatch):
    monkeypatch.setattr(ata, '_camera_settings', lambda _cid: (_ for _ in ()).throw(RuntimeError('boom')))
    assert ata.evaluate_event(1, EVENT, RECORD) == 0


# ---------------------------------------------------------------------------
# Integration with descriptions
# ---------------------------------------------------------------------------

def test_tag_rule_cameras_are_always_described(monkeypatch):
    av = importlib.import_module('app.ai_verification')
    ata = importlib.import_module('app.ai_tag_alerts')
    settings = {**av.DEFAULT_AI_VERIFICATION_SETTINGS, 'describe_events': 'off', 'camera_ids': ['other']}
    monkeypatch.setattr(ata, 'camera_has_rules', lambda camera_id: camera_id == 'cam')
    assert av.camera_describe_mode('cam', settings) == 'all'
    assert av.camera_describe_mode('elsewhere', settings) == 'off'
    assert av.describes_camera('cam', settings) is True


def test_end_to_end_alert_on_a_described_event(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    av = importlib.import_module('app.ai_verification')
    ata = importlib.import_module('app.ai_tag_alerts')
    dispatch = importlib.import_module('app.alert_dispatch')
    state = importlib.import_module('app.state')
    ata._cooldowns.clear()
    db = state.database
    db.set_setting('cameras', [{'id': 'cam', 'name': 'Front', 'stream_url': 'rtsp://x/s', 'enabled': False,
                                'detection': {'zones': [_zone()]}}], '2026-09-28T00:00:00+00:00')
    event_id = db.add_event(created_at='2026-09-28T01:00:00+00:00', source='rtsp', snapshot_path='s.jpg',
                            detections=[{'label': 'person', 'confidence': 0.9, 'x': 0.6, 'y': 0.6,
                                         'width': 0.1, 'height': 0.2, 'zone_name': 'Gate'}],
                            metadata={'camera_id': 'cam', 'camera_name': 'Front'})
    sent: list = []
    monkeypatch.setattr(dispatch, 'submit_alert_notification', lambda _fn, t, e, r: sent.append((t, e)) or True)
    monkeypatch.setattr(dispatch, '_rule_notify_active_now', lambda _rule: True)
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')
    monkeypatch.setattr(av.VisionVerifier, 'describe',
                        lambda _s, _img, _cam='': ('A man carries a ladder past the gate.', ['ladder']))

    # Global describe mode is off, but this camera has a tag rule, so the
    # event is queued for describing. Record the job instead of letting a real
    # pool worker run it concurrently with the direct call below.
    pools = importlib.import_module('app.postprocess_pool')
    queued: list = []

    class _Pool:
        def submit(self, fn, *args, **_kwargs):
            queued.append((fn, args))
            return True

    monkeypatch.setattr(pools, 'verification_pool', lambda: _Pool())
    assert av.submit_event_description(event_id, camera_id='cam', camera_name='Front') is True
    (job, args), = queued
    assert job is av.describe_only and args == (event_id, 'Front', 'cam')
    job(*args)

    event = db.get_event(event_id)
    assert event['alert_triggered'] == 1
    assert event['alert']['rule_name'] == 'Gate · Ladders'
    assert [e for _t, e in sent] == [event_id]

    # A backfill of the same kind of event never alerts.
    second = db.add_event(created_at='2026-09-28T01:05:00+00:00', source='rtsp', snapshot_path='s.jpg',
                          detections=[{'label': 'person', 'confidence': 0.9, 'x': 0.6, 'y': 0.6,
                                       'width': 0.1, 'height': 0.2, 'zone_name': 'Gate'}],
                          metadata={'camera_id': 'cam', 'camera_name': 'Front'})
    ata._cooldowns.clear()
    av._describe_and_store(db.get_event(second), second, av.effective_ai_verification_settings(), 'Front',
                           evaluate_rules=False)
    assert db.get_event(second)['alert_triggered'] == 0
    assert len(sent) == 1


def test_mark_event_alert_triggered(tmp_path):
    db = EventDatabase(str(tmp_path / 'flag.sqlite3'))
    event_id = db.add_event(created_at='2026-09-28T01:00:00+00:00', source='rtsp', snapshot_path=None, detections=[])
    assert db.mark_event_alert_triggered(event_id) is True
    assert db.get_event(event_id)['alert_triggered'] == 1
    assert db.mark_event_alert_triggered(event_id + 50) is False
