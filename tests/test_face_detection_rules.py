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
