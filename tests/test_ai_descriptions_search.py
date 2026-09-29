"""AI event descriptions and plain-English search.

* Descriptions are written by the local model on the AI pool, stored in the
  event metadata and a full-text index, attached to surviving alerts so email
  and push lead with them, and never block or break delivery.
* Search interprets a question into concept groups + camera + time window
  (model first, keyword fallback) and queries the index, honouring the same
  owner scoping as the Events list.
"""
from __future__ import annotations

import base64
import importlib
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin

from app.database import EventDatabase


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible server that answers each prompt type
# ---------------------------------------------------------------------------

class _SmartModelServer:
    def __init__(self, *, caption='A courier in a hi-vis vest leaves a parcel at the front door.',
                 verdict='{"present": true, "reason": "person visible"}', search=None):
        self.caption, self.verdict = caption, verdict
        self.search = search or {'terms': [['red'], ['car', 'vehicle']], 'camera': 'Driveway',
                                 'start': None, 'end': None}
        self.kinds: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                self._send({'data': [{'id': 'gemma3:4b'}]})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                system = body['messages'][0]['content']
                if 'captions' in system:
                    kind, reply = 'describe', owner.caption
                elif 'search request' in system:
                    kind, reply = 'search', json.dumps(owner.search)
                else:
                    kind, reply = 'verify', owner.verdict
                owner.kinds.append(kind)
                self._send({'choices': [{'message': {'content': reply}}]})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/v1'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def av():
    return importlib.import_module('app.ai_verification')


def _settings(av, **overrides):
    return {**av.DEFAULT_AI_VERIFICATION_SETTINGS, **overrides}


# ---------------------------------------------------------------------------
# Caption cleaning and settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('raw', 'clean'), [
    ('"A man walks a dog."', 'A man walks a dog.'),
    ('**Caption:** A red car parks.\n', 'A red car parks.'),
    ('  Two   people\nchat  ', 'Two people chat'),
])
def test_clean_description(av, raw, clean):
    assert av.clean_description(raw) == clean


def test_clean_description_bounds_length_and_rejects_empty(av):
    long = av.clean_description('word ' * 200)
    assert len(long) <= av.MAX_DESCRIPTION_CHARS + 1 and long.endswith('…')
    with pytest.raises(av.VerificationError):
        av.clean_description('  "" ')


def test_describe_events_setting_is_validated(av, monkeypatch):
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: dict(av.DEFAULT_AI_VERIFICATION_SETTINGS))
    assert av.validate_ai_verification_settings({'describe_events': 'ALL'})['describe_events'] == 'all'
    with pytest.raises(Exception) as exc:
        av.validate_ai_verification_settings({'describe_events': 'sometimes'})
    assert getattr(exc.value, 'status_code', None) == 400


# ---------------------------------------------------------------------------
# Notification formatting
# ---------------------------------------------------------------------------

def test_notifications_lead_with_the_ai_description():
    from app.alert_formatting import build_alert_content

    alert = {'label': 'person', 'confidence': 0.87, 'rule_name': 'Front / Door / person',
             'message': 'Alert triggered: person detected (87.00%)',
             'ai_description': 'A courier in a hi-vis vest leaves a parcel at the front door.'}
    content = build_alert_content(alert, event_id=5, camera_name='Front')
    assert content.alert_message == 'A courier in a hi-vis vest leaves a parcel at the front door.'
    assert content.plain_text.startswith('A courier in a hi-vis vest')
    assert 'Confidence: 87.00%' in content.plain_text
    plain = build_alert_content({**alert, 'ai_description': ''}, event_id=5, camera_name='Front')
    assert plain.alert_message.startswith('Alert Triggered')


# ---------------------------------------------------------------------------
# Description flow on the AI pool
# ---------------------------------------------------------------------------

class _Db:
    def __init__(self, settings, event=None):
        self.settings = settings
        self.event = event or {'id': 1, 'snapshot_path': 'x.jpg', 'detections': [], 'metadata': {}}
        self.descriptions: dict[int, dict] = {}
        self.metadata: dict[int, dict] = {}

    def get_setting(self, key):
        return self.settings if key == 'ai_verification' else None

    def get_event(self, event_id):
        return {**self.event, 'id': event_id}

    def merge_event_metadata(self, event_id, patch):
        self.metadata.setdefault(event_id, {}).update(patch)
        return True

    def set_event_description(self, event_id, record):
        self.descriptions[event_id] = record
        return True

    def add_ai_recording_tags(self, event_id, tags):
        self.recording_tags = getattr(self, 'recording_tags', {})
        self.recording_tags[event_id] = list(tags)
        return 1


@pytest.fixture
def flow(av, monkeypatch):
    dispatch = importlib.import_module('app.alert_dispatch')
    forwarded: list[list[dict]] = []
    monkeypatch.setattr(dispatch, 'submit_alert_notification',
                        lambda _fn, triggered, _event_id, _rules: forwarded.append(list(triggered)) or True)
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')

    def configure(*, verdict=(True, 'ok'), caption='A red car parks in the driveway.', tags=(), **settings):
        db = _Db(_settings(av, **settings))
        monkeypatch.setattr(av._state, 'database', db)
        monkeypatch.setattr(av.VisionVerifier, 'ask', lambda _s, _img, _label, _cam='': verdict)

        def describe(_self, _img, _cam='', **_kw):
            if isinstance(caption, Exception):
                raise caption
            return caption, list(tags)

        monkeypatch.setattr(av.VisionVerifier, 'describe', describe)
        return db

    return configure, forwarded


def test_alert_description_is_stored_and_attached(av, flow):
    configure, forwarded = flow
    db = configure(describe_events='alerts')
    av.verify_and_forward([{'label': 'car', 'confidence': 0.7}], 3, [], 'Driveway')
    assert forwarded == [[{'label': 'car', 'confidence': 0.7, 'ai_description': 'A red car parks in the driveway.'}]]
    assert db.descriptions[3]['text'] == 'A red car parks in the driveway.'
    assert db.descriptions[3]['model'] == 'gemma3:4b'


def test_filtered_alert_is_not_described_in_alerts_mode(av, flow):
    configure, forwarded = flow
    db = configure(enabled=True, describe_events='alerts', verdict=(False, 'shadow'))
    av.verify_and_forward([{'label': 'person', 'confidence': 0.6}], 4, [], '')
    assert forwarded == [] and db.descriptions == {}


def test_all_mode_describes_even_filtered_events(av, flow):
    configure, forwarded = flow
    db = configure(enabled=True, describe_events='all', verdict=(False, 'shadow'))
    av.verify_and_forward([{'label': 'person', 'confidence': 0.6}], 5, [], '')
    assert forwarded == [] and 5 in db.descriptions


def test_description_failure_still_delivers(av, flow):
    configure, forwarded = flow
    db = configure(describe_events='alerts', caption=av.VerificationError('timed out'))
    triggered = [{'label': 'car', 'confidence': 0.7}]
    av.verify_and_forward(triggered, 6, [], '')
    assert forwarded == [triggered] and db.descriptions == {}


def test_describe_only_jobs(av, flow):
    configure, _forwarded = flow
    db = configure(describe_events='all')
    av.describe_only(7, 'Gate')
    assert 7 in db.descriptions
    db.event['metadata'] = {'ai_description': {'text': 'done'}}
    db.descriptions.clear()
    av.describe_only(8, 'Gate')  # already described: no second model call
    assert db.descriptions == {}
    db.settings['describe_events'] = 'alerts'
    db.event['metadata'] = {}
    av.describe_only(9, 'Gate')
    assert db.descriptions == {}


def test_submit_routes_by_mode_and_camera(av, monkeypatch):
    pools = importlib.import_module('app.postprocess_pool')
    dispatch = importlib.import_module('app.alert_dispatch')
    monkeypatch.setattr(importlib.import_module('app.ai_tag_alerts'), 'camera_has_rules', lambda _cid: False)
    queued, direct = [], []

    class _Pool:
        def submit(self, fn, *args, **kwargs):
            queued.append((fn.__name__, kwargs.get('priority')))
            return True

    monkeypatch.setattr(pools, 'verification_pool', lambda: _Pool())
    monkeypatch.setattr(dispatch, 'submit_alert_notification', lambda *_a: direct.append(1) or True)
    settings = _settings(av, describe_events='alerts', camera_ids=['cam-1'])
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: settings)

    av.submit_alert_notification_with_verification([{'label': 'car', 'confidence': 0.5}], 1, [], camera_id='cam-1')
    av.submit_alert_notification_with_verification([{'label': 'car', 'confidence': 0.5}], 2, [], camera_id='cam-2')
    assert queued == [('verify_and_forward', pools.PRIORITY_CLIP)] and direct == [1]

    assert av.submit_event_description(3, camera_id='cam-1') is False  # alerts mode: background off
    settings['describe_events'] = 'all'
    assert av.submit_event_description(3, camera_id='cam-1') is True
    assert queued[-1] == ('describe_only', pools.PRIORITY_BACKGROUND)


def test_client_describe_round_trip(av):
    reply = '```json\n{"description": "Two cats sit on the fence.", "tags": ["fence", "Cat Bowl"]}\n```'
    with _SmartModelServer(caption=reply) as server:
        verifier = av.VisionVerifier(_settings(av, server_url=server.url, timeout_seconds=5))
        assert verifier.describe(b'jpeg', 'Pergola') == ('Two cats sit on the fence.', ['fence', 'cat bowl'])
    assert server.kinds == ['describe']


def test_plain_text_reply_still_describes(av):
    assert av.parse_description_reply('  "Two cats sit on the fence."  ') == ('Two cats sit on the fence.', [])


def test_tags_are_sanitised(av):
    raw = ['Ladder', 'ladder', 'hi-vis vest', 'a very long multi word tag here', 'image', 7, '<script>',
           'x', 'parcel', 'bin', 'rake', 'hose', 'bike', 'pram', 'dog lead']
    assert av.clean_tags(raw) == ['ladder', 'hi-vis vest', 'parcel', 'bin', 'rake', 'hose', 'bike', 'pram']
    assert av.clean_tags(['person', 'ladder'], exclude={'person'}) == ['ladder']
    assert av.clean_tags('ladder') == []


def test_tags_skip_detected_labels_and_reach_recordings(av, flow):
    configure, forwarded = flow
    db = configure(describe_events='alerts', tags=['person', 'ladder', 'hi-vis vest'])
    db.event['detections'] = [{'label': 'person', 'confidence': 0.9}]
    av.verify_and_forward([{'label': 'person', 'confidence': 0.9}], 12, [], '')
    assert db.descriptions[12]['tags'] == ['ladder', 'hi-vis vest']
    assert db.recording_tags[12] == ['ladder', 'hi-vis vest']
    assert forwarded and forwarded[0][0]['ai_description'] == 'A red car parks in the driveway.'


# ---------------------------------------------------------------------------
# Full-text index and search
# ---------------------------------------------------------------------------

def _event(db, camera_id, created_at, text=None, name=None):
    event_id = db.add_event(created_at=created_at, source='rtsp', snapshot_path='snap.jpg', detections=[],
                            metadata={'camera_id': camera_id, 'camera_name': name or camera_id})
    if text is not None:
        db.set_event_description(event_id, {'text': text, 'model': 'm'})
    return event_id


@pytest.fixture
def described_db(tmp_path):
    db = EventDatabase(str(tmp_path / 'search.sqlite3'))
    ids = {
        'red_car': _event(db, 'drive', '2026-09-27T03:00:00+00:00', 'A red car parks in the driveway.'),
        'blue_ute': _event(db, 'drive', '2026-09-27T04:00:00+00:00', 'A blue ute reverses out.'),
        'ladder': _event(db, 'yard', '2026-09-28T01:00:00+00:00', 'A man carrying a ladder walks past the gate.'),
        'plain': _event(db, 'yard', '2026-09-28T02:00:00+00:00'),
    }
    return db, ids


def test_index_matches_groups_with_stemming(described_db):
    db, ids = described_db
    found = [e['id'] for e in db.search_event_descriptions(groups=[['red'], ['car', 'vehicle']])]
    assert found == [ids['red_car']]
    found = [e['id'] for e in db.search_event_descriptions(groups=[['carries'], ['ladders']])]
    assert found == [ids['ladder']]  # porter stemming
    found = {e['id'] for e in db.search_event_descriptions(groups=[['car', 'ute']])}
    assert found == {ids['red_car'], ids['blue_ute']}  # "car" must not match "carrying"
    found = [e['id'] for e in db.search_event_descriptions(groups=[['cars']])]
    assert found == [ids['red_car']]


def test_index_filters_camera_time_and_any_described(described_db):
    db, ids = described_db
    found = [e['id'] for e in db.search_event_descriptions(groups=[['car', 'ute', 'ladder']], camera_ids=['yard'])]
    assert found == [ids['ladder']]
    found = [e['id'] for e in db.search_event_descriptions(
        groups=None, since='2026-09-27T00:00:00Z', until='2026-09-27T23:59:59Z')]
    assert sorted(found) == sorted([ids['red_car'], ids['blue_ute']])
    assert ids['plain'] not in [e['id'] for e in db.search_event_descriptions(groups=None)]


def test_index_is_injection_safe_and_follows_deletes(described_db):
    db, ids = described_db
    # Quotes/operators in a term are neutralised: it is a phrase, not FTS syntax.
    assert db.search_event_descriptions(groups=[['red" OR "blue']]) == []
    assert db.search_event_descriptions(groups=[['NEAR(']]) == []
    db.delete_event(ids['red_car'])
    assert db.search_event_descriptions(groups=[['red']]) == []


def test_fts_expression_quotes_terms():
    from app.db.descriptions import fts_match_expression

    assert fts_match_expression([['red'], ['car', 'hi-vis vest']]) == '("red") AND ("car" OR "hi vis vest")'
    assert fts_match_expression([['"; DROP']]) == '("drop")'


def test_like_fallback_without_fts(described_db):
    db, ids = described_db
    db._description_fts = False
    found = [e['id'] for e in db.search_event_descriptions(groups=[['red'], ['car']])]
    assert found == [ids['red_car']]


def test_backfill_candidates(described_db):
    db, ids = described_db
    assert [e['id'] for e in db.events_without_description()] == [ids['plain']]


# ---------------------------------------------------------------------------
# Query interpretation
# ---------------------------------------------------------------------------

@pytest.fixture
def search_env(monkeypatch, described_db):
    search = importlib.import_module('app.ai_search')
    db, ids = described_db
    monkeypatch.setattr(search._state, 'database', db)
    monkeypatch.setattr(search, '_cameras', lambda: [{'id': 'drive', 'name': 'Driveway'}, {'id': 'yard', 'name': 'Front Yard'}])
    monkeypatch.setattr(search, 'admin_timezone', lambda: timezone.utc)
    return search, ids


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def test_keyword_plan_finds_camera_and_time(search_env):
    search, _ids = search_env
    plan = search.plan_with_keywords('show me anyone carrying a ladder in the front yard yesterday afternoon', NOW)
    assert plan['groups'] == [['carrying'], ['ladder']]
    assert plan['camera']['id'] == 'yard'
    assert plan['since'].startswith('2026-09-27T12:00') and plan['until'].startswith('2026-09-27T18:00')


def test_keyword_search_without_a_model(search_env, monkeypatch):
    search, ids = search_env
    monkeypatch.setattr(search, 'effective_ai_verification_settings', lambda: {'enabled': False, 'describe_events': 'off'})
    result = search.search_events('red car', now_utc=NOW)
    assert [e['id'] for e in result['items']] == [ids['red_car']]
    assert result['interpretation']['interpreted_by'] == 'keywords'


def test_model_plan_with_synonyms_camera_and_times(search_env, monkeypatch):
    search, ids = search_env
    av = importlib.import_module('app.ai_verification')
    with _SmartModelServer(search={'terms': [['red'], ['car', 'vehicle', 'ute']], 'camera': 'driveway',
                                   'start': '2026-09-27 00:00', 'end': '2026-09-27 23:59'}) as server:
        monkeypatch.setattr(search, 'effective_ai_verification_settings',
                            lambda: {**av.DEFAULT_AI_VERIFICATION_SETTINGS, 'enabled': True, 'server_url': server.url})
        result = search.search_events('red vehicle on the driveway yesterday', now_utc=NOW)
    assert [e['id'] for e in result['items']] == [ids['red_car']]
    interp = result['interpretation']
    assert interp['interpreted_by'] == 'model' and interp['camera'] == 'Driveway'
    assert interp['since'].startswith('2026-09-27T00:00')


def test_model_failure_falls_back_and_unmatched_concepts_relax(search_env, monkeypatch):
    search, ids = search_env
    av = importlib.import_module('app.ai_verification')
    monkeypatch.setattr(search, 'effective_ai_verification_settings',
                        lambda: {**av.DEFAULT_AI_VERIFICATION_SETTINGS, 'enabled': True,
                                 'server_url': 'http://127.0.0.1:9/v1', 'timeout_seconds': 3})
    result = search.search_events('blue car', now_utc=NOW)  # nothing mentions both
    assert result['interpretation']['interpreted_by'] == 'keywords'
    assert result['interpretation']['relaxed'] is True
    assert {e['id'] for e in result['items']} == {ids['red_car'], ids['blue_ute']}


def test_model_reply_is_sanitised(search_env):
    search, _ids = search_env
    groups = search._clean_groups([['Red', 'RED', 'the'], 'car', [1, None], ['x' * 60], []])
    assert groups == [['red'], ['car']]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_search_and_backfill_api(tmp_path, monkeypatch):
    app, _db_path = _load_app(tmp_path, monkeypatch)
    import app.state as state

    db = state.database
    described = _event(db, 'drive', '2026-09-28T01:00:00+00:00', 'A red car parks in the driveway.')
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}
        status, _h, result = client.request('/api/event-search?q=red%20car')
        assert status == 200
        assert [item['id'] for item in result['items']] == [described]
        assert result['items'][0]['metadata']['ai_description']['text'].startswith('A red car')

        status, _h, body = client.request('/api/settings/ai-verification/describe-backfill', method='POST',
                                          data=json.dumps({'hours': 24}).encode(), headers=headers)
        assert status == 400 and 'descriptions' in body['detail']  # describe mode is off
        status, _h, _b = client.request('/api/settings/ai-verification/describe-backfill', method='POST',
                                        data=json.dumps({'hours': 0}).encode(), headers=headers)
        assert status == 400
        status, _h, body = client.request('/api/settings/ai-verification/describe-backfill')
        assert status == 200 and body['running'] is False
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Backup restore: the index is allowlisted by exact DDL, nothing else is
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('extra_sql', [
    "CREATE VIRTUAL TABLE smuggled USING fts5(x)",
    "DROP TRIGGER trg_events_delete_description; "
    "CREATE TRIGGER trg_events_delete_description AFTER DELETE ON events BEGIN SELECT 1; END",
])
def test_restore_rejects_unknown_virtual_tables_and_altered_triggers(tmp_path, monkeypatch, extra_sql):
    import sqlite3

    from fastapi import HTTPException

    _load_app(tmp_path, monkeypatch)
    backup = importlib.import_module('app.backup')
    crafted = tmp_path / 'crafted.sqlite3'
    EventDatabase(str(crafted))  # a genuine app database, FTS index included
    conn = sqlite3.connect(crafted)
    conn.executescript(extra_sql)
    conn.commit()
    conn.close()
    with pytest.raises(HTTPException) as exc:
        backup.overwrite_database_from_file(crafted)
    assert exc.value.status_code == 400


def test_restore_accepts_a_genuine_database_with_the_index(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    backup = importlib.import_module('app.backup')
    genuine = tmp_path / 'genuine.sqlite3'
    source = EventDatabase(str(genuine))
    _event(source, 'drive', '2026-09-28T01:00:00+00:00', 'A red car parks.')
    backup.overwrite_database_from_file(genuine)  # no exception



# ---------------------------------------------------------------------------
# AI tags on recordings
# ---------------------------------------------------------------------------

def _recording_for(db, event_id, camera_id='drive'):
    return db.add_recording(
        event_id=event_id, camera_id=camera_id, started_at='2026-09-28T01:00:00+00:00',
        ended_at='2026-09-28T01:00:10+00:00', duration_seconds=10, file_path='clip.mp4',
        thumbnail_path=None, source='camera', created_at='2026-09-28T01:00:00+00:00',
    )


def test_ai_tags_are_searchable_and_marked_on_recordings(tmp_path):
    db = EventDatabase(str(tmp_path / 'tags.sqlite3'))
    event_id = _event(db, 'drive', '2026-09-28T01:00:00+00:00')
    recording_id = _recording_for(db, event_id)
    db.add_recording_labels(recording_id, ['person'])
    db.set_event_description(event_id, {'text': 'A man walks past with tools.', 'tags': ['ladder'], 'model': 'm'})
    assert db.add_ai_recording_tags(event_id, ['ladder', 'person']) == 1

    # The tag is searchable even though the sentence never says "ladder".
    assert [e['id'] for e in db.search_event_descriptions(groups=[['ladder']])] == [event_id]
    recording = db.get_recording(recording_id)
    assert recording['labels'] == ['person']          # the detected label stays a detection
    assert recording['ai_labels'] == ['ladder']
    listed = db.list_recordings(label='ladder')       # the label filter matches AI tags
    assert [r['id'] for r in listed] == [recording_id]
    assert listed[0]['ai_labels'] == ['ladder'] and listed[0]['labels'] == ['person']
    event = db.get_event(event_id)
    assert event['recordings'][0]['ai_labels'] == ['ladder']


def test_detection_promotes_a_label_first_seen_as_an_ai_tag(tmp_path):
    db = EventDatabase(str(tmp_path / 'promote.sqlite3'))
    event_id = _event(db, 'drive', '2026-09-28T01:00:00+00:00')
    recording_id = _recording_for(db, event_id)
    db.add_ai_recording_tags(event_id, ['dog'])
    assert db.get_recording(recording_id)['ai_labels'] == ['dog']
    db.add_recording_labels(recording_id, ['dog'])
    recording = db.get_recording(recording_id)
    assert recording['labels'] == ['dog'] and recording['ai_labels'] == []


def test_description_gets_detector_hint_and_close_up(monkeypatch):
    # A wide frame with a small detected bird: the model must hear what the
    # detector found and see a close-up, so it does not guess "a black cat".
    # One image per request: two images doubled the model's time on a P4 and
    # pushed descriptions past the timeout.
    cv2 = pytest.importorskip('cv2')
    np = pytest.importorskip('numpy')
    av = importlib.import_module('app.ai_verification')
    ok, frame = cv2.imencode('.jpg', np.zeros((1440, 2560, 3), dtype=np.uint8))
    assert ok
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: frame.tobytes())
    sent: list = []
    monkeypatch.setattr(av.VisionVerifier, 'chat',
                        lambda _self, messages, **_kw: sent.append(messages) or '{"description": "A magpie.", "tags": []}')

    def sent_image():
        content = sent[-1][1]['content']
        images = [part for part in content if part['type'] == 'image_url']
        assert len(images) == 1
        data = base64.b64decode(images[0]['image_url']['url'].split(',', 1)[1])
        return content[0]['text'], cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR).shape[:2]

    bird = {'label': 'bird', 'confidence': 0.54, 'x': 0.7, 'y': 0.3, 'width': 0.04, 'height': 0.06}
    motion = {'label': 'motion', 'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}
    settings = dict(av.DEFAULT_AI_VERIFICATION_SETTINGS)
    record = av.describe_event({'detections': [bird, motion]}, settings, camera_name='Pergola')
    assert record['text'] == 'A magpie.'
    text, (height, width) = sent_image()
    assert 'An object detector flagged: bird.' in text and 'close-up' in text and 'rather than guessing' in text
    assert (height, width) == (504, 896), 'a 35% close-up of the 2560x1440 frame'

    # A large object is described from the whole frame, shrunk to 1280 px.
    car = {'label': 'car', 'confidence': 0.9, 'x': 0.2, 'y': 0.3, 'width': 0.5, 'height': 0.4}
    av.describe_event({'detections': [car]}, settings)
    text, shape = sent_image()
    assert 'flagged: car' in text and 'close-up' not in text and shape == (720, 1280)

    # Motion only, or focus crop off: the whole frame.
    av.describe_event({'detections': [motion]}, settings)
    text, shape = sent_image()
    assert 'flagged' not in text and shape == (720, 1280)
    av.describe_event({'detections': [bird]}, {**settings, 'focus_crop': False})
    text, shape = sent_image()
    assert 'flagged: bird' in text and 'close-up' not in text and shape == (720, 1280)


def test_background_descriptions_wait_longer_and_timeouts_cool_down(monkeypatch):
    av = importlib.import_module('app.ai_verification')
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')
    timeouts: list = []

    def chat(self, _messages, **_kw):
        timeouts.append(self.timeout)
        return '{"description": "A car.", "tags": []}'

    monkeypatch.setattr(av.VisionVerifier, 'chat', chat)
    settings = {**av.DEFAULT_AI_VERIFICATION_SETTINGS, 'timeout_seconds': 20}
    av.describe_event({'detections': []}, settings)
    av.describe_event({'detections': []}, settings, background=True)
    assert timeouts == [20.0, av.BACKGROUND_DESCRIBE_TIMEOUT_SECONDS]

    # A timed-out request marks the model busy, so background work holds off.
    import urllib.request
    monkeypatch.setattr(av, '_model_cooldown_until', 0.0)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError('timed out')))
    with pytest.raises(av.VerificationError, match='did not answer within 20s'):
        av.VisionVerifier(settings)._request('/chat/completions', {})
    assert av.model_cooling_down()
    monkeypatch.setattr(av, '_model_cooldown_until', 0.0)
    assert not av.model_cooling_down()


# ---------------------------------------------------------------------------
# Catch-up: events skipped while the AI queue was full
# ---------------------------------------------------------------------------

class _CatchUpDb:
    def __init__(self, events):
        self.events = {event['id']: event for event in events}
        self.described: list[int] = []

    def events_without_description(self, *, since=None, limit=100):
        pending = [e for e in self.events.values() if e['id'] not in self.described and e['created_at'] >= since]
        return sorted(pending, key=lambda e: -e['id'])[:limit]


@pytest.fixture
def catch_up(av, monkeypatch):
    now = datetime.now(timezone.utc)

    def event(event_id, camera='cam', age=10):
        created = (now - timedelta(seconds=age)).isoformat()
        return {'id': event_id, 'created_at': created, 'snapshot_path': 's.jpg', 'detections': [],
                'metadata': {'camera_id': camera, 'camera_name': camera.title()}}

    calls: list[tuple[int, bool]] = []

    def setup(events, *, modes=None):
        db = _CatchUpDb(events)
        monkeypatch.setattr(av._state, 'database', db)
        monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: _settings(av, describe_events='all'))
        monkeypatch.setattr(av, 'camera_describe_mode', lambda cid, _s: (modes or {}).get(cid, 'all'))

        def describe(event, event_id, _settings_, _name, *, evaluate_rules=True, background=False):
            calls.append((event_id, evaluate_rules))
            db.described.append(event_id)
            return 'A car.'

        monkeypatch.setattr(av, '_describe_and_store', describe)
        return db

    monkeypatch.setattr(av, '_catch_up_attempted', set())
    return setup, event, calls


def test_catch_up_describes_skipped_events_newest_first(av, catch_up):
    setup, event, calls = catch_up
    setup([event(1, age=600), event(2, age=30), event(3, camera='porch'), event(4, age=5 * 3600)],
          modes={'porch': 'alerts'})
    while av.catch_up_once():
        pass
    # Newest first; the 'alerts'-only camera and the event outside the window
    # are left alone; only a recent event still runs its tag alert rules.
    assert calls == [(2, True), (1, False)]


def test_catch_up_tries_each_event_once(av, catch_up, monkeypatch):
    setup, event, calls = catch_up
    db = setup([event(7)])
    monkeypatch.setattr(av, '_describe_and_store',
                        lambda *_a, **_k: calls.append('failed'))   # e.g. snapshot missing: stays undescribed
    assert av.catch_up_once() is True
    assert av.catch_up_once() is False                               # not retried forever
    assert calls == ['failed'] and db.described == []


def test_full_queue_starts_catch_up_that_waits_for_idle(av, catch_up, monkeypatch):
    setup, event, calls = catch_up
    setup([event(9)])
    pools = importlib.import_module('app.postprocess_pool')

    class _FullPool:
        def submit(self, *_a, **_k):
            return False

    monkeypatch.setattr(pools, 'verification_pool', lambda: _FullPool())
    busy = iter([True, True, False, False])
    monkeypatch.setattr(av, '_ai_pool_busy', lambda: next(busy, False))
    started: list = []

    class _Thread:
        def __init__(self, target, **_kw):
            self.target = target

        def start(self):
            started.append(self)

    monkeypatch.setattr(av.threading, 'Thread', _Thread)
    monkeypatch.setattr(av, '_catch_up_running', False)
    assert av.submit_event_description(9, camera_id='cam', camera_name='Cam') is False
    assert av.submit_event_description(10, camera_id='cam', camera_name='Cam') is False
    assert len(started) == 1                      # one catch-up thread, however many are skipped
    assert started[0].target is av._run_catch_up
    av._run_catch_up(idle_poll_seconds=0)
    assert calls == [(9, True)] and av._catch_up_running is False


def test_describe_only_defers_to_catch_up_while_the_model_cools_down(av, monkeypatch):
    class _Db:
        def get_event(self, event_id):
            return {'id': event_id, 'snapshot_path': 's.jpg', 'detections': [], 'metadata': {}}

    monkeypatch.setattr(av._state, 'database', _Db())
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: _settings(av, describe_events='all'))
    described: list = []
    catch_ups: list = []
    monkeypatch.setattr(av, '_describe_and_store', lambda *a, **k: described.append(k))
    monkeypatch.setattr(av, 'start_description_catch_up', lambda: catch_ups.append(True))
    monkeypatch.setattr(av, 'model_cooling_down', lambda: True)
    av.describe_only(5, 'Gate')
    assert described == [] and catch_ups == [True]
    monkeypatch.setattr(av, 'model_cooling_down', lambda: False)
    av.describe_only(6, 'Gate')
    assert described == [{'background': True}]
