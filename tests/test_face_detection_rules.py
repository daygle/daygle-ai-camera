"""Unit tests for the ``_unknown`` face-rule helpers (app/face_detection_rules.py).

Unknown-person alerting moved from the legacy ``alert_unknown`` /
``alert_unknown_email`` face-recognition settings to the ``_unknown`` system
face-detection rule (Face Rules tab). These tests lock down the two helpers
that back that move:

- ``enabled_unknown_rule`` -- the gate the unknown-face alert path and the
  email/push dispatch use ("is unknown alerting on?").
- ``heal_legacy_unknown_alert_config`` -- the one-time startup migration that
  seeds an ``_unknown`` rule from a legacy config so existing installs keep
  alerting after the settings keys are removed.
"""
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
# enabled_unknown_rule
# ---------------------------------------------------------------------------

def test_enabled_unknown_rule_returns_enabled_rule(monkeypatch):
    db = _DbStub({'face_detection_rules': _rules_payload(_unknown_rule(enabled=True))})
    monkeypatch.setattr(fdr._state, 'database', db)

    rule = fdr.enabled_unknown_rule()
    assert rule is not None
    assert rule['id'] == '_unknown'


def test_enabled_unknown_rule_none_when_disabled(monkeypatch):
    db = _DbStub({'face_detection_rules': _rules_payload(_unknown_rule(enabled=False))})
    monkeypatch.setattr(fdr._state, 'database', db)

    assert fdr.enabled_unknown_rule() is None


def test_enabled_unknown_rule_none_when_absent(monkeypatch):
    db = _DbStub({
        'face_detection_rules': _rules_payload(
            {'id': 'person_1', 'name': 'Alex', 'enabled': True}
        ),
    })
    monkeypatch.setattr(fdr._state, 'database', db)

    assert fdr.enabled_unknown_rule() is None


def test_enabled_unknown_rule_none_when_no_setting_stored(monkeypatch):
    monkeypatch.setattr(fdr._state, 'database', _DbStub({}))
    assert fdr.enabled_unknown_rule() is None


def test_enabled_unknown_rule_none_when_database_uninitialised(monkeypatch):
    monkeypatch.setattr(fdr._state, 'database', None)
    assert fdr.enabled_unknown_rule() is None


# ---------------------------------------------------------------------------
# heal_legacy_unknown_alert_config
# ---------------------------------------------------------------------------

def _legacy_face_recognition_config(alert_unknown=True, email='stranger@example.com'):
    return {
        'enabled': True,
        'alert_unknown': alert_unknown,
        'alert_unknown_email': email,
    }


def test_heal_seeds_unknown_rule_from_legacy_config(monkeypatch):
    db = _DbStub({})
    monkeypatch.setattr(fdr._state, 'database', db)
    monkeypatch.setattr(
        fdr, 'effective_face_recognition_config', _legacy_face_recognition_config
    )

    assert fdr.heal_legacy_unknown_alert_config() is True

    assert db.saved and db.saved[0][0] == 'face_detection_rules'
    rules = db.saved[0][1]['rules']
    assert len(rules) == 1
    rule = rules[0]
    assert rule['id'] == '_unknown'
    assert rule['name'] == 'Unknown Person'
    # Legacy ``alert_unknown`` governed both alert generation and push; email
    # additionally required recipients to be configured.
    assert rule['enabled'] is True
    assert rule['push_enabled'] is True
    assert rule['email_enabled'] is True
    assert rule['email_recipients'] == 'stranger@example.com'


def test_heal_seeds_disabled_rule_from_legacy_email_only(monkeypatch):
    """Legacy email addresses with alert_unknown off map to a disabled rule
    with email ready -- the enabled gate keeps alerts off until the operator
    flips the rule on."""
    db = _DbStub({})
    monkeypatch.setattr(fdr._state, 'database', db)
    monkeypatch.setattr(
        fdr, 'effective_face_recognition_config',
        lambda: _legacy_face_recognition_config(alert_unknown=False),
    )

    assert fdr.heal_legacy_unknown_alert_config() is True
    rule = db.saved[0][1]['rules'][0]
    assert rule['enabled'] is False
    assert rule['push_enabled'] is False
    assert rule['email_enabled'] is True


def test_heal_noop_when_unknown_rule_already_exists(monkeypatch):
    db = _DbStub({
        'face_detection_rules': _rules_payload(_unknown_rule(enabled=False)),
    })
    monkeypatch.setattr(fdr._state, 'database', db)
    monkeypatch.setattr(
        fdr, 'effective_face_recognition_config', _legacy_face_recognition_config
    )

    assert fdr.heal_legacy_unknown_alert_config() is False
    assert db.saved == []


def test_heal_noop_without_legacy_config(monkeypatch):
    db = _DbStub({})
    monkeypatch.setattr(fdr._state, 'database', db)
    monkeypatch.setattr(
        fdr, 'effective_face_recognition_config', lambda: {'enabled': True}
    )

    assert fdr.heal_legacy_unknown_alert_config() is False
    assert db.saved == []


def test_heal_noop_when_database_uninitialised(monkeypatch):
    monkeypatch.setattr(fdr._state, 'database', None)
    monkeypatch.setattr(
        fdr, 'effective_face_recognition_config', _legacy_face_recognition_config
    )

    assert fdr.heal_legacy_unknown_alert_config() is False


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


def _known_face(track_id, confidence, name='Alice'):
    return {
        'label': 'face', 'recognized': True, 'track_id': track_id,
        'person_name': name, 'confidence': confidence,
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


def test_known_alert_debounced_per_track_by_cooldown(monkeypatch):
    # The cooldown claim under _face_rule_cooldown_lock means a lingering subject
    # alerts once per track and is then suppressed until the window elapses.
    _use_known_rules(monkeypatch, _known_rule(cooldown_minutes=5))
    fdr._face_rule_cooldowns.pop('kCooldown', None)
    assert fdr.known_face_rules_for_camera('kCooldown', [_known_face(7, 0.9)]) != []
    # Same track, same camera, still inside the 5-minute window -> suppressed.
    assert fdr.known_face_rules_for_camera('kCooldown', [_known_face(7, 0.9)]) == []
    # A different track on the same camera is unaffected by the first's cooldown.
    assert fdr.known_face_rules_for_camera('kCooldown', [_known_face(8, 0.9)]) != []


def test_cooldown_lock_is_a_real_module_level_lock():
    # Regression guard: the cooldown guard must be a single lock object built at
    # import, not lazily (a lazy initializer could race two callers into two
    # different locks and silently defeat the double-alert guard).
    import threading
    assert isinstance(fdr._face_rule_cooldown_lock, type(threading.Lock()))
