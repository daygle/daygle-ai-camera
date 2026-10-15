"""Unit tests for face-detection rule validation (app/face_detection_rules.py)."""
from __future__ import annotations

import app.face_detection_rules as fdr


class _DbStub:
    """Minimal settings-database stand-in (get_setting / set_setting)."""

    def __init__(self, settings=None):
        self._settings = dict(settings or {})
        self.saved = []

    def get_setting(self, key):
        return self._settings.get(key)

    def set_setting(self, key, value, updated_at=None):
        self.saved.append((key, value))
        self._settings[key] = value
        return value


def _rules_payload(*rules):
    return {'rules': list(rules)}


def _unknown_rule(enabled=True, **overrides):
    rule = {
        'id': '_unknown', 'person_id': None, 'name': 'Unknown Person',
        'enabled': enabled, 'email_enabled': False, 'push_enabled': False,
        'email_recipients': '', 'cooldown_minutes': 5,
    }
    rule.update(overrides)
    return rule


# ---------------------------------------------------------------------------
# min_confidence validation
# ---------------------------------------------------------------------------

def test_validate_rule_parses_min_confidence():
    validated = fdr.validate_face_detection_rules(_rules_payload(
        {'id': 'person_1', 'name': 'Alice', 'min_confidence': 0.6},
        {'id': 'person_2', 'name': 'Bob', 'min_confidence': ''},
        {'id': 'person_3', 'name': 'Carol', 'min_confidence': 'not-a-number'},
        {'id': 'person_4', 'name': 'Dave', 'min_confidence': 2.0},
    ))
    rules = validated['rules']
    assert rules[0]['min_confidence'] == 0.6
    assert rules[1]['min_confidence'] is None
    assert rules[2]['min_confidence'] is None
    assert rules[3]['min_confidence'] is None


def test_validate_rule_min_confidence_defaults_none():
    validated = fdr.validate_face_detection_rules(_rules_payload(
        {'id': 'person_1', 'name': 'Alice'},
    ))
    assert validated['rules'][0]['min_confidence'] is None


# ---------------------------------------------------------------------------
# known_face_rules_for_camera (per-rule confidence gate)
# ---------------------------------------------------------------------------

def _known_rule(name='Alice', **overrides):
    rule = {
        'id': 'person_1', 'person_id': 1, 'name': name, 'enabled': True,
        'email_enabled': True, 'push_enabled': False, 'email_recipients': '',
        'cooldown_minutes': 5, 'min_confidence': None,
    }
    rule.update(overrides)
    return rule


def _known_face(track_id, confidence, name='Alice', person_id=1):
    # Mirrors ``face_identity._apply``: a recognised face always carries both
    # the matched person's id and name.
    return {
        'label': 'face', 'recognized': True, 'track_id': track_id,
        'person_id': person_id, 'person_name': name, 'confidence': confidence,
    }


def _use_known_rules(monkeypatch, *rules):
    db = _DbStub({'face_detection_rules': _rules_payload(*rules)})
    monkeypatch.setattr(fdr._state, 'database', db)
    monkeypatch.setattr(fdr, 'effective_face_recognition_config', lambda: {'enabled': True})


def test_known_alert_fires_when_at_or_above_min_confidence(monkeypatch):
    _use_known_rules(monkeypatch, _known_rule(min_confidence=0.7))
    assert fdr.known_face_rules_for_camera('kA', [_known_face(1, 0.7)]) != []
    assert fdr.known_face_rules_for_camera('kB', [_known_face(1, 0.9)]) != []


def test_known_alert_suppressed_below_min_confidence(monkeypatch):
    _use_known_rules(monkeypatch, _known_rule(min_confidence=0.7))
    assert fdr.known_face_rules_for_camera('kC', [_known_face(1, 0.5)]) == []
    # A later, higher-confidence sighting of the same track still alerts.
    assert fdr.known_face_rules_for_camera('kD', [_known_face(1, 0.8)]) != []


def test_known_alert_without_min_confidence_gates_nothing(monkeypatch):
    _use_known_rules(monkeypatch, _known_rule())
    assert fdr.known_face_rules_for_camera('kE', [_known_face(1, 0.1)]) != []


def test_known_alert_fires_once_while_the_person_stays(monkeypatch):
    """No cooldown: the presence gate alerts once per person while they stay
    in view, even when the tracker re-acquires them under a new track id; a
    different person alerts on their own."""
    import app.live_monitor as lm

    _use_known_rules(monkeypatch, _known_rule(), _known_rule(name='Bob', id='person_2', person_id=2))
    fdr._state.live_event_presence.pop('kPresence', None)

    def gate(*faces):
        return lm._new_known_face_alerts(
            'kPresence', fdr.known_face_rules_for_camera('kPresence', list(faces)),
            absence=45.0, recording_id=None,
        )

    try:
        assert [a['rule_name'] for a in gate(_known_face(7, 0.9))] == ['Alice']
        # Every sighting matches; only the first is new.
        assert fdr.known_face_rules_for_camera('kPresence', [_known_face(7, 0.9)]) != []
        assert gate(_known_face(7, 0.9)) == []
        assert gate(_known_face(8, 0.9)) == []  # same person, new track id
        assert [a['rule_name'] for a in gate(_known_face(8, 0.9), _known_face(9, 0.9, name='Bob', person_id=2))] == ['Bob']
    finally:
        fdr._state.live_event_presence.pop('kPresence', None)
