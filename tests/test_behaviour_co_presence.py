"""Multi-object co-presence rules: zone schema, pure matcher, and emission.

A co-presence rule fires when several kinds of object are in the same area
within a short recent-sighting window, rather than on either label alone.
These tests cover ``app.zone_schema.normalize_zone_co_presence``, the
dependency-free decision in ``app.behaviour.co_presence_met``, and the
one-alert-per-episode behaviour of
``app.behaviour_monitor.emit_co_presence_alerts``.

Zone matching is exercised through real geometry (a rectangle covering the
detection box) rather than by stubbing ``app.zone_detection``: ``tests/support.py``
rebuilds the ``app.*`` package tree mid-suite, so a stub on the module collected
here would not be the one the monitor imports.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import app.state as state  # noqa: E402
from app import alert_dispatch, behaviour  # noqa: E402
from app import behaviour_monitor as bm  # noqa: E402
from app.alert_formatting import build_alert_content  # noqa: E402
from app.zone_schema import normalize_monitoring_zones, normalize_zone_co_presence  # noqa: E402


class _FakeDatabase:
    """Records the behaviour events the monitor stores."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def add_event_with_alerts(self, **kwargs):
        self.events.append(kwargs)
        return len(self.events)

    def get_event(self, event_id):
        return None

    def get_setting(self, key):
        return None


@pytest.fixture(autouse=True)
def _isolated_state():
    original_database = state.database
    bm._co_presence_state.clear()
    try:
        yield
    finally:
        state.database = original_database
        bm._co_presence_state.clear()


def _zone(rule_overrides=None, *, zone_id='z1', name='Yard', geometry=None):
    rule = {
        'enabled': True,
        'name': 'Co-presence',
        'all_of': ['cat', 'person'],
        'any_of': [],
        'window_seconds': 15,
        'cooldown_seconds': 0,
        'record_on_detect': False,
        'email_enabled': False,
        'email_recipients': [],
        'push_enabled': False,
        'push_recipients': [],
        'notify_start': None,
        'notify_end': None,
    }
    rule.update(rule_overrides or {})
    geometry = geometry or {}
    x = geometry.get('x', 0.0)
    y = geometry.get('y', 0.0)
    width = geometry.get('width', 1.0)
    height = geometry.get('height', 1.0)
    return {
        'id': zone_id,
        'name': name,
        'enabled': True,
        'x': x,
        'y': y,
        'width': width,
        'height': height,
        'points': [
            {'x': x, 'y': y},
            {'x': x + width, 'y': y},
            {'x': x + width, 'y': y + height},
            {'x': x, 'y': y + height},
        ],
        'co_presence': rule,
    }


def _settings(zone):
    return {'id': 'cam1', 'name': 'Back yard', 'detection': {'zones': [zone]}}


def _det(label, x=0.4):
    return {
        'label': label,
        'confidence': 0.9,
        'box': {'x': x, 'y': 0.4, 'width': 0.1, 'height': 0.1},
    }


class TestCoPresenceSchema:
    def test_absent_rule_is_dropped(self) -> None:
        assert normalize_zone_co_presence({}) is None
        zones = normalize_monitoring_zones([{'name': 'Porch'}])
        assert 'co_presence' not in zones[0]

    def test_defaults_are_filled_in(self) -> None:
        rule = normalize_zone_co_presence({'co_presence': {}})
        assert rule is not None
        assert rule['enabled'] is True
        assert rule['name'] == 'Co-presence'
        assert rule['all_of'] == []
        assert rule['any_of'] == []
        assert rule['window_seconds'] == 15
        assert rule['cooldown_seconds'] == 60
        assert rule['record_on_detect'] is True
        assert rule['email_enabled'] is False
        assert rule['push_enabled'] is False
        assert rule['notify_start'] is None
        assert rule['notify_end'] is None

    def test_labels_are_canonicalized_and_split_between_the_lists(self) -> None:
        rule = normalize_zone_co_presence({'co_presence': {
            'all_of': ['Human', 'pet'],
            # A string is accepted, aliases apply, and a label already required
            # is not repeated as an alternative.
            'any_of': 'person, dog',
            'window_seconds': 30,
        }})
        assert rule['all_of'] == ['person', 'pet']
        assert rule['any_of'] == ['dog']
        assert rule['window_seconds'] == 30

    def test_window_and_cooldown_are_clamped(self) -> None:
        assert normalize_zone_co_presence({'co_presence': {'window_seconds': 0}})['window_seconds'] == 1
        assert normalize_zone_co_presence({'co_presence': {'window_seconds': 9999}})['window_seconds'] == 300
        assert normalize_zone_co_presence({'co_presence': {'window_seconds': 'x'}})['window_seconds'] == 15
        assert normalize_zone_co_presence({'co_presence': {'cooldown_seconds': -5}})['cooldown_seconds'] == 0

    def test_motion_and_face_are_not_object_labels(self) -> None:
        rule = normalize_zone_co_presence({'co_presence': {'all_of': ['motion', 'face', 'cat']}})
        assert rule['all_of'] == ['cat']
        assert rule['any_of'] == []

    def test_attached_through_normalize_monitoring_zones(self) -> None:
        zones = normalize_monitoring_zones([{
            'id': 'z1',
            'name': 'Yard',
            'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}],
            'co_presence': {
                'all_of': ['cat'],
                'any_of': ['person'],
                'email_enabled': True,
                'email_recipients': ['ops@example.com'],
            },
        }])
        rule = zones[0]['co_presence']
        assert rule['all_of'] == ['cat']
        assert rule['any_of'] == ['person']
        assert rule['email_enabled'] is True
        assert rule['email_recipients'] == ['ops@example.com']


class TestCoPresenceMatcher:
    def test_requires_every_all_of_label(self) -> None:
        seen = {'cat': 100.0}
        assert behaviour.co_presence_met([{'cat'}, {'person'}], [], seen, 101.0, 15) is False
        seen['person'] = 101.0
        assert behaviour.co_presence_met([{'cat'}, {'person'}], [], seen, 101.0, 15) is True

    def test_recent_sighting_window_is_respected(self) -> None:
        seen = {'cat': 94.0, 'person': 100.0}
        assert behaviour.co_presence_met([{'cat'}, {'person'}], [], seen, 100.0, 15) is True
        seen['cat'] = 80.0
        assert behaviour.co_presence_met([{'cat'}, {'person'}], [], seen, 100.0, 15) is False

    def test_any_of_needs_one_alternative(self) -> None:
        seen = {'person': 100.0, 'bird': 100.0}
        assert behaviour.co_presence_met([{'person'}], [{'cat'}, {'dog'}], seen, 100.0, 15) is False
        seen['dog'] = 100.0
        assert behaviour.co_presence_met([{'person'}], [{'cat'}, {'dog'}], seen, 100.0, 15) is True

    def test_any_of_alone_is_enough(self) -> None:
        assert behaviour.co_presence_met([], [{'cat'}, {'dog'}], {'dog': 100.0}, 100.0, 15) is True

    def test_empty_rule_never_fires(self) -> None:
        assert behaviour.co_presence_met([], [], {'cat': 100.0, 'person': 100.0}, 100.0, 15) is False

    def test_garbage_input_is_safe(self) -> None:
        assert behaviour.co_presence_met(None, None, None, 1.0, 1.0) is False
        assert behaviour.co_presence_met([{'cat'}], [], {'cat': 'not-a-time'}, 1.0, 1.0) is False


class TestCoPresenceMonitor:
    def test_one_alert_per_episode_and_rearm(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        clock = [1000.0]
        monkeypatch.setattr(bm.time, 'time', lambda: clock[0])
        settings = _settings(_zone())

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat')]) == 0

        clock[0] += 1
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        assert len(database.events) == 1
        event = database.events[0]
        assert event['metadata']['source'] == 'co_presence'
        assert event['metadata']['zone_id'] == 'z1'
        assert event['metadata']['labels'] == ['cat', 'person']
        assert event['alerts'] == []  # no email/push on the rule

        # Still together: the episode is already alerted.
        clock[0] += 1
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 0

        # The cat drops out of the window, so the episode ends.
        clock[0] += 30
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('person', x=0.6)]) == 0

        # Together again: a new episode alerts.
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        assert len(database.events) == 2

    def test_group_label_matches_any_member(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        monkeypatch.setattr(bm.time, 'time', lambda: 2000.0)
        # "pet" (cat or dog) AND a person: a cat satisfies the pet side.
        settings = _settings(_zone({'all_of': ['pet'], 'any_of': ['person']}))

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        assert database.events[0]['metadata']['labels'] == ['cat', 'person']

    def test_slow_still_object_still_counts_inside_the_window(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        clock = [3000.0]
        monkeypatch.setattr(bm.time, 'time', lambda: clock[0])
        settings = _settings(_zone({'window_seconds': 20}))

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat')]) == 0
        # The sitting cat is not detected this cycle; its sighting is 10s old.
        clock[0] += 10
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('person', x=0.6)]) == 1
        assert database.events[0]['metadata']['labels'] == ['cat', 'person']

    def test_objects_outside_the_zone_do_not_count(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        monkeypatch.setattr(bm.time, 'time', lambda: 4000.0)
        # The zone covers the left 40% of the frame; the person is on the right.
        settings = _settings(_zone(geometry={'x': 0.0, 'width': 0.4}))

        assert bm.emit_co_presence_alerts(
            'cam1', settings, [_det('cat', x=0.1), _det('person', x=0.7)],
        ) == 0
        assert database.events == []

    def test_rule_without_labels_never_fires(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        monkeypatch.setattr(bm.time, 'time', lambda: 5000.0)
        settings = _settings(_zone({'all_of': [], 'any_of': []}))

        assert bm.emit_co_presence_alerts(
            'cam1', settings, [_det('cat'), _det('person', x=0.6)],
        ) == 0
        assert database.events == []

    def test_alerting_rule_writes_an_alert_row_and_queues_delivery(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        monkeypatch.setattr(bm.time, 'time', lambda: 6000.0)
        submitted = []
        monkeypatch.setattr(bm, 'submit_alert_notification', lambda *args, **kwargs: submitted.append(args) or True)
        settings = _settings(_zone({
            'email_enabled': True,
            'email_recipients': ['ops@example.com'],
        }))

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        event = database.events[0]
        assert event['alert_triggered'] is True
        assert event['alerts'][0]['label'] == 'cat + person'
        assert 'together' in event['alerts'][0]['message']
        assert submitted, 'the alert should be handed to the delivery pool'

    def test_alert_history_and_notification_name_every_object(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        monkeypatch.setattr(bm.time, 'time', lambda: 8000.0)
        submitted = []
        monkeypatch.setattr(
            bm, 'submit_alert_notification',
            lambda delivery, triggered, event_id, rules: submitted.append((triggered, rules)) or True,
        )
        settings = _settings(_zone({
            'email_enabled': True,
            'email_recipients': ['ops@example.com'],
            'push_enabled': True,
            'push_recipients': [],
        }))

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1

        # The alert-history row names the whole group, not just one label.
        row = database.events[0]['alerts'][0]
        assert row['label'] == 'cat + person'
        assert row['message'] == 'Cat + Person together in Yard'

        # Email and push build their subject/body from this same payload via
        # build_alert_content(), so both name every object that is together.
        triggered, _rules = submitted[0]
        assert triggered[0]['label'] == 'cat + person'
        content = build_alert_content(
            triggered[0],
            event_id=1,
            camera_name='Back yard',
            triggered_labels=alert_dispatch._all_triggered_labels(triggered),
        )
        assert content.subject == 'Daygle AI Camera Alert: Cat + Person Detected (Back yard)'
        assert content.headline == 'Cat + Person'
        assert 'Cat + Person' in content.alert_message
        assert 'Rule: Cat + Person' in content.plain_text

        # Both delivery channels receive that same alert payload (the shared
        # formatter above turns it into their subject and body).
        delivered: list[tuple[str, dict]] = []

        class _RecordingChannel:
            def __init__(self, settings):
                pass

            def send_alert(self, alert, **kwargs):
                delivered.append((type(self).__name__, alert))

        monkeypatch.setattr(alert_dispatch, 'EmailAlertService', _RecordingChannel)
        monkeypatch.setattr(alert_dispatch, 'PushNotificationService', _RecordingChannel)
        monkeypatch.setattr(alert_dispatch, 'effective_email_alert_settings', lambda: {
            'enabled': True, 'host': 'smtp.example', 'from_address': 'ops@example.com',
        })
        monkeypatch.setattr(alert_dispatch, 'effective_push_notification_settings', lambda: {
            'enabled': True, 'server_url': 'https://ntfy.example', 'topic': 'cams',
        })
        alert_dispatch.deliver_alert_notifications(triggered, 1, _rules)
        assert len(delivered) == 2, 'both email and push should receive the alert'
        assert all(payload['label'] == 'cat + person' for _channel, payload in delivered)

    def test_editing_the_labels_rearms_an_active_episode(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        clock = [9000.0]
        monkeypatch.setattr(bm.time, 'time', lambda: clock[0])
        # A non-zero cooldown proves the edited rule's first alert is not muted
        # by the previous episode's cooldown either.
        zone = _zone({'cooldown_seconds': 60})
        settings = _settings(zone)

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        clock[0] += 1
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 0

        # The operator changes the rule to dog + person. The new rule's first
        # match must alert, even though the old episode was still active.
        zone['co_presence']['all_of'] = ['dog', 'person']
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('dog'), _det('person', x=0.6)]) == 1
        assert database.events[-1]['metadata']['labels'] == ['dog', 'person']

    def test_editing_the_window_rearms_an_active_episode(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        clock = [9500.0]
        monkeypatch.setattr(bm.time, 'time', lambda: clock[0])
        zone = _zone({'cooldown_seconds': 60, 'window_seconds': 15})
        settings = _settings(zone)

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        zone['co_presence']['window_seconds'] = 30
        clock[0] += 1
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1

    def test_editing_only_display_fields_keeps_the_episode(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        clock = [9900.0]
        monkeypatch.setattr(bm.time, 'time', lambda: clock[0])
        zone = _zone()
        settings = _settings(zone)

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        zone['co_presence']['name'] = 'Cat and visitor'
        clock[0] += 1
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 0

    def test_deleted_or_disabled_zones_and_rules_drop_their_state(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        clock = [7000.0]
        monkeypatch.setattr(bm.time, 'time', lambda: clock[0])
        zone = _zone()
        other = _zone(zone_id='z2', name='Drive')
        settings = _settings(zone)
        settings['detection']['zones'].append(other)
        # Another camera's episode for the same zone must never be touched.
        bm._co_presence_state['cam2|z1'] = {'labels': {}, 'active': False, 'last_fired': None}

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 2
        assert {'cam1|z1', 'cam1|z2'} <= set(bm._co_presence_state)

        # Disabling a rule drops only its own entry.
        other['co_presence']['enabled'] = False
        clock[0] += 1
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 0
        assert 'cam1|z2' not in bm._co_presence_state
        assert 'cam1|z1' in bm._co_presence_state

        # Deleting the zone drops its entry once the rule is enabled again.
        other['co_presence']['enabled'] = True
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        assert 'cam1|z2' in bm._co_presence_state
        settings['detection']['zones'] = [zone]
        clock[0] += 1
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 0
        assert 'cam1|z2' not in bm._co_presence_state

        # Disabling the zone drops the last entry for this camera.
        zone['enabled'] = False
        clock[0] += 1
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 0
        assert not [key for key in bm._co_presence_state if key.startswith('cam1|')]
        assert 'cam2|z1' in bm._co_presence_state

    def test_camera_motion_reset_rearms_the_episode(self, monkeypatch) -> None:
        database = _FakeDatabase()
        monkeypatch.setattr(state, 'database', database)
        clock = [7000.0]
        monkeypatch.setattr(bm.time, 'time', lambda: clock[0])
        settings = _settings(_zone())

        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        bm.reset_behavioural_state('cam1')
        entry = bm._co_presence_state['cam1|z1']
        assert entry['active'] is False
        assert entry['labels'] == {}

        clock[0] += 1
        assert bm.emit_co_presence_alerts('cam1', settings, [_det('cat'), _det('person', x=0.6)]) == 1
        assert len(database.events) == 2
