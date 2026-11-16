"""AI alert verification: reason-first prompts, the "unsure" verdict, robust
parsing, foreground priority for the model, the alert time budget, model
health, the Events AI Check filter and the AI page stats."""

from __future__ import annotations

import importlib
import threading
import time
from datetime import UTC, datetime

import pytest


@pytest.fixture
def av():
    return importlib.import_module('app.ai_verification')


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_prompts_ask_for_the_reason_before_the_verdict(av):
    for prompt in (av.build_prompt('person'), av.build_check_prompt(['person', 'car'])):
        assert prompt.index('"reason"') < prompt.index('"present": true')
        assert 'infrared' in prompt and '"unsure"' in prompt
        assert 'nothing clear' not in prompt


def test_prompts_speak_labels_as_words(av):
    assert '"fire hydrant"' in av.build_prompt('fire_hydrant')
    prompt = av.build_check_prompt(['traffic_light'])
    assert '"traffic light"' in prompt and 'traffic_light' not in prompt
    verdicts, _ = av.parse_check_reply(
        '{"checks": {"traffic light": {"reason": "a lamp", "present": false}}}', ['traffic_light'])
    assert verdicts == {'traffic_light': (False, 'a lamp')}


# ---------------------------------------------------------------------------
# Parsing and the unsure verdict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('reply', 'expected'), [
    ('{"reason": "a {blurry} shape by the gate", "present": false}', (False, 'a {blurry} shape by the gate')),
    ('{"reason": "too dark to tell", "present": "unsure"}', ('unsure', 'too dark to tell')),
    ('Unsure - it is very dark.', ('unsure', 'it is very dark.')),
])
def test_parse_verdict_handles_braces_and_unsure(av, reply, expected):
    assert av.parse_verdict(reply) == expected


def test_unsure_is_delivered_and_marked_unverified(av, monkeypatch):
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')

    class Verifier:
        def check(self, _image, _labels, _camera='', **_kw):
            return {'person': ('unsure', 'too dark'), 'car': (True, 'a car')}, None

    record = av.verify_event({'detections': []}, ['person', 'car'], av.DEFAULT_AI_VERIFICATION_SETTINGS,
                             verifier=Verifier())
    assert record['labels']['person'] == {'present': None, 'unsure': True, 'reason': 'too dark'}
    assert record['status'] == 'unsure'


def test_unsure_alert_is_forwarded_and_logged(av, monkeypatch):
    dispatch = importlib.import_module('app.alert_dispatch')
    forwarded, logged, stored = [], [], {}
    monkeypatch.setattr(dispatch, 'submit_alert_notification',
                        lambda _fn, triggered, _event_id, _rules: forwarded.append(list(triggered)) or True)
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')
    monkeypatch.setattr(av, 'log_ai_diagnostic', lambda kind, *_a, **_k: logged.append(kind))

    class Db:
        def get_setting(self, _key):
            return dict(av.DEFAULT_AI_VERIFICATION_SETTINGS)

        def get_event(self, event_id):
            return {'id': event_id, 'detections': []}

        def merge_event_metadata(self, event_id, patch):
            stored[event_id] = patch

    monkeypatch.setattr(av._state, 'database', Db())
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: dict(av.DEFAULT_AI_VERIFICATION_SETTINGS))
    monkeypatch.setattr(av.VisionVerifier, 'ask', lambda *_a, **_k: ('unsure', 'blurry'))
    triggered = [{'label': 'person', 'confidence': 0.5, 'ai_verify': True}]
    av.verify_and_forward(triggered, 5, [], 'Gate')
    assert forwarded == [triggered]
    assert stored[5]['ai_verification']['status'] == 'unsure'
    assert 'ai_verification_unsure' in logged


# ---------------------------------------------------------------------------
# The model gate: foreground first, budget not spent behind background work
# ---------------------------------------------------------------------------

def test_background_request_waits_for_a_waiting_foreground_one(av):
    gate = av._ModelGate()
    order = []
    assert gate.acquire(timeout=1)  # foreground holder
    fg = threading.Thread(target=lambda: gate.acquire(timeout=2) and (order.append('fg'), gate.release()))
    bg = threading.Thread(target=lambda: gate.acquire(timeout=2, background=True) and (order.append('bg'), gate.release()))
    fg.start()
    time.sleep(0.05)
    bg.start()
    time.sleep(0.05)
    gate.release()
    fg.join(3)
    bg.join(3)
    assert order == ['fg', 'bg']


def test_foreground_wait_behind_background_does_not_spend_its_timeout(av):
    gate = av._ModelGate()
    assert gate.acquire(timeout=1, background=True)
    got = []
    waiter = threading.Thread(target=lambda: got.append(gate.acquire(timeout=0.1)))
    waiter.start()
    time.sleep(0.3)  # three times the waiter's timeout
    assert got == []
    gate.release()
    waiter.join(2)
    assert got == [True]
    gate.release()


def test_foreground_wait_behind_foreground_still_times_out(av):
    gate = av._ModelGate()
    assert gate.acquire(timeout=1)
    started = time.monotonic()
    assert gate.acquire(timeout=0.1) is False
    assert time.monotonic() - started < 1
    gate.release()


def test_request_timeout_counts_the_wait_for_the_model(av, monkeypatch):
    verifier = av.VisionVerifier({**av.DEFAULT_AI_VERIFICATION_SETTINGS, 'timeout_seconds': 20})
    seen = []

    class Gate:
        def acquire(self, **_kw):
            return True

        def release(self):
            pass

    monkeypatch.setattr(av, '_model_request_lock', Gate())
    clock = iter([100.0, 112.0])
    monkeypatch.setattr(av.time, 'monotonic', lambda: next(clock, 112.0))
    monkeypatch.setattr(verifier, '_chat_request', lambda _body, timeout=None: seen.append(timeout) or {
        'choices': [{'message': {'content': 'OK'}}]})
    verifier.chat([])
    assert seen == [8.0]  # 20s budget, 12s spent waiting
    clock = iter([100.0, 119.0])
    verifier.chat([])
    assert seen[-1] == av._MIN_REQUEST_SECONDS  # never less than a usable request


def test_backlog_ignores_time_behind_background_captions(av, monkeypatch):
    monkeypatch.setattr(av, '_background_pool_seconds', 0.0)
    clock = av._background_pool_clock()
    submitted = time.monotonic() - 100
    assert av._queue_wait(submitted, clock) == pytest.approx(100, abs=1)
    av._add_background_pool_time(90)
    assert av._queue_wait(submitted, clock) == pytest.approx(10, abs=1)
    assert av._queue_wait(submitted, None) == pytest.approx(100, abs=1)


# ---------------------------------------------------------------------------
# Model health
# ---------------------------------------------------------------------------

def test_model_health_tracks_the_last_outcome(av, monkeypatch):
    monkeypatch.setattr(av, '_model_health', {'last_success_at': None, 'last_error': None, 'last_error_at': None})
    settings = dict(av.DEFAULT_AI_VERIFICATION_SETTINGS)
    assert av.model_health(settings)['state'] == 'unknown'
    verifier = av.VisionVerifier(settings)
    monkeypatch.setattr(verifier, '_chat_request', lambda *_a, **_k: (_ for _ in ()).throw(
        av.ModelUnavailableError('model server unreachable: refused')))
    with pytest.raises(av.VerificationError):
        verifier.chat([])
    health = av.model_health(settings)
    assert health['state'] == 'error' and 'unreachable' in health['last_error']
    monkeypatch.setattr(verifier, '_chat_request', lambda *_a, **_k: {'choices': [{'message': {'content': 'OK'}}]})
    time.sleep(0.001)
    verifier.chat([])
    assert av.model_health(settings)['state'] == 'ok'


# ---------------------------------------------------------------------------
# Events AI Check filter and the AI page stats
# ---------------------------------------------------------------------------

@pytest.fixture
def verdict_db(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'verdicts.sqlite3'))
    now = datetime.now(UTC).isoformat()
    ids = {}
    for name, record in {
        'confirmed': {'status': 'confirmed', 'latency_ms': 1000, 'delivered': True},
        'stopped': {'status': 'filtered', 'latency_ms': 3000, 'delivered': False},
        'partial': {'status': 'filtered', 'latency_ms': 2000, 'delivered': True},
        'unsure': {'status': 'unsure', 'latency_ms': 2000, 'delivered': True},
        'error': {'status': 'error', 'latency_ms': 20000},
        'skipped': {'status': 'skipped'},
        'plain': None,
    }.items():
        metadata = {'camera_id': 'gate'}
        if record:
            metadata['ai_verification'] = record
        ids[name] = db.add_event_with_alerts(created_at=now, source='gate', snapshot_path=None,
                                             detections=[], alerts=[], metadata=metadata)
    return db, ids


@pytest.mark.parametrize(('ai_check', 'expected'), [
    ('filtered', {'stopped', 'partial'}),
    ('confirmed', {'confirmed'}),
    ('unverified', {'unsure', 'error', 'skipped'}),
    (None, {'confirmed', 'stopped', 'partial', 'unsure', 'error', 'skipped', 'plain'}),
])
def test_events_ai_check_filter(verdict_db, ai_check, expected):
    db, ids = verdict_db
    items, _ = db.search_events_page(limit=50, ai_check=ai_check)
    assert {item['id'] for item in items} == {ids[name] for name in expected}


def test_verification_stats(av, verdict_db, monkeypatch):
    db, _ = verdict_db
    monkeypatch.setattr(av._state, 'database', db)
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: dict(av.DEFAULT_AI_VERIFICATION_SETTINGS))
    stats = av.verification_stats(7)
    assert (stats['confirmed'], stats['filtered'], stats['stopped'], stats['unverified'], stats['unsure']) == (1, 2, 1, 3, 1)
    assert stats['avg_latency_ms'] == 2000  # answered checks only: 1000, 3000, 2000, 2000
    assert stats['health']['state'] in {'ok', 'error', 'unknown'}


def test_connection_test_returns_the_image_the_model_saw(av, monkeypatch):
    import cv2
    import numpy as np

    ok, jpeg = cv2.imencode('.jpg', np.zeros((40, 60, 3), dtype=np.uint8))
    assert ok
    event = {'id': 3, 'snapshot_path': 'x.jpg', 'detections': [{'label': 'person'}], 'metadata': {}}
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: jpeg.tobytes())
    monkeypatch.setattr(av, '_latest_object_event', lambda: event)
    monkeypatch.setattr(av.VisionVerifier, 'list_models', lambda _self: [])
    monkeypatch.setattr(av.VisionVerifier, 'chat',
                        lambda _self, _messages, **_kw: '{"reason": "a person", "present": true}')
    result = av.run_connection_test(dict(av.DEFAULT_AI_VERIFICATION_SETTINGS))
    assert result['verification']['status'] == 'confirmed'
    assert result['image'].startswith('data:image/jpeg;base64,')
