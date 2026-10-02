"""End-to-end boundary regressions for the AI pipeline audit."""
from __future__ import annotations

import importlib
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


@pytest.fixture
def av():
    return importlib.import_module('app.ai_verification')


@pytest.mark.parametrize('url', ['http://host:bad/v1', 'http://host:99999/v1', 'http://[broken',
                                 'http://user:secret@host/v1', 'http://bad host/v1'])
def test_invalid_model_urls_are_client_errors(av, monkeypatch, url):
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: dict(av.DEFAULT_AI_VERIFICATION_SETTINGS))
    with pytest.raises(HTTPException) as error:
        av.validate_ai_verification_settings({'server_url': url})
    assert error.value.status_code == 400


@pytest.mark.parametrize('content', ['', None, {}, [], 123])
def test_empty_or_invalid_chat_content_is_not_success(av, monkeypatch, content):
    verifier = av.VisionVerifier(av.DEFAULT_AI_VERIFICATION_SETTINGS)
    monkeypatch.setattr(verifier, '_chat_request', lambda _: {'choices': [{'message': {'content': content}}]})
    with pytest.raises(av.VerificationError):
        verifier.chat([])


@pytest.mark.parametrize('reply', ['{"error":"model unavailable"}', '{"description":null}', '{broken', '[]'])
def test_structured_error_is_not_stored_as_caption(av, reply):
    with pytest.raises(av.VerificationError):
        av.parse_description_reply(reply)


def test_verification_robust_to_malformed_confidence_and_string_boolean(av):
    alerts = [{'label': 'person', 'ai_verify': True, 'confidence': 'bad'},
              {'label': 'car', 'ai_verify': 'false', 'confidence': 1}]
    assert av.labels_to_verify(alerts) == ['person']


def test_unexpected_ai_failure_still_forwards_notification(av, monkeypatch):
    forwarded = []
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: (_ for _ in ()).throw(RuntimeError('db failed')))
    monkeypatch.setattr(av, '_forward', lambda *args: forwarded.append(args))
    alerts = [{'label': 'person', 'ai_verify': True}]
    av.verify_and_forward(alerts, 1, [])
    assert forwarded == [(alerts, 1, [])]


def test_connection_test_does_not_claim_missing_snapshot_success(av, monkeypatch):
    monkeypatch.setattr(av._state, 'database', SimpleNamespace(get_event=lambda _: {'id': 1, 'detections': []}))
    monkeypatch.setattr(av.VisionVerifier, 'list_models', lambda _: [])
    monkeypatch.setattr(av, '_read_snapshot', lambda _: None)
    with pytest.raises(av.VerificationError, match='No snapshot'):
        av.run_connection_test(av.DEFAULT_AI_VERIFICATION_SETTINGS, event_id=1)


def test_invalid_box_never_breaks_focus(av):
    detections = [{'label': 'person', 'x': float('nan'), 'y': 0, 'width': .1, 'height': .1}]
    assert av._label_box(detections, 'person') is None
    assert av.focus_image(b'jpeg', detections, 'person') == b'jpeg'


def test_zone_booleans_and_cooldown_are_normalized():
    schema = importlib.import_module('app.zone_schema')
    rule = schema.normalize_zone_ai_tags({'ai_tags': {'enabled': 'false', 'email_enabled': 'false',
                                                    'push_enabled': 'false', 'cooldown_seconds': 999999}})
    assert not rule['enabled'] and not rule['email_enabled'] and not rule['push_enabled']
    assert rule['cooldown_seconds'] == 86400


def test_polygon_bounding_box_is_not_full_frame():
    tags = importlib.import_module('app.ai_tag_alerts')
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1,
            'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 0, 'y': 1}]}
    assert not tags.event_in_zone({'detections': []}, zone)
    assert not tags.event_in_zone({'detections': [{'x': .8, 'y': .8, 'width': .1, 'height': .1}]}, zone)


def test_geometry_matches_overlapping_and_renamed_zones():
    tags = importlib.import_module('app.ai_tag_alerts')
    event = {'detections': [{'zone_name': 'Old Name', 'x': .6, 'y': .6, 'width': .1, 'height': .1}]}
    zone = {'id': 'new', 'name': 'New Name', 'x': .5, 'y': .5, 'width': .4, 'height': .4}
    assert tags.event_in_zone(event, zone)


def test_zone_confirmation_masks_pixels_outside_polygon():
    tags = importlib.import_module('app.ai_tag_alerts')
    import cv2
    import numpy as np
    frame = np.full((200, 200, 3), 255, np.uint8)
    _, encoded = cv2.imencode('.jpg', frame)
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1,
            'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 0, 'y': 1}]}
    result = cv2.imdecode(np.frombuffer(tags._zone_image(encoded.tobytes(), zone), np.uint8), cv2.IMREAD_COLOR)
    assert result[180, 180].mean() < 10
    assert result[20, 20].mean() > 240


def test_same_term_is_checked_separately_per_zone(av, monkeypatch):
    tags = importlib.import_module('app.ai_tag_alerts')
    monkeypatch.setattr(av, '_read_snapshot', lambda _: b'original')
    monkeypatch.setattr(tags, '_zone_image', lambda _, zone: zone['image'])
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: dict(av.DEFAULT_AI_VERIFICATION_SETTINGS))
    seen = []
    monkeypatch.setattr(av.VisionVerifier, 'ask', lambda _, image, *a, **kw: seen.append(image) or (image == b'left', ''))
    cache = {}
    assert tags._confirm_terms({}, ['ladder'], '', cache, {'image': b'left'}) == ['ladder']
    assert tags._confirm_terms({}, ['ladder'], '', cache, {'image': b'right'}) == []
    assert seen == [b'left', b'right']


def test_detected_label_still_matches_tags_only_rule(av, monkeypatch):
    tags = importlib.import_module('app.ai_tag_alerts')
    event = {'detections': [{'label': 'cat'}], 'metadata': {}}
    stored, evaluated = [], []
    db = SimpleNamespace(get_event=lambda _: event,
                         set_event_description=lambda _, record: stored.append(record) or True,
                         add_ai_recording_tags=lambda *_: None)
    monkeypatch.setattr(av._state, 'database', db)
    monkeypatch.setattr(av, '_read_snapshot', lambda _: b'jpeg')
    monkeypatch.setattr(av.VisionVerifier, 'describe', lambda *a, **kw: ('An animal rests.', ['cat']))
    monkeypatch.setattr(tags, 'evaluate_event', lambda _, e, r: evaluated.append(r))
    av._describe_and_store(event, 1, av.DEFAULT_AI_VERIFICATION_SETTINGS, '')
    assert stored[0]['tags'] == [] and '_model_tags' not in stored[0]
    assert evaluated[0]['tags'] == ['cat']
    assert tags.matched_terms({'tags': ['cat'], 'match': 'tags'}, evaluated[0]['tags'], '') == ['cat']


def test_stale_description_job_reuses_persisted_caption(av, monkeypatch):
    db = SimpleNamespace(get_event=lambda _: {'metadata': {'ai_description': {'text': 'Already done.'}}})
    monkeypatch.setattr(av._state, 'database', db)
    monkeypatch.setattr(av, 'describe_event', lambda *a, **kw: pytest.fail('duplicate model work'))
    assert av._describe_and_store({}, 1, {}, '') == 'Already done.'


def test_deleted_event_never_fires_ai_tag_rules(av, monkeypatch):
    tags = importlib.import_module('app.ai_tag_alerts')
    monkeypatch.setattr(av._state, 'database', SimpleNamespace(set_event_description=lambda *_: False))
    monkeypatch.setattr(av, 'describe_event', lambda *a, **kw: {'text': 'A ladder.', 'tags': ['ladder']})
    monkeypatch.setattr(tags, 'evaluate_event', lambda *a: pytest.fail('alert on deleted event'))
    assert av._describe_and_store({}, 1, {}, '') is None


def test_backfill_filters_camera_and_alert_scope_before_limit(av, monkeypatch):
    events = [
        {'id': 3, 'metadata': {'camera_id': 'other'}},
        {'id': 2, 'metadata': {'camera_id': 'cam'}, 'alert_triggered': False},
        {'id': 1, 'metadata': {'camera_id': 'cam'}, 'alert_triggered': True},
    ]
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: {**av.DEFAULT_AI_VERIFICATION_SETTINGS,
                                                                       'describe_events': 'alerts', 'camera_ids': ['cam']})
    monkeypatch.setattr(av, '_undescribed_events', lambda _: iter(events))
    monkeypatch.setattr(av, 'camera_describe_mode', lambda cid, _: 'alerts' if cid == 'cam' else 'off')
    monkeypatch.setattr(av, '_backfill_state', {'running': False})
    captured = []

    class Thread:
        def __init__(self, target, args, **kw):
            captured.extend(args[0])
        def start(self):
            pass

    monkeypatch.setattr(av.threading, 'Thread', Thread)
    status = av.start_description_backfill(24, limit=1)
    assert status['total'] == 1
    assert [event['id'] for event in captured] == [1]


def test_catch_up_naive_timestamp_and_changed_scope(av, monkeypatch):
    event = {'id': 1, 'created_at': datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), 'metadata': {}}
    monkeypatch.setattr(av, '_undescribed_events', lambda _: iter([event]))
    monkeypatch.setattr(av, '_catch_up_attempted', set())
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: {})
    mode = ['off']
    monkeypatch.setattr(av, 'camera_describe_mode', lambda *a: mode[0])
    calls = []
    monkeypatch.setattr(av, '_describe_and_store', lambda *a, **kw: calls.append(kw))
    assert not av.catch_up_once()
    assert av._catch_up_attempted == set()
    mode[0] = 'all'
    assert av.catch_up_once()
    assert calls[0]['evaluate_rules'] is True


def test_catch_up_thread_failure_resets_running_flag(av, monkeypatch):
    monkeypatch.setattr(av, '_catch_up_running', False)
    class Thread:
        def __init__(self, **kw):
            pass
        def start(self):
            raise RuntimeError('cannot create thread')
    monkeypatch.setattr(av.threading, 'Thread', Thread)
    assert av.start_description_catch_up() is False
    assert av._catch_up_running is False


def test_late_recording_links_inherit_ai_tags(tmp_path):
    from app.database import EventDatabase
    db = EventDatabase(str(tmp_path / 'db.sqlite3'))
    event_id = db.add_event('2026-10-02T01:00:00+00:00', 'rtsp', None, [])
    db.set_event_description(event_id, {'text': 'A ladder.', 'tags': ['ladder']})
    def recording(link):
        return db.add_recording(event_id=link, camera_id='cam', started_at='2026-10-02T01:00:00+00:00',
                                ended_at='2026-10-02T01:01:00+00:00', duration_seconds=60,
                                file_path='clip.mp4', thumbnail_path=None, source='rtsp',
                                created_at='2026-10-02T01:01:00+00:00', labels=['person'])
    direct = recording(event_id)
    assert 'ladder' in db.get_recording(direct)['ai_labels']
    late = recording(None)
    assert db.set_event_recording(event_id, late)
    assert 'ladder' in db.get_recording(late)['ai_labels']


def test_partial_ai_settings_save_preserves_runtime_tuning(monkeypatch):
    settings = importlib.import_module('app.ai_settings')
    monkeypatch.setattr(settings, 'effective_ai_config', lambda: {'inference_threads': 8, 'max_concurrent_inferences': 3})
    result = settings.validate_ai_settings({'confidence': .5})
    assert result['inference_threads'] == 8 and result['max_concurrent_inferences'] == 3


def test_plural_description_terms_match():
    tags = importlib.import_module('app.ai_tag_alerts')
    assert tags.matched_terms({'tags': ['battery', 'box'], 'match': 'description'}, [],
                              'Two batteries sit beside boxes.') == ['battery', 'box']


def test_tag_rule_descriptions_keep_whole_scene(av, monkeypatch):
    tags = importlib.import_module('app.ai_tag_alerts')
    monkeypatch.setattr(tags, 'camera_has_rules', lambda _: True)
    monkeypatch.setattr(av, '_read_snapshot', lambda _: b'whole-frame')
    monkeypatch.setattr(av, 'focus_image', lambda *a: pytest.fail('cropping hides other AI tag zones'))
    seen = []
    monkeypatch.setattr(av.VisionVerifier, 'describe', lambda _, image, *a, **kw: seen.append(image) or ('A parcel.', ['parcel']))
    event = {'metadata': {'camera_id': 'cam'}, 'detections': [{'label': 'bird', 'x': .5, 'y': .5, 'width': .02, 'height': .02}]}
    av.describe_event(event, av.DEFAULT_AI_VERIFICATION_SETTINGS)
    assert seen == [b'whole-frame']


def test_ai_queue_exception_fails_open(av, monkeypatch):
    pools = importlib.import_module('app.postprocess_pool')
    dispatch = importlib.import_module('app.alert_dispatch')
    sent = []
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: dict(av.DEFAULT_AI_VERIFICATION_SETTINGS))
    monkeypatch.setattr(pools, 'verification_pool', lambda: (_ for _ in ()).throw(RuntimeError('pool failed')))
    monkeypatch.setattr(dispatch, 'submit_alert_notification', lambda *a: sent.append(a) or True)
    assert av.submit_alert_notification_with_verification([{'label': 'person', 'ai_verify': True}], 1, [])
    assert len(sent) == 1


def test_model_completions_are_serialized_across_threads(av, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    verifier = av.VisionVerifier(av.DEFAULT_AI_VERIFICATION_SETTINGS)
    def request(_):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {'choices': [{'message': {'content': 'OK'}}]}
    monkeypatch.setattr(verifier, '_chat_request', request)
    first = threading.Thread(target=lambda: verifier.chat([]))
    second = threading.Thread(target=lambda: verifier.chat([]))
    first.start()
    assert entered.wait(2)
    second.start()
    try:
        assert not av._model_request_lock.acquire(blocking=False)
        assert len(calls) == 1
    finally:
        release.set()
        first.join(3)
        second.join(3)
    assert len(calls) == 2
