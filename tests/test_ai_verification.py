"""AI alert verification: a local vision model double-checks object alerts.

The feature must fail open everywhere (disabled, unreachable, unparseable,
queue full, backlog), never verify face/motion alerts, drop only the alerts
the model rejected, and record the verdict on the event.
"""
from __future__ import annotations

import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import HTTPException

from tests.support import LocalClient, _load_app, _login, _server, _setup_admin


@pytest.fixture
def av():
    return importlib.import_module('app.ai_verification')


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible model server
# ---------------------------------------------------------------------------

class _FakeModelServer:
    def __init__(self, reply: str = '{"present": false, "reason": "a shadow on the wall"}'):
        self.reply = reply
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # keep test output quiet
                pass

            def _send(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - http.server API
                owner.headers.append(dict(self.headers))
                self._send({'data': [{'id': 'gemma3:4b'}]})

            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get('Content-Length') or 0)
                owner.requests.append(json.loads(self.rfile.read(length)))
                owner.headers.append(dict(self.headers))
                self._send({'choices': [{'message': {'role': 'assistant', 'content': owner.reply}}]})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/v1'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------------------
# Verdict parsing and prompt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('reply', 'present', 'reason'), [
    ('{"present": true, "reason": "a man at the door"}', True, 'a man at the door'),
    ('```json\n{"present": false, "reason": "tree shadow"}\n```', False, 'tree shadow'),
    ('Sure! {"present": "no", "reason": "reflection"} Hope that helps.', False, 'reflection'),
    ('No. It is a coat on a hook.', False, 'It is a coat on a hook.'),
    ('yes', True, ''),
])
def test_parse_verdict_accepts_common_reply_shapes(av, reply, present, reason):
    assert av.parse_verdict(reply) == (present, reason)


@pytest.mark.parametrize('reply', ['', 'I cannot tell.', '{"present": "maybe"}', '{"answer": true}'])
def test_parse_verdict_rejects_unusable_replies(av, reply):
    with pytest.raises(av.VerificationError):
        av.parse_verdict(reply)


def test_prompt_names_the_label_and_camera(av):
    prompt = av.build_prompt('person', 'Front Door')
    assert '"person"' in prompt and 'Front Door' in prompt and '"present"' in prompt


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------

@pytest.fixture
def no_stored_settings(av, monkeypatch):
    monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: dict(av.DEFAULT_AI_VERIFICATION_SETTINGS))


@pytest.mark.parametrize('payload', [
    {'server_url': 'file:///etc/passwd'},
    {'server_url': 'ftp://host/v1'},
    {'server_url': 'http://'},
    {'server_url': 'http://host/v1?x=1'},
    {'model': 'gemma3 4b; rm'},
    {'model': ''},
    {'timeout_seconds': 1},
    {'timeout_seconds': 500},
    {'timeout_seconds': True},
    {'api_key': 'a\nb'},
    {'camera_ids': {'a': 1}},
])
def test_invalid_settings_are_rejected(av, no_stored_settings, payload):
    with pytest.raises(HTTPException) as exc:
        av.validate_ai_verification_settings(payload)
    assert exc.value.status_code == 400


def test_valid_settings_are_normalised(av, no_stored_settings):
    settings = av.validate_ai_verification_settings({
        'server_url': 'http://192.168.1.5:11434/v1/', 'model': 'qwen2.5vl:3b', 'focus_crop': 'false',
        'timeout_seconds': '30', 'camera_ids': ['cam-1', ' cam-2 '], 'unknown_key': 'dropped',
        # Verification moved to the alert rules: the old global keys are dropped.
        'enabled': True, 'labels': ['person'], 'skip_above_confidence': 0.9,
    })
    assert settings['server_url'] == 'http://192.168.1.5:11434/v1'
    assert settings['timeout_seconds'] == 30
    assert settings['focus_crop'] is False
    assert settings['camera_ids'] == ['cam-1', 'cam-2']
    for key in ('unknown_key', 'enabled', 'labels', 'skip_above_confidence'):
        assert key not in settings


def test_stored_global_verification_keys_are_ignored(av, monkeypatch):
    stored = {'enabled': True, 'labels': ['person'], 'skip_above_confidence': 0.5, 'model': 'moondream'}
    monkeypatch.setattr(av._state, 'database', _FakeDb(stored))
    settings = av._build_effective_settings()
    assert settings['model'] == 'moondream'
    assert not {'enabled', 'labels', 'skip_above_confidence'} & set(settings)


# ---------------------------------------------------------------------------
# Which alerts are verified
# ---------------------------------------------------------------------------

def _settings(av, **overrides):
    return {**av.DEFAULT_AI_VERIFICATION_SETTINGS, **overrides}


def _alert(label, confidence, **extra):
    """An alert fired by a rule with AI Verify ticked."""
    return {'label': label, 'confidence': confidence, 'ai_verify': True, **extra}


def test_face_motion_and_generic_alerts_are_never_verified(av):
    triggered = [
        _alert('face', 0.9),
        _alert('Alice', 0.9, face_rule_id='person_1'),
        _alert('motion', 0.5),
        _alert('person', 0.6),
        _alert('car', 0.8),
    ]
    assert av.labels_to_verify(triggered) == ['car', 'person']


def test_only_alerts_from_ticked_rules_are_verified(av):
    triggered = [{'label': 'person', 'confidence': 0.9}, _alert('car', 0.5), {'label': 'dog', 'ai_verify': False}]
    assert av.labels_to_verify(triggered) == ['car']


def test_per_rule_confidence_skip_and_cap(av):
    triggered = [_alert(name, 0.5) for name in ('person', 'car', 'dog', 'cat', 'bird')]
    assert len(av.labels_to_verify(triggered)) == av.MAX_LABELS_PER_EVENT
    confident = [_alert('person', 0.95, ai_verify_skip_above=0.9), _alert('car', 0.95)]
    assert av.labels_to_verify(confident) == ['car']
    assert av.labels_to_verify([_alert('person', 0.9, ai_verify_skip_above=0.9)]) == []
    assert av.labels_to_verify([_alert('person', 0.89, ai_verify_skip_above=0.9)]) == ['person']


def test_rule_ai_verify_is_normalised():
    from app.zone_schema import normalize_zone_object_rules

    rules = normalize_zone_object_rules({'object_rules': [
        {'label': 'person', 'ai_verify': True, 'ai_verify_skip_above': 1.7},
        {'label': 'car', 'ai_verify_skip_above': 'bad'},
        {'label': 'motion', 'ai_verify': True},
        {'label': 'face', 'ai_verify': True},
    ]})
    by_label = {rule['label']: rule for rule in rules}
    assert by_label['person']['ai_verify'] is True and by_label['person']['ai_verify_skip_above'] == 1.0
    # Existing rules start unticked.
    assert by_label['car']['ai_verify'] is False and by_label['car']['ai_verify_skip_above'] == 1.0
    # Motion and face have no object to check.
    assert by_label['motion']['ai_verify'] is False and by_label['face']['ai_verify'] is False


def test_ticked_rule_marks_the_alerts_it_fires(av):
    from app.alerts import AlertEngine
    from app.zone_detection import zone_object_alert_rules

    def rule(label, **extra):
        return {'label': label, 'min_confidence': 0.3, 'cooldown_seconds': 0,
                'alert_schedules': [{'email_enabled': True}], **extra}

    settings = {'id': 'cam', 'name': 'Cam', 'detection': {'zones': [{
        'id': 'yard', 'name': 'Yard', 'enabled': True, 'monitor_objects': True,
        'object_rules': [rule('person', ai_verify=True, ai_verify_skip_above=0.8), rule('car')],
    }]}}
    rules = zone_object_alert_rules(settings)
    alerts = AlertEngine(rules).process([
        {'label': 'person', 'confidence': 0.6, 'zone_id': 'yard'},
        {'label': 'car', 'confidence': 0.6, 'zone_id': 'yard'},
    ], rules)
    by_label = {alert['label']: alert for alert in alerts}
    assert by_label['person']['ai_verify'] is True and by_label['person']['ai_verify_skip_above'] == 0.8
    assert 'ai_verify' not in by_label['car']
    assert av.labels_to_verify(alerts) == ['person']


# ---------------------------------------------------------------------------
# Verify-and-forward flow
# ---------------------------------------------------------------------------

class _FakeDb:
    def __init__(self, settings):
        self.settings = settings
        self.metadata: dict[int, dict] = {}

    def get_setting(self, key):
        return self.settings if key == 'ai_verification' else None

    def get_event(self, event_id):
        return {'id': event_id, 'snapshot_path': 'x.jpg', 'detections': []}

    def merge_event_metadata(self, event_id, patch):
        self.metadata.setdefault(event_id, {}).update(patch)
        return True


@pytest.fixture
def flow(av, monkeypatch):
    dispatch = importlib.import_module('app.alert_dispatch')
    forwarded: list[list[dict]] = []
    monkeypatch.setattr(dispatch, 'submit_alert_notification',
                        lambda _fn, triggered, _event_id, _rules: forwarded.append(list(triggered)) or True)
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')

    def configure(answers: dict, **settings):
        db = _FakeDb(_settings(av, **settings))
        monkeypatch.setattr(av._state, 'database', db)

        def ask(_self, _image, label, _camera=''):
            answer = answers[label]
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(av.VisionVerifier, 'ask', ask)
        return db

    return configure, forwarded


def test_rejected_label_is_dropped_but_face_alert_still_sent(av, flow):
    configure, forwarded = flow
    db = configure({'person': (False, 'a shadow')})
    triggered = [_alert('person', 0.6, rule_name='P'),
                 {'label': 'Alice', 'face_rule_id': 'person_1', 'confidence': 0.9, 'rule_name': 'F'}]
    av.verify_and_forward(triggered, 7, [], 'Gate')
    assert forwarded == [[triggered[1]]]
    record = db.metadata[7]['ai_verification']
    assert record['status'] == 'filtered' and record['delivered'] is True
    assert record['labels']['person'] == {'present': False, 'reason': 'a shadow'}


def test_all_rejected_sends_nothing(av, flow):
    configure, forwarded = flow
    db = configure({'person': (False, 'bush')})
    av.verify_and_forward([_alert('person', 0.6)], 8, [], '')
    assert forwarded == []
    assert db.metadata[8]['ai_verification']['delivered'] is False


def test_confirmed_alert_is_sent(av, flow):
    configure, forwarded = flow
    db = configure({'person': (True, 'person walking')})
    triggered = [_alert('person', 0.6)]
    av.verify_and_forward(triggered, 9, [], '')
    assert forwarded == [triggered]
    assert db.metadata[9]['ai_verification']['status'] == 'confirmed'


def test_model_error_fails_open(av, flow):
    configure, forwarded = flow
    db = configure({'person': av.VerificationError('model server unreachable')})
    triggered = [_alert('person', 0.6)]
    av.verify_and_forward(triggered, 10, [], '')
    assert forwarded == [triggered]
    assert db.metadata[10]['ai_verification']['status'] == 'error'


def test_unexpected_exception_fails_open(av, flow):
    configure, forwarded = flow
    configure({'person': RuntimeError('boom')})
    triggered = [_alert('person', 0.6)]
    av.verify_and_forward(triggered, 11, [], '')
    assert forwarded == [triggered]


def test_backlogged_job_is_delivered_unverified(av, flow):
    configure, forwarded = flow
    db = configure({'person': (False, 'never asked')})
    triggered = [_alert('person', 0.6)]
    av.verify_and_forward(triggered, 12, [], '', submitted_at=-1e9)
    assert forwarded == [triggered]
    assert db.metadata[12]['ai_verification']['status'] == 'skipped'


def test_unticked_rule_queued_for_description_is_not_verified(av, flow, monkeypatch):
    # An AI tag alert rule makes its camera describe every event, which queues
    # its alerts on the AI pool; alerts whose rule is unticked skip verification.
    configure, forwarded = flow
    db = configure({'person': (False, 'a shadow')})
    monkeypatch.setattr(importlib.import_module('app.ai_tag_alerts'), 'camera_has_rules', lambda cid: cid == 'cam-B')
    monkeypatch.setattr(av, '_describe_and_store', lambda *_args, **_kwargs: None)
    triggered = [{'label': 'person', 'confidence': 0.6}]
    av.verify_and_forward(triggered, 13, [], 'Back', camera_id='cam-B')
    assert forwarded == [triggered]
    assert 13 not in db.metadata


def test_missing_snapshot_fails_open(av, flow, monkeypatch):
    configure, forwarded = flow
    configure({'person': (False, 'never asked')})
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: None)
    triggered = [_alert('person', 0.6)]
    av.verify_and_forward(triggered, 13, [], '')
    assert forwarded == [triggered]


# ---------------------------------------------------------------------------
# Submission from the detection loop
# ---------------------------------------------------------------------------

@pytest.fixture
def submit_env(av, monkeypatch):
    dispatch = importlib.import_module('app.alert_dispatch')
    pools = importlib.import_module('app.postprocess_pool')
    # Routing here is about verification; no camera has an AI tag rule (a
    # tag-rule camera is always described - see test_ai_tag_alerts).
    monkeypatch.setattr(importlib.import_module('app.ai_tag_alerts'), 'camera_has_rules', lambda _cid: False)
    direct: list[int] = []
    queued: list[int] = []
    monkeypatch.setattr(dispatch, 'submit_alert_notification',
                        lambda _fn, _triggered, event_id, _rules: direct.append(event_id) or True)

    class _Pool:
        accept = True

        def submit(self, _fn, _triggered, event_id, *_args, **_kwargs):
            if self.accept:
                queued.append(event_id)
            return self.accept

    pool = _Pool()
    monkeypatch.setattr(pools, 'verification_pool', lambda: pool)

    def use(settings):
        monkeypatch.setattr(av, 'effective_ai_verification_settings', lambda: settings)

    return use, pool, direct, queued


def test_unticked_rule_delivers_directly(av, submit_env):
    use, _pool, direct, queued = submit_env
    use({**av.DEFAULT_AI_VERIFICATION_SETTINGS})
    av.submit_alert_notification_with_verification([{'label': 'person', 'confidence': 0.5}], 1, [], camera_id='cam')
    assert direct == [1] and queued == []


def test_ticked_rule_queues_and_full_queue_fails_open(av, submit_env):
    use, pool, direct, queued = submit_env
    use(_settings(av))
    av.submit_alert_notification_with_verification([_alert('person', 0.5)], 2, [], camera_id='cam')
    assert queued == [2] and direct == []
    pool.accept = False
    av.submit_alert_notification_with_verification([_alert('person', 0.5)], 3, [], camera_id='cam')
    assert direct == [3]


def test_face_only_alert_skips_the_verification_queue(av, submit_env):
    use, _pool, direct, queued = submit_env
    use(_settings(av))
    av.submit_alert_notification_with_verification([_alert('face', 0.9)], 4, [], camera_id='cam')
    assert direct == [4] and queued == []


# ---------------------------------------------------------------------------
# HTTP client against a fake OpenAI-compatible server
# ---------------------------------------------------------------------------

def test_client_sends_image_and_parses_reply(av):
    with _FakeModelServer() as server:
        verifier = av.VisionVerifier({**_settings(av), 'server_url': server.url, 'api_key': 'sekret', 'timeout_seconds': 5})
        assert verifier.ask(b'\xff\xd8jpeg', 'person', 'Gate') == (False, 'a shadow on the wall')
        assert verifier.list_models() == ['gemma3:4b']
    body = server.requests[0]
    assert body['model'] == 'gemma3:4b' and body['temperature'] == 0
    image_part = body['messages'][1]['content'][1]
    assert image_part['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert server.headers[0]['Authorization'] == 'Bearer sekret'


class _ReasoningServer(_FakeModelServer):
    """Replies like a server that either rejects ``reasoning_effort`` or
    returns an answer spent entirely on thinking."""

    def __init__(self, *, rejects_reasoning_effort=False, thinks=False):
        super().__init__()
        owner = self
        base = self.httpd.RequestHandlerClass

        class Handler(base):
            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length))
                owner.requests.append(body)
                if rejects_reasoning_effort and 'reasoning_effort' in body:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Unrecognized request argument: reasoning_effort"}')
                    return
                message = {'role': 'assistant', 'content': owner.reply}
                if thinks:
                    message = {'role': 'assistant', 'content': '', 'reasoning': 'Let me look at the image...'}
                self._send({'choices': [{'message': message}]})

        self.httpd.RequestHandlerClass = Handler


def test_client_asks_the_model_not_to_think(av):
    with _FakeModelServer() as server:
        verifier = av.VisionVerifier({**_settings(av), 'server_url': server.url, 'timeout_seconds': 5})
        verifier.ask(b'jpeg', 'person')
    assert server.requests[0]['reasoning_effort'] == 'none'


def test_server_rejecting_reasoning_effort_is_asked_without_it(av):
    with _ReasoningServer(rejects_reasoning_effort=True) as server:
        verifier = av.VisionVerifier({**_settings(av), 'server_url': server.url, 'timeout_seconds': 5})
        assert verifier.ask(b'jpeg', 'person') == (False, 'a shadow on the wall')
        assert verifier.ask(b'jpeg', 'person') == (False, 'a shadow on the wall')
    # First call retried once without the field; the second skips it outright.
    assert ['reasoning_effort' in body for body in server.requests] == [True, False, False]
    av._servers_without_reasoning_effort.discard(server.url)


def test_reply_spent_on_thinking_gives_a_clear_error(av):
    with _ReasoningServer(thinks=True) as server:
        verifier = av.VisionVerifier({**_settings(av), 'server_url': server.url, 'timeout_seconds': 5})
        with pytest.raises(av.VerificationError, match='thinking'):
            verifier.ask(b'jpeg', 'person')


def test_client_reports_an_unreachable_server(av):
    verifier = av.VisionVerifier({**_settings(av), 'server_url': 'http://127.0.0.1:9/v1', 'timeout_seconds': 3})
    with pytest.raises(av.VerificationError, match='unreachable'):
        verifier.ask(b'jpeg', 'person')


# ---------------------------------------------------------------------------
# Focus crop
# ---------------------------------------------------------------------------

def test_focus_crop_zooms_in_on_a_small_object(av):
    cv2 = pytest.importorskip('cv2')
    np = pytest.importorskip('numpy')
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode('.jpg', frame)
    assert ok
    detections = [{'label': 'person', 'x': 0.8, 'y': 0.1, 'width': 0.05, 'height': 0.1}]
    cropped = cv2.imdecode(np.frombuffer(av.focus_image(encoded.tobytes(), detections, 'person'), np.uint8), cv2.IMREAD_COLOR)
    assert cropped.shape[1] < 640 and cropped.shape[0] < 480
    # A large object, or no box for the label, keeps the whole frame.
    big = [{'label': 'person', 'x': 0.1, 'y': 0.1, 'width': 0.6, 'height': 0.8}]
    assert av.focus_image(encoded.tobytes(), big, 'person') == encoded.tobytes()
    assert av.focus_image(encoded.tobytes(), detections, 'car') == encoded.tobytes()


# ---------------------------------------------------------------------------
# Database helper and API
# ---------------------------------------------------------------------------

def test_merge_event_metadata_keeps_existing_keys(tmp_path):
    from app.database import EventDatabase

    db = EventDatabase(str(tmp_path / 'm.sqlite3'))
    event_id = db.add_event(created_at='2026-09-28T00:00:00+00:00', source='rtsp', snapshot_path=None,
                            detections=[], metadata={'camera_id': 'cam'})
    assert db.merge_event_metadata(event_id, {'ai_verification': {'status': 'filtered'}}) is True
    metadata = db.get_event(event_id)['metadata']
    assert metadata == {'camera_id': 'cam', 'ai_verification': {'status': 'filtered'}}
    assert db.merge_event_metadata(event_id + 999, {'x': 1}) is False


def test_settings_api_round_trip_and_test_endpoint(tmp_path, monkeypatch):
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}
        status, _h, settings = client.request('/api/settings/ai-verification')
        assert status == 200 and 'enabled' not in settings

        status, _h, _b = client.request('/api/settings/ai-verification', method='PUT',
                                        data=json.dumps({'server_url': 'file:///x'}).encode(), headers=headers)
        assert status == 400

        with _FakeModelServer('{"present": true, "reason": "ok"}') as model:
            payload = {'server_url': model.url, 'model': 'gemma3:4b'}
            status, _h, saved = client.request('/api/settings/ai-verification', method='PUT',
                                               data=json.dumps(payload).encode(), headers=headers)
            assert status == 200 and saved['server_url'] == model.url

            # No object event yet: the test proves the model answers.
            status, _h, result = client.request('/api/settings/ai-verification/test', method='POST',
                                                data=json.dumps({'settings': payload}).encode(), headers=headers)
            assert status == 200, result
            assert result['ok'] is True and result['model_listed'] is True

        status, _h, result = client.request(
            '/api/settings/ai-verification/test', method='POST',
            data=json.dumps({'settings': {**payload, 'server_url': 'http://127.0.0.1:9/v1', 'timeout_seconds': 3}}).encode(),
            headers=headers,
        )
        assert status == 400 and 'failed' in result['detail']
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_ai_page_is_admin_only_and_served(tmp_path, monkeypatch):
    """Intelligence > AI (/ai) hosts the AI settings; it used to redirect to /onnx."""
    app, _db = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _h, body = client.request('/ai')
        assert status == 200
        assert 'id="aiSettingsForm"' in body and '/static/ai.js' in body
        settings_status, _h, settings_body = client.request('/settings')
        assert settings_status == 200
        # The AI form lives on /ai only. (Settings used to carry a "moved to
        # Intelligence > AI" note; it was retired once the sidebar link was
        # the established way there.)
        assert 'aiVerificationForm' not in settings_body
    finally:
        server.should_exit = True
        thread.join(timeout=5)
