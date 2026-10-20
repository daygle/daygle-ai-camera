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

from tests.support import add_event, _load_app, LocalClient, _login, _server, _setup_admin


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


@pytest.fixture
def default_groups(monkeypatch):
    import app.alerts as alerts
    import app.zone_schema as zone_schema
    from app.label_groups import DEFAULT_LABEL_GROUPS

    for module in (alerts, zone_schema):
        monkeypatch.setattr(module, 'cached_label_groups', lambda: dict(DEFAULT_LABEL_GROUPS))


def test_group_rule_verifies_the_group_not_the_misread_label(av, default_groups):
    # An IR-lit cat read as "bird" (recording 26881) is a real animal but not
    # a real bird: an ``animal`` rule must ask about animals.
    from app.alerts import AlertEngine
    from app.zone_detection import zone_object_alert_rules

    def rule(label):
        return {'label': label, 'min_confidence': 0.3, 'cooldown_seconds': 0, 'ai_verify': True,
                'alert_schedules': [{'email_enabled': True}]}

    settings = {'id': 'cam', 'name': 'Cam', 'detection': {'zones': [{
        'id': 'yard', 'name': 'Yard', 'enabled': True, 'monitor_objects': True,
        'object_rules': [rule('animal'), rule('bird')],
    }]}}
    rules = zone_object_alert_rules(settings)
    alerts = AlertEngine(rules).process([{'label': 'bird', 'confidence': 0.8, 'zone_id': 'yard'}], rules)
    by_rule = {alert['rule_name'].rsplit('/', 1)[-1].strip(): alert for alert in alerts}
    assert by_rule['animal']['label'] == 'bird' and by_rule['animal']['ai_verify_label'] == 'animal'
    # The notification names the rule that fired, not the detector's guess.
    from app.alert_formatting import build_alert_content
    assert build_alert_content(by_rule['animal'], event_id=1).rule_display == 'Animal'
    assert build_alert_content(by_rule['bird'], event_id=1).rule_display == 'Bird'
    assert 'rule_label' not in by_rule['bird']
    # A rule for the label itself still asks about that label.
    assert 'ai_verify_label' not in by_rule['bird']
    assert av.labels_to_verify(alerts) == ['animal', 'bird']


def test_group_prompt_and_close_up_use_the_group(av, default_groups):
    assert 'an "animal"' in av.build_prompt('animal') and 'like an animal' in av.build_prompt('animal')
    assert 'a "person"' in av.build_prompt('person')
    detections = [{'label': 'dog', 'x': 0.6, 'y': 0.7, 'width': 0.04, 'height': 0.12},
                  {'label': 'car', 'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.3}]
    assert av._label_box(detections, 'animal') == pytest.approx((0.6, 0.7, 0.64, 0.82))


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


def test_rejected_alert_lets_the_next_sighting_alert(av, flow):
    """Event #67665: a tiny 0.43 person box was rejected ("no person visible");
    the clear person seconds later must open its own, verified event."""
    from app.event_presence import observe_presence
    configure, forwarded = flow
    configure({'person': (False, 'No person visible; image shows car and ground.')})
    av._state.live_event_presence.clear()
    try:
        assert observe_presence('camera-1', {'person': 1}, absence=45.0, now=0.0) == {'person'}
        av.verify_and_forward([_alert('person', 0.431, rule_name='P')], 11, [], 'Driveway', camera_id='camera-1')
        assert forwarded == []
        assert observe_presence('camera-1', {'person': 1}, absence=45.0, now=10.0) == {'person'}
    finally:
        av._state.live_event_presence.clear()


def test_group_rule_rejection_reopens_the_detected_label(av, flow):
    configure, _forwarded = flow
    configure({'animal': (False, 'a bag')})
    av._state.live_event_presence.clear()
    try:
        from app.event_presence import observe_presence
        observe_presence('camera-1', {'cat': 1}, absence=45.0, now=0.0)
        av.verify_and_forward([_alert('cat', 0.5, ai_verify_label='animal')], 12, [], '', camera_id='camera-1')
        assert observe_presence('camera-1', {'cat': 1}, absence=45.0, now=5.0) == {'cat'}
    finally:
        av._state.live_event_presence.clear()


def test_confirmed_alert_keeps_the_object_covered(av, flow):
    from app.event_presence import observe_presence
    configure, _forwarded = flow
    configure({'person': (True, 'person walking')})
    av._state.live_event_presence.clear()
    try:
        observe_presence('camera-1', {'person': 1}, absence=45.0, now=0.0)
        av.verify_and_forward([_alert('person', 0.8)], 13, [], '', camera_id='camera-1')
        assert observe_presence('camera-1', {'person': 1}, absence=45.0, now=5.0) == set()
    finally:
        av._state.live_event_presence.clear()


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


def test_other_event_descriptions_alone_do_not_hold_alerts_in_the_ai_queue(av, submit_env):
    use, _pool, direct, queued = submit_env
    use(_settings(av, describe_alerts='off', describe_other_events='tags'))
    av.submit_alert_notification_with_verification([{'label': 'person', 'confidence': 0.5}], 5, [], camera_id='cam')
    assert direct == [5] and queued == []
    use(_settings(av, describe_alerts='tags', describe_other_events='off'))
    av.submit_alert_notification_with_verification([{'label': 'person', 'confidence': 0.5}], 6, [], camera_id='cam')
    assert queued == [6]


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
    event_id = add_event(db, created_at='2026-09-28T00:00:00+00:00', source='rtsp', snapshot_path=None,
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


# ---------------------------------------------------------------------------
# Combined check: every label (and the description) in one request
# ---------------------------------------------------------------------------

def test_check_prompt_names_every_label_and_the_description_fields(av):
    prompt = av.build_check_prompt(['person', 'car'], 'Gate', content='both', detected=['person', 'car'])
    assert '"person"' in prompt and '"car"' in prompt and 'Gate' in prompt
    assert '"checks"' in prompt and '"description"' in prompt and '"tags"' in prompt
    tags_only = av.build_check_prompt(['person'], content='tags')
    assert '"tags"' in tags_only and '"description"' not in tags_only
    verify_only = av.build_check_prompt(['person', 'car'])
    assert '"description"' not in verify_only and '"tags"' not in verify_only


def test_parse_check_reply_reads_verdicts_and_description(av):
    reply = ('```json\n{"checks": {"Person": {"present": "yes", "reason": "man at door"}, '
             '"traffic light": {"present": false, "reason": "a lamp"}}, '
             '"description": "A man waits at the door.", "tags": ["Parcel", "image"]}\n```')
    verdicts, description = av.parse_check_reply(reply, ['person', 'traffic_light'], 'both')
    assert verdicts == {'person': (True, 'man at door'), 'traffic_light': (False, 'a lamp')}
    assert description == ('A man waits at the door.', ['parcel'])


@pytest.mark.parametrize('reply, labels, content, verdicts, description', [
    ('not json at all', ['person'], 'both', {}, None),
    ('{"checks": {"person": {"present": "maybe"}}, "description": ""}', ['person'], 'both', {}, None),
    ('{"checks": {"person": {"present": true}}}', ['person', 'car'], 'description', {'person': (True, '')}, None),
    ('{"checks": {"car": {"present": false}}, "tags": ["bin"]}', ['car'], 'tags', {'car': (False, '')}, ('', ['bin'])),
    ('{"checks": {"car": {"present": true}}, "description": "A car.", "tags": ["bin"]}', ['car'], 'description',
     {'car': (True, '')}, ('A car.', [])),
])
def test_parse_check_reply_leaves_out_what_it_cannot_read(av, reply, labels, content, verdicts, description):
    assert av.parse_check_reply(reply, labels, content) == (verdicts, description)


def test_describe_prompt_and_reply_follow_the_content_choice(av):
    assert '"description"' not in av.build_describe_prompt(content='tags')
    assert '"tags"' not in av.build_describe_prompt(content='description')
    assert av.parse_description_reply('{"tags": ["Ladder"]}', 'tags') == ('', ['ladder'])
    assert av.parse_description_reply('{"description": "A man.", "tags": ["hat"]}', 'description') == ('A man.', [])
    with pytest.raises(av.VerificationError):
        av.parse_description_reply('A man walks by.', 'tags')


def test_alert_and_other_event_content_are_validated(av, no_stored_settings):
    settings = av.validate_ai_verification_settings({'describe_alerts': ' Both ', 'describe_other_events': 'tags'})
    assert (settings['describe_alerts'], settings['describe_other_events']) == ('both', 'tags')
    assert 'describe_events' not in settings
    with pytest.raises(HTTPException):
        av.validate_ai_verification_settings({'describe_alerts': 'everything'})


@pytest.mark.parametrize('alerts, others, mode', [
    ('off', 'off', 'off'), ('tags', 'off', 'alerts'), ('off', 'tags', 'all'), ('both', 'description', 'all'),
])
def test_describe_mode_follows_the_two_settings(av, alerts, others, mode):
    assert av.describe_mode(_settings(av, describe_alerts=alerts, describe_other_events=others)) == mode


@pytest.mark.parametrize('content, modes, expected', [
    ('tags', set(), 'tags'),
    ('tags', {'tags', 'both'}, 'tags'),
    ('tags', {'description'}, 'both'),
    ('description', {'both'}, 'description'),
    ('description', {'tags'}, 'both'),
    ('both', {'tags'}, 'both'),
    ('off', {'tags'}, 'both'),
])
def test_tag_rules_widen_the_content_to_what_they_match_on(av, monkeypatch, content, modes, expected):
    tag_alerts = importlib.import_module('app.ai_tag_alerts')
    monkeypatch.setattr(tag_alerts, 'camera_has_rules', lambda _cid: True)
    monkeypatch.setattr(tag_alerts, 'camera_rule_match_modes', lambda _cid, _event=None: modes)
    settings = _settings(av, describe_alerts=content, describe_other_events=content)
    assert av.camera_describe_content('cam', settings, alerted=True) == expected
    assert av.camera_describe_content('cam', settings, alerted=False) == expected


def test_content_is_chosen_per_event_kind_and_camera_scope(av, monkeypatch):
    monkeypatch.setattr(importlib.import_module('app.ai_tag_alerts'), 'camera_has_rules', lambda _cid: False)
    settings = _settings(av, describe_alerts='both', describe_other_events='tags', camera_ids=['cam'])
    assert av.camera_describe_content('cam', settings, alerted=True) == 'both'
    assert av.camera_describe_content('cam', settings, alerted=False) == 'tags'
    assert av.camera_describe_content('elsewhere', settings, alerted=True) == 'off'
    alerted = {'alert_triggered': True, 'metadata': {'camera_id': 'cam'}}
    rejected = {**alerted, 'metadata': {'camera_id': 'cam', 'ai_verification': {'delivered': False}}}
    assert av.event_describe_content(alerted, settings) == 'both'
    assert av.event_describe_content(rejected, settings) == 'tags'
    assert av.event_describe_content({'metadata': {'camera_id': 'cam'}}, settings) == 'tags'


@pytest.fixture
def combined(av, monkeypatch):
    """The verify-and-forward flow with the combined check, ask and describe faked."""
    dispatch = importlib.import_module('app.alert_dispatch')
    tag_alerts = importlib.import_module('app.ai_tag_alerts')
    forwarded: list[list[dict]] = []
    calls: list[tuple] = []
    monkeypatch.setattr(dispatch, 'submit_alert_notification',
                        lambda _fn, triggered, _event_id, _rules: forwarded.append(list(triggered)) or True)
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')
    monkeypatch.setattr(tag_alerts, 'camera_has_rules', lambda _cid: False)
    monkeypatch.setattr(tag_alerts, 'camera_rule_match_modes', lambda _cid, _event=None: set())
    catch_ups: list[bool] = []
    monkeypatch.setattr(av, 'start_description_catch_up', lambda: catch_ups.append(True) or True)

    class Db(_FakeDb):
        def __init__(self, settings):
            super().__init__(settings)
            self.descriptions: dict[int, dict] = {}

        def set_event_description(self, event_id, record):
            self.descriptions[event_id] = record
            return True

        def add_ai_recording_tags(self, _event_id, _tags):
            return 0

    def configure(*, check=None, ask=(True, 'ok'), describe=('A person at the gate.', ['bag']), **settings):
        db = Db(_settings(av, **settings))
        monkeypatch.setattr(av._state, 'database', db)

        def fake_check(_self, _image, labels, _camera='', *, content=None, **_kw):
            calls.append(('check', tuple(labels), content))
            if isinstance(check, Exception):
                raise check
            return check

        def fake_ask(_self, _image, label, _camera='', **_kw):
            calls.append(('ask', label))
            return ask

        def fake_describe(_self, _image, _camera='', *, content='both', **_kw):
            calls.append(('describe', content))
            return describe

        monkeypatch.setattr(av.VisionVerifier, 'check', fake_check)
        monkeypatch.setattr(av.VisionVerifier, 'ask', fake_ask)
        monkeypatch.setattr(av.VisionVerifier, 'describe', fake_describe)
        return db

    return configure, forwarded, calls, catch_ups


def test_several_labels_are_verified_in_one_request(av, combined):
    configure, forwarded, calls, _ = combined
    db = configure(check=({'person': (True, 'walking'), 'car': (False, 'a poster')}, None))
    triggered = [_alert('person', 0.6), _alert('car', 0.5)]
    av.verify_and_forward(triggered, 20, [], 'Gate')
    assert calls == [('check', ('person', 'car'), None)]
    assert forwarded == [[triggered[0]]]
    record = db.metadata[20]['ai_verification']
    assert record['status'] == 'filtered' and record['combined'] is True


def test_single_label_without_description_keeps_the_plain_question(av, combined):
    configure, forwarded, calls, _ = combined
    configure()
    av.verify_and_forward([_alert('person', 0.6)], 21, [], '')
    assert calls == [('ask', 'person')]


def test_verification_and_description_share_one_request(av, combined):
    configure, forwarded, calls, catch_ups = combined
    db = configure(check=({'person': (True, 'walking')}, ('A person at the gate.', ['bag', 'person'])),
                   describe_alerts='both')
    db.get_event = lambda event_id: {'id': event_id, 'snapshot_path': 'x.jpg',
                                      'detections': [{'label': 'person'}], 'metadata': {}}
    av.verify_and_forward([_alert('person', 0.6)], 22, [], 'Gate', camera_id='cam')
    assert calls == [('check', ('person',), 'both')]
    assert forwarded[0][0]['ai_description'] == 'A person at the gate.'
    assert db.descriptions[22]['text'] == 'A person at the gate.'
    # A tag repeating a detection is hidden from the stored tags.
    assert db.descriptions[22]['tags'] == ['bag']
    assert catch_ups == []


def test_unanswered_parts_are_asked_separately(av, combined):
    configure, forwarded, calls, _ = combined
    db = configure(check=({'person': (True, 'walking')}, None), ask=(False, 'a bush'), describe_alerts='both', describe_other_events='both')
    triggered = [_alert('person', 0.6), _alert('dog', 0.5)]
    av.verify_and_forward(triggered, 23, [], '', camera_id='cam')
    assert calls == [('check', ('person', 'dog'), 'both'), ('ask', 'dog'), ('describe', 'both')]
    assert forwarded == [[{**triggered[0], 'ai_description': 'A person at the gate.'}]]
    assert db.metadata[23]['ai_verification']['labels']['dog'] == {'present': False, 'reason': 'a bush'}


def test_unreachable_model_is_not_asked_again_per_label(av, combined):
    configure, forwarded, calls, _ = combined
    db = configure(check=av.VerificationError('model did not answer within 20s (busy or slow)'))
    triggered = [_alert('person', 0.6), _alert('car', 0.5)]
    av.verify_and_forward(triggered, 24, [], '')
    assert calls == [('check', ('person', 'car'), None)]
    assert forwarded == [triggered]
    assert db.metadata[24]['ai_verification']['status'] == 'error'


def test_unavailable_model_is_not_asked_to_describe_either(av, combined):
    """A timed-out check must not hold the alert for a second timeout on the
    description: it goes out now and the catch-up describes the event."""
    configure, forwarded, calls, catch_ups = combined
    db = configure(check=av.ModelUnavailableError('model did not answer within 20s (busy or slow)'),
                   describe_alerts='both')
    triggered = [_alert('person', 0.6)]
    av.verify_and_forward(triggered, 27, [], '', camera_id='cam')
    assert calls == [('check', ('person',), 'both')]
    assert forwarded == [triggered] and catch_ups == [True]
    assert '_model_unavailable' not in db.metadata[27]['ai_verification']


def _far_apart(av, monkeypatch, answers):
    """Two small objects at opposite edges: each label is asked on its own."""
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')
    monkeypatch.setattr(av, 'focus_image', lambda image, _detections, _label: image)
    asked: list[str] = []

    class Verifier:
        def ask(self, _image, label, _camera=''):
            asked.append(label)
            answer = answers[label]
            if isinstance(answer, Exception):
                raise answer
            return answer

    event = {'snapshot_path': 'x.jpg', 'detections': [
        {'label': 'person', 'x': 0.0, 'y': 0.4, 'width': 0.05, 'height': 0.1},
        {'label': 'car', 'x': 0.95, 'y': 0.4, 'width': 0.05, 'height': 0.1},
    ]}
    record, _ = av.verify_and_describe_event(event, ['person', 'car'], _settings(av), verifier=Verifier())
    return record, asked


def test_per_label_checks_stop_once_the_model_is_unavailable(av, monkeypatch):
    record, asked = _far_apart(av, monkeypatch, {
        'person': av.ModelUnavailableError('model did not answer within 20s (busy or slow)'),
        'car': (True, 'a car'),
    })
    assert asked == ['person']
    assert record['labels']['car'] == {'present': None, 'error': 'model did not answer within 20s (busy or slow)'}
    assert record['status'] == 'error' and record['_model_unavailable'] is True


def test_per_label_checks_go_on_after_an_unparseable_reply(av, monkeypatch):
    record, asked = _far_apart(av, monkeypatch, {
        'person': av.VerificationError("unrecognised model reply: 'maybe'"),
        'car': (False, 'a shadow'),
    })
    assert asked == ['person', 'car']
    assert record['status'] == 'filtered' and '_model_unavailable' not in record


def test_reply_with_braces_in_trailing_prose_is_still_read(av):
    reply = '{"checks": {"person": {"present": false, "reason": "a coat"}}} Note: {uncertain}'
    verdicts, _ = av.parse_check_reply(reply, ['person'])
    assert verdicts == {'person': (False, 'a coat')}


def test_all_rejected_in_alerts_mode_stores_no_description(av, combined):
    configure, forwarded, calls, _ = combined
    db = configure(check=({'person': (False, 'shadow')}, ('Nothing notable.', [])), describe_alerts='both')
    av.verify_and_forward([_alert('person', 0.6)], 25, [], '', camera_id='cam')
    assert forwarded == [] and db.descriptions == {}


def test_tags_only_description_is_stored_without_text(av, combined):
    configure, forwarded, calls, catch_ups = combined
    db = configure(check=({'person': (True, 'ok')}, ('', ['ladder'])), describe_alerts='tags')
    triggered = [_alert('person', 0.6)]
    av.verify_and_forward(triggered, 26, [], '', camera_id='cam')
    assert calls == [('check', ('person',), 'tags')]
    assert db.descriptions[26]['text'] == '' and db.descriptions[26]['tags'] == ['ladder']
    # No sentence to lead the notification, and nothing left for the catch-up.
    assert forwarded == [triggered] and catch_ups == []


def test_alerts_and_other_events_get_their_own_content(av, combined):
    configure, forwarded, calls, _ = combined
    configure(check=({'person': (True, 'walking')}, ('A person at the gate.', ['bag'])),
              describe_alerts='both', describe_other_events='tags')
    av.verify_and_forward([_alert('person', 0.6)], 27, [], '', camera_id='cam')
    assert calls == [('check', ('person',), 'both')]
    assert forwarded[0][0]['ai_description'] == 'A person at the gate.'


def test_rejected_alert_is_described_as_another_event(av, combined):
    configure, forwarded, calls, _ = combined
    db = configure(check=({'person': (False, 'shadow')}, ('A shadow on the path.', ['bag'])),
                   describe=('', ['bin']), describe_alerts='both', describe_other_events='tags')
    av.verify_and_forward([_alert('person', 0.6)], 28, [], '', camera_id='cam')
    # The combined reply was written for an alert; the event is not one now.
    assert calls == [('check', ('person',), 'both'), ('describe', 'tags')]
    assert forwarded == [] and db.descriptions[28]['tags'] == ['bin']


def test_alert_description_off_still_verifies_in_one_question(av, combined):
    configure, forwarded, calls, _ = combined
    db = configure(describe_alerts='off', describe_other_events='tags')
    av.verify_and_forward([_alert('person', 0.6)], 29, [], '', camera_id='cam')
    assert calls == [('ask', 'person')]
    assert forwarded == [[_alert('person', 0.6)]] and db.descriptions == {}


def test_small_objects_far_apart_get_their_own_close_ups(av):
    left = {'label': 'person', 'x': 0.02, 'y': 0.4, 'width': 0.05, 'height': 0.1}
    right = {'label': 'dog', 'x': 0.9, 'y': 0.5, 'width': 0.05, 'height': 0.05}
    settings = _settings(av)
    assert av._check_image(b'jpeg', [left, right], ['person', 'dog'], settings,
                           describing=False, camera_id=None) is None
    big = {'label': 'car', 'x': 0.1, 'y': 0.1, 'width': 0.7, 'height': 0.7}
    assert av._check_image(b'jpeg', [big], ['car'], settings, describing=True, camera_id=None) == (b'jpeg', False)
    assert av._check_image(b'jpeg', [left, right], ['person', 'dog'], _settings(av, focus_crop=False),
                           describing=False, camera_id=None) == (b'jpeg', False)


def test_client_check_sends_one_request(av):
    reply = '{"checks": {"person": {"present": true, "reason": "walking"}}, "tags": ["bag"]}'
    with _FakeModelServer(reply) as server:
        verifier = av.VisionVerifier({**_settings(av), 'server_url': server.url, 'timeout_seconds': 5})
        assert verifier.check(b'jpeg', ['person'], 'Gate', content='tags') == (
            {'person': (True, 'walking')}, ('', ['bag']))
    assert len(server.requests) == 1
    assert '"checks"' in server.requests[0]['messages'][1]['content'][0]['text']


def test_group_alert_is_kept_when_the_group_is_confirmed(av, flow, default_groups, monkeypatch):
    configure, forwarded = flow
    answers = {'animal': (True, 'a cat beside the van'), 'bird': (False, 'it is a cat')}
    db = configure(answers)
    monkeypatch.setattr(av.VisionVerifier, 'check',
                        lambda _self, _image, labels, *_a, **_k: ({label: answers[label] for label in labels}, None))
    animal = _alert('bird', 0.8, ai_verify_label='animal', rule_name='A')
    bird = _alert('bird', 0.8, rule_name='B')
    av.verify_and_forward([animal, bird], 10, [], 'Driveway')
    # Each alert is judged by its own question.
    assert forwarded == [[animal]]
    labels = db.metadata[10]['ai_verification']['labels']
    assert labels['animal']['present'] is True and labels['bird']['present'] is False


# ---------------------------------------------------------------------------
# AI relabel: a rejected alert's object raises the alert it really is
# ---------------------------------------------------------------------------

_YARD_RULES = [
    {'name': 'Yard / person', 'object': 'person', 'zone_id': 'yard', 'enabled': True, 'push_enabled': True,
     'ai_verify': True},
    {'name': 'Yard / cat', 'object': 'cat', 'zone_id': 'yard', 'enabled': True, 'push_enabled': True},
    {'name': 'Yard / dog (no delivery)', 'object': 'dog', 'zone_id': 'yard', 'enabled': True},
    {'name': 'Gate / bird', 'object': 'bird', 'zone_id': 'gate', 'enabled': True, 'push_enabled': True},
]


def test_relabel_question_is_asked_only_with_candidates(av):
    assert 'instead' not in av.build_prompt('person')
    prompt = av.build_prompt('person', candidates=['cat', 'dog'])
    assert '"instead": "none"' in prompt and '"cat", "dog"' in prompt
    assert 'instead' in av.build_check_prompt(['person', 'car'], candidates=['cat'])
    assert 'instead' not in av.build_check_prompt(['person', 'car'])


@pytest.mark.parametrize(('reply', 'expected'), [
    ('{"present": false, "reason": "a cat", "instead": "Cat"}', (False, 'a cat', 'cat')),
    ('{"present": false, "reason": "a shadow", "instead": "none"}', (False, 'a shadow', None)),
    # Only a listed candidate, and only on a rejection.
    ('{"present": false, "reason": "a fox", "instead": "fox"}', (False, 'a fox', None)),
    ('{"present": true, "reason": "a person", "instead": "cat"}', (True, 'a person', None)),
    ('No, it is a cat.', (False, 'it is a cat.', None)),
])
def test_verdict_reads_the_relabel(av, reply, expected):
    assert av.parse_verdict(reply, ['cat', 'dog']) == expected
    assert av.parse_verdict(reply)[:2] == expected[:2] and len(av.parse_verdict(reply)) == 2


def test_combined_reply_reads_the_relabel_per_label(av):
    reply = ('{"checks": {"person": {"present": false, "reason": "a cat", "instead": "cat"},'
             ' "car": {"present": false, "reason": "a bin", "instead": "cat"}}}')
    verdicts, _ = av.parse_check_reply(reply, ['person', 'car'], candidates={'person': ['cat']})
    assert verdicts == {'person': (False, 'a cat', 'cat'), 'car': (False, 'a bin')}


def test_candidates_are_the_other_notifying_rules_in_the_alerts_zone(av):
    from app.ai_relabel import relabel_candidates
    triggered = [_alert('person', 0.6, rule_name='Yard / person')]
    assert relabel_candidates(triggered, ['person'], _YARD_RULES) == {'person': ['cat']}
    assert relabel_candidates(triggered, ['person'], _YARD_RULES[:1]) == {}


@pytest.fixture
def relabel(av, monkeypatch):
    dispatch = importlib.import_module('app.alert_dispatch')
    forwarded: list[list[dict]] = []
    monkeypatch.setattr(dispatch, 'submit_alert_notification',
                        lambda _fn, triggered, _event_id, _rules: forwarded.append(list(triggered)) or True)
    monkeypatch.setattr(av, '_read_snapshot', lambda _event: b'jpeg')
    av._state.live_event_presence.clear()

    class Db(_FakeDb):
        def __init__(self, settings, detections):
            super().__init__(settings)
            self.detections = detections
            self.alerts: list[dict] = []

        def get_event(self, event_id):
            return {'id': event_id, 'snapshot_path': 'x.jpg', 'detections': self.detections, 'recording_id': 5}

        def add_alert(self, **row):
            self.alerts.append(row)

        def mark_event_alert_triggered(self, _event_id):
            return True

    def configure(answer, detections=None):
        detections = detections if detections is not None else [
            {'label': 'person', 'confidence': 0.43, 'x': 0.86, 'y': 0.61, 'width': 0.06, 'height': 0.07}]
        db = Db(_settings(av), detections)
        monkeypatch.setattr(av._state, 'database', db)
        asked: list[list | None] = []

        def ask(_self, _image, _label, _camera='', **kw):
            asked.append(kw.get('candidates'))
            return answer if kw.get('candidates') else answer[:2]

        monkeypatch.setattr(av.VisionVerifier, 'ask', ask)
        return db, asked

    yield configure, forwarded
    av._state.live_event_presence.clear()


def test_rejected_person_the_model_calls_a_cat_fires_the_cat_rule(av, relabel):
    configure, forwarded = relabel
    db, asked = configure((False, 'a cat on the drive', 'cat'))
    triggered = [_alert('person', 0.43, rule_name='Yard / person')]
    av.verify_and_forward(triggered, 30, _YARD_RULES, 'Driveway', camera_id='cam')
    assert asked == [['cat']]
    [[alert]] = forwarded
    assert alert['label'] == 'cat' and alert['rule_name'] == 'Yard / cat'
    assert alert['ai_relabel_from'] == 'person' and alert['confidence'] == 0.43
    assert [row['rule_name'] for row in db.alerts] == ['Yard / cat'] and db.alerts[0]['recording_id'] == 5
    record = db.metadata[30]['ai_verification']
    assert record['labels']['person']['instead'] == 'cat' and record['delivered'] is True
    # Known to be a cat now: the person is not re-checked on its next sighting.
    assert av._state.live_event_presence['cam']['cat']['high'] == 1


def test_relabelled_cat_alerts_once_while_it_stays(av, relabel):
    configure, forwarded = relabel
    configure((False, 'a cat', 'cat'))
    triggered = [_alert('person', 0.43, rule_name='Yard / person')]
    av.verify_and_forward(triggered, 31, _YARD_RULES, '', camera_id='cam')
    av.verify_and_forward(triggered, 32, _YARD_RULES, '', camera_id='cam')
    assert [[alert['label'] for alert in batch] for batch in forwarded] == [['cat']]


def test_no_relabel_when_the_detector_already_reported_it(av, relabel):
    """The driveway snapshot: cat 85% and person 68% on one object. The cat's
    own rules already ran; the relabel must not alert it a second time."""
    configure, forwarded = relabel
    db, _asked = configure((False, 'a cat', 'cat'), detections=[
        {'label': 'cat', 'confidence': 0.85, 'x': 0.42, 'y': 0.46, 'width': 0.09, 'height': 0.04},
        {'label': 'person', 'confidence': 0.68, 'x': 0.42, 'y': 0.46, 'width': 0.09, 'height': 0.06},
    ])
    av.verify_and_forward([_alert('person', 0.68, rule_name='Yard / person')], 33, _YARD_RULES, '',
                          camera_id='cam')
    assert forwarded == [] and db.alerts == []


def test_relabel_to_none_filters_as_before(av, relabel):
    configure, forwarded = relabel
    db, _asked = configure((False, 'a shadow', None))
    av.verify_and_forward([_alert('person', 0.43, rule_name='Yard / person')], 34, _YARD_RULES, '',
                          camera_id='cam')
    assert forwarded == [] and db.alerts == []
    assert 'instead' not in db.metadata[34]['ai_verification']['labels']['person']
