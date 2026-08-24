"""Zone-scoped people rules (the Zones-page People Detection card).

Face-detection rules gained optional ``camera_id`` / ``zone_id`` scoping so
per-person and stranger alerts can be confined to one area of one camera.
Unscoped rules keep the legacy global behaviour exactly.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

fdr = importlib.import_module('app.face_detection_rules')  # noqa: E402
fi = importlib.import_module('app.face_identity')  # noqa: E402


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_face_rule_state():
    """The cooldown maps are module-level singletons; leak-free tests reset them."""
    fdr._face_rule_cooldowns.clear()
    fi._alerted_unknown.clear()
    yield
    fdr._face_rule_cooldowns.clear()
    fi._alerted_unknown.clear()


class _DbStub:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def get_setting(self, key):
        return self._settings.get(key)

    def set_setting(self, key, value, updated_at=None):
        self._settings[key] = value
        return value


def _store(monkeypatch, rules):
    db = _DbStub({'face_detection_rules': {'rules': list(rules)}})
    monkeypatch.setattr(fdr._state, 'database', db)
    monkeypatch.setattr(fdr, 'effective_face_recognition_config', lambda: {'enabled': True})
    return db


def _person_rule(name='Alice', **overrides):
    rule = {
        'id': f'person_{name.lower()}', 'person_id': 1, 'name': name,
        'enabled': True, 'email_enabled': True, 'push_enabled': False,
        'email_recipients': 'a@example.com', 'cooldown_minutes': 5,
        'min_confidence': None,
    }
    rule.update(overrides)
    return rule


# ---------------------------------------------------------------------------
# validation keeps scoping fields

def test_validate_rule_preserves_camera_and_zone_scope():
    payload = {'rules': [{'id': 'r1', 'name': 'Alice', 'camera_id': 'cam-1', 'zone_id': 'door'}]}
    validated = fdr.validate_face_detection_rules(payload)
    rule = validated['rules'][0]
    assert rule['camera_id'] == 'cam-1'
    assert rule['zone_id'] == 'door'


def test_validate_rule_defaults_scope_to_global():
    validated = fdr.validate_face_detection_rules({'rules': [{'id': 'r1', 'name': 'Alice'}]})
    assert validated['rules'][0]['camera_id'] == ''
    assert validated['rules'][0]['zone_id'] == ''


# ---------------------------------------------------------------------------
# known-person rule matching honours scope

def _face(track_id=7, zone_id=None, confidence=0.9, person_name='Alice'):
    det = {
        'label': 'face', 'track_id': track_id, 'recognized': True,
        'person_id': 1, 'person_name': person_name, 'confidence': confidence,
    }
    if zone_id is not None:
        det['zone_id'] = zone_id
    return det


def test_scoped_known_rule_fires_only_inside_zone(monkeypatch):
    _store(monkeypatch, [_person_rule(camera_id='cam-1', zone_id='door')])
    inside = fdr.known_face_rules_for_camera('cam-1', [_face(zone_id='door')])
    assert len(inside) == 1
    # Same camera, different zone -> nothing.
    assert fdr.known_face_rules_for_camera('cam-1', [_face(zone_id='yard')]) == []
    # No zone stamp at all -> nothing (the pipeline stamps before calling).
    assert fdr.known_face_rules_for_camera('cam-1', [_face()]) == []
    # Different camera -> nothing.
    assert fdr.known_face_rules_for_camera('cam-2', [_face(zone_id='door')]) == []


def test_unscoped_known_rule_keeps_legacy_anywhere_behaviour(monkeypatch):
    _store(monkeypatch, [_person_rule()])
    # Legacy behaviour: any camera, any zone (or no zone stamp) alerts --
    # once per track until the rule cooldown expires, hence distinct tracks.
    for zone, tid in (('door', 11), (None, 12)):
        assert len(fdr.known_face_rules_for_camera('any-cam', [_face(track_id=tid, zone_id=zone)])) == 1


def test_known_alert_carries_face_rule_id(monkeypatch):
    rule = _person_rule(id='zone:door:person:1', camera_id='cam-1', zone_id='door')
    _store(monkeypatch, [rule])
    alerts = fdr.known_face_rules_for_camera('cam-1', [_face(zone_id='door')])
    assert alerts[0]['face_rule_id'] == 'zone:door:person:1'


# ---------------------------------------------------------------------------
# scoped unknown rules

class _AvailableService:
    available = True


def _use_service(monkeypatch):
    monkeypatch.setattr(fi, 'get_face_recognition_service', lambda: _AvailableService())


def _unknown_face(track_id, zone_id=None, confidence=0.9):
    det = {
        'label': 'face', 'track_id': track_id, 'recognized': True,
        'person_id': None, 'person_name': None, 'confidence': confidence,
    }
    if zone_id is not None:
        det['zone_id'] = zone_id
    return det


def _scoped_unknown(zone_id, **overrides):
    rule = {
        'id': f'_unknown:{zone_id}', 'person_id': None, 'name': 'Unknown Person',
        'enabled': True, 'email_enabled': True, 'push_enabled': False,
        'email_recipients': '', 'cooldown_minutes': 5, 'min_confidence': None,
        'camera_id': '',
        'zone_id': zone_id,
    }
    rule.update(overrides)
    return rule


def test_scoped_unknown_rule_fires_only_in_its_zone(monkeypatch):
    _use_service(monkeypatch)
    _store(monkeypatch, [_scoped_unknown('door')])
    fi.reset_camera_identities('uZ')
    alerts = fi.unknown_face_alerts('uZ', [_unknown_face(1, zone_id='door')])
    assert len(alerts) == 1
    assert alerts[0]['face_rule_ids'] == ['_unknown:door']
    # Outside the zone -> silent; no zone stamp -> silent.
    fi.reset_camera_identities('uZ')
    assert fi.unknown_face_alerts('uZ', [_unknown_face(2, zone_id='yard')]) == []
    fi.reset_camera_identities('uZ')
    assert fi.unknown_face_alerts('uZ', [_unknown_face(3)]) == []


def test_two_zone_unknown_rules_each_fire_once_per_track(monkeypatch):
    _use_service(monkeypatch)
    _store(monkeypatch, [_scoped_unknown('door'), _scoped_unknown('yard')])
    fi.reset_camera_identities('uW')
    # A face stamped to one zone fires only that zone's rule.
    alerts = fi.unknown_face_alerts('uW', [_unknown_face(5, zone_id='door')])
    assert alerts[0]['face_rule_ids'] == ['_unknown:door']
    # Same stranger, now in the other zone: the other rule still fires once.
    alerts = fi.unknown_face_alerts('uW', [_unknown_face(5, zone_id='yard')])
    assert alerts[0]['face_rule_ids'] == ['_unknown:yard']
    # Lingering in the same zone does not re-fire.
    assert fi.unknown_face_alerts('uW', [_unknown_face(5, zone_id='yard')]) == []


def test_is_unknown_rule_matches_zone_variants():
    assert fdr.is_unknown_rule({'id': '_unknown'})
    assert fdr.is_unknown_rule({'id': '_unknown:door'})
    assert not fdr.is_unknown_rule({'id': 'person_1'})
    assert not fdr.is_unknown_rule({'id': '_unknownish'})
