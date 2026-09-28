"""Regression tests for the model-management, notification and utility fixes.

* Face-rule (per-person and unknown-face) emails honour the rule's Email
  toggle; recipients alone must not send.
* Line breaks in camera/rule names cannot break email Subjects or ntfy Titles.
* Push settings reject non-http(s) server URLs and topics ntfy cannot serve.
* The Cloudflare Tunnel token file is written 0600.
* Camera offline/recovery notifications are queued, not delivered inline on
  the live-alert monitor thread.
* Bare IPv6 camera hosts are bracketed in the RTSP URL.
"""
from __future__ import annotations

import importlib
import os
import stat

import pytest
from fastapi import HTTPException

from tests.support import _load_app


class _CapturingMailer:
    captured: list[dict] = []

    def __init__(self, settings):
        self.settings = settings

    def send_alert(self, alert, *, event_id, recipients=None, **_kwargs):
        _CapturingMailer.captured.append({'rule': alert.get('rule_name'), 'recipients': recipients})


def _face_rule(rule_id, *, email_enabled, name='Alice', person_id='1'):
    return {
        'id': rule_id, 'person_id': person_id, 'name': name, 'enabled': True,
        'email_enabled': email_enabled, 'push_enabled': False,
        'email_recipients': 'owner@example.com', 'cooldown_minutes': 5,
    }


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    ad = importlib.import_module('app.alert_dispatch')
    fdr = importlib.import_module('app.face_detection_rules')
    _CapturingMailer.captured = []
    monkeypatch.setattr(ad, 'effective_email_alert_settings', lambda: {'enabled': True})
    monkeypatch.setattr(ad, 'EmailAlertService', _CapturingMailer)
    monkeypatch.setattr(
        ad._state.database, 'get_event',
        lambda _event_id: {'metadata': {}, 'snapshot_path': None, 'created_at': ''},
    )

    def use_rules(*rules):
        unknown = next((rule for rule in rules if rule['id'] == '_unknown'), None)
        monkeypatch.setattr(fdr, 'effective_face_detection_rules', lambda: {'rules': list(rules)})
        monkeypatch.setattr(fdr, 'enabled_unknown_rule', lambda: unknown)

    return ad, use_rules


def test_person_face_rule_email_respects_email_toggle(dispatch):
    ad, use_rules = dispatch
    alert = {'label': 'face', 'rule_name': 'Alice', 'face_rule_id': 'person_1', 'confidence': 0.9, 'message': 'm'}

    use_rules(_face_rule('person_1', email_enabled=False))
    ad.deliver_email_alerts([alert], 1, rules=[])
    assert _CapturingMailer.captured == []

    use_rules(_face_rule('person_1', email_enabled=True))
    ad.deliver_email_alerts([alert], 2, rules=[])
    assert _CapturingMailer.captured == [{'rule': 'Alice', 'recipients': ['owner@example.com']}]


def test_unknown_face_email_respects_email_toggle(dispatch):
    ad, use_rules = dispatch
    alert = {'label': 'face', 'rule_name': 'Unknown face', 'face_rule_ids': ['_unknown'], 'confidence': 0.9, 'message': 'm'}

    use_rules(_face_rule('_unknown', email_enabled=False, name='Unknown Person', person_id=None))
    ad.deliver_email_alerts([alert], 1, rules=[])
    assert _CapturingMailer.captured == []

    use_rules(_face_rule('_unknown', email_enabled=True, name='Unknown Person', person_id=None))
    ad.deliver_email_alerts([alert], 2, rules=[])
    assert len(_CapturingMailer.captured) == 1


def test_line_breaks_in_names_cannot_break_notification_headers():
    email_alerts = importlib.import_module('app.email_alerts')
    push = importlib.import_module('app.push_notifications')

    assert email_alerts._encode_subject('Front\r\nBcc: x@evil.test') == 'Front Bcc: x@evil.test'
    assert '\n' not in push._encode_ntfy_header('Gate\nX-Injected: 1')


@pytest.mark.parametrize('server_url', ['file:///etc/passwd', 'ftp://ntfy.example', 'ntfy.sh', 'https://'])
def test_push_settings_reject_non_http_server_urls(monkeypatch, server_url):
    validators = importlib.import_module('app.payload_validators')
    monkeypatch.setattr(validators, 'effective_push_notification_settings', lambda: {})
    with pytest.raises(HTTPException):
        validators.validate_push_notification_settings({'server_url': server_url, 'topic': 'cams'})


@pytest.mark.parametrize('topic', ['cams/../admin', 'cams?x=1', 'cams#frag', 'a' * 65])
def test_push_settings_reject_topics_ntfy_cannot_serve(monkeypatch, topic):
    validators = importlib.import_module('app.payload_validators')
    monkeypatch.setattr(validators, 'effective_push_notification_settings', lambda: {})
    with pytest.raises(HTTPException):
        validators.validate_push_notification_settings({'server_url': 'https://ntfy.sh', 'topic': topic})


def test_push_settings_accept_a_normal_topic(monkeypatch):
    validators = importlib.import_module('app.payload_validators')
    monkeypatch.setattr(validators, 'effective_push_notification_settings', lambda: {})
    settings = validators.validate_push_notification_settings(
        {'enabled': True, 'server_url': 'http://ntfy.lan:8080', 'topic': 'front-door_cams'},
    )
    assert settings['topic'] == 'front-door_cams'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes')
def test_tunnel_token_file_is_private(tmp_path):
    tunnel = importlib.import_module('app.cloudflare_tunnel')
    old_umask = os.umask(0o022)
    try:
        store = tunnel.CloudflareTunnelSecretStore(str(tmp_path / 'app.sqlite3'))
        store.write('secret-token')
        store.write('rotated-token')
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.read() == 'rotated-token'
    assert not store.path.with_suffix('.tmp').exists()


def test_camera_offline_notification_is_queued_not_sent_inline(monkeypatch):
    camera_health = importlib.import_module('app.camera_health')
    postprocess_pool = importlib.import_module('app.postprocess_pool')
    submitted: list[tuple] = []

    class _Pool:
        def submit(self, fn, *args, **_kwargs):
            submitted.append((fn, args))
            return True

    marked: list[str] = []
    monkeypatch.setattr(postprocess_pool, 'notification_pool', lambda: _Pool())
    monkeypatch.setattr(camera_health, '_mark_camera_offline_notified', marked.append)

    monkeypatch.setattr(camera_health, 'effective_camera_offline_alert_settings', lambda: {'enabled': False})
    camera_health._submit_camera_notification('cam', 'Gate', 'offline')
    assert submitted == [] and marked == []

    monkeypatch.setattr(camera_health, 'effective_camera_offline_alert_settings', lambda: {'enabled': True})
    camera_health._submit_camera_notification('cam', 'Gate', 'offline')
    assert marked == ['cam']
    assert submitted == [(camera_health._deliver_camera_offline_notification, ('cam', 'Gate', 'offline'))]


def test_ipv6_camera_host_is_bracketed():
    utils = importlib.import_module('app.utils')
    assert utils.build_stream_url({'host': 'fd00::10', 'username': 'u', 'password': 'p@ss'}) == (
        'rtsp://u:p%40ss@[fd00::10]:554/stream1'
    )
    assert utils.build_stream_url({'host': 'cam.local'}) == 'rtsp://cam.local:554/stream1'
    assert utils.build_stream_url({'host': '[fd00::10]', 'port': 8554}) == 'rtsp://[fd00::10]:8554/stream1'
