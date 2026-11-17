"""From a cat in view to the notification that leaves the box.

Runs the real live cycle, AI verification (only the model's reply text is
faked), notification dispatch and the real email / push services; only the
SMTP session and the ntfy HTTP request are replaced.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.main as app_main  # noqa: E402  -- break the circular-import gate
assert app_main is sys.modules['app.main']

from tests.test_cat_detection_flow import _drive, _seen  # noqa: E402


def _rule(label, **schedule):
    return {'label': label, 'enabled': True, 'min_confidence': 0.4, 'ai_verify': True,
            'alert_schedules': [{'email_enabled': True, 'push_enabled': True,
                                 'email_recipients': ['owner@example.com'], **schedule}]}


def _deliver(tmp_path, monkeypatch, rules, frames, reply, *, push_down=False):
    mails, pushes = [], []

    def setup(lm):
        import app.ai_verification as av
        import app.email_alerts as email_alerts
        import app.push_notifications as push_notifications
        import app.state as state

        state.database.set_setting('alert_email', {
            'enabled': True, 'host': 'smtp.test', 'port': 587, 'from_address': 'cam@example.com'}, '2026-01-01T00:00:00+00:00')
        state.database.set_setting('alert_push', {
            'enabled': True, 'server_url': 'https://ntfy.test', 'topic': 'garden'}, '2026-01-01T00:00:00+00:00')

        def chat(_self, messages, max_tokens=80):
            answer = reply(messages[-1]['content'][0]['text'])
            if isinstance(answer, Exception):
                raise answer
            return json.dumps(answer)

        @contextlib.contextmanager
        def session(_self):
            yield object()

        def push(_self, title, body, *, tags=None, topic=None):
            if push_down:
                raise push_notifications.PushNotificationUnavailable('ntfy unreachable')
            pushes.append((title, body))

        monkeypatch.setattr(av.VisionVerifier, 'chat', chat)
        monkeypatch.setattr(email_alerts.EmailAlertService, '_create_smtp_session', session)
        monkeypatch.setattr(email_alerts.EmailAlertService, '_send_via',
                            lambda _self, smtp, message: (mails.append(message), smtp)[1])
        monkeypatch.setattr(push_notifications.PushNotificationService, '_deliver', push)
        monkeypatch.setattr(lm, 'submit_alert_notification_with_verification', lambda alerts, event_id, rules, **kw: (
            av._verify_and_forward(alerts, event_id, rules, kw.get('camera_name', ''), None, kw.get('camera_id'))))

    _main, _settings, results, _routed = _drive(tmp_path, monkeypatch, frames, rules=rules, setup=setup)
    from app.alert_dispatch import wait_for_pending_alert_notifications
    wait_for_pending_alert_notifications(10)
    return results, mails, pushes


def _walk(label, cycles=3):
    return [[_seen(label, 0.10 + 0.03 * index)] for index in range(cycles)]


def _yes(_prompt):
    return {'reason': 'a cat on the path', 'present': True}


def test_confirmed_cat_sends_one_email_and_one_push(tmp_path, monkeypatch):
    _results, mails, pushes = _deliver(tmp_path, monkeypatch, [_rule('cat')], _walk('cat', 4), _yes)
    assert [(m['To'], m['Subject']) for m in mails] == [('owner@example.com', 'Daygle AI Camera Alert: Cat Detected (Garden)')]
    assert [title for title, _body in pushes] == ['Daygle AI Camera Alert: Cat Detected (Garden)']
    assert 'Rule: Cat' in pushes[0][1] and 'Confidence: 80.00%' in pushes[0][1]


def test_rejected_or_outside_the_window_sends_nothing(tmp_path, monkeypatch):
    for rules, reply in (
        ([_rule('cat')], lambda _p: {'reason': 'a bag', 'present': False}),
        ([_rule('cat', notify_start='03:00', notify_end='03:01')], _yes),
    ):
        _results, mails, pushes = _deliver(tmp_path, monkeypatch, rules, _walk('cat'), reply)
        assert mails == [] and pushes == []


def test_misread_cat_is_sent_as_a_cat_only_on_the_models_yes(tmp_path, monkeypatch):
    from app.ai_verification import VerificationError

    _results, mails, pushes = _deliver(tmp_path, monkeypatch, [_rule('cat')], _walk('dog'), _yes)
    assert [m['Subject'] for m in mails] == ['Daygle AI Camera Alert: Cat Detected (Garden)']
    assert pushes[0][1].startswith('Cat Detected (AI confirmed; the detector reported a dog)')
    assert 'Confidence:' not in pushes[0][1], "the score was for the detector's label"
    _results, mails, pushes = _deliver(tmp_path, monkeypatch, [_rule('cat')], _walk('dog'),
                                       lambda _p: VerificationError('timed out'))
    assert mails == [] and pushes == []


def test_animal_rule_names_the_animal(tmp_path, monkeypatch):
    _results, _mails, pushes = _deliver(tmp_path, monkeypatch, [_rule('animal')], _walk('bird'),
                                        lambda _p: {'reason': 'a cat', 'present': True, 'kind': 'cat'})
    title, body = pushes[0]
    assert title == 'Daygle AI Camera Alert: Cat Detected (Garden)'
    assert 'the detector reported a bird' in body and 'Rule: Animal' in body


def test_push_outage_still_emails_and_queues_the_push(tmp_path, monkeypatch):
    _results, mails, pushes = _deliver(tmp_path, monkeypatch, [_rule('cat')], _walk('cat'), _yes, push_down=True)
    assert len(mails) == 1 and pushes == []
    import app.alert_outbox as outbox
    assert [(entry['channel'], entry['label']) for entry in outbox.public_entries()] == [('push', 'cat')]


def test_two_cats_arriving_together_notify_once(tmp_path, monkeypatch):
    frames = [[_seen('cat', 0.10 + 0.03 * index), _seen('cat', 0.70 - 0.03 * index)] for index in range(3)]
    _results, mails, pushes = _deliver(tmp_path, monkeypatch, [_rule('cat')], frames, _yes)
    assert len(mails) == 1 and len(pushes) == 1
