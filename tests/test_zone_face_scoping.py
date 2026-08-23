"""Zone-scoped face detection: geometry gating, alert-rule exposure, schema.

A camera whose zones carry an enabled ``face`` rule scopes ALL face
processing to those zones -- faces detected elsewhere are dropped before the
recognition pass. A camera without any face rule keeps the legacy behaviour
exactly (faces follow the generic object-label path).
"""
from __future__ import annotations

from app.alerts import AlertEngine
from app.zone_detection import (
    camera_face_zone_keys,
    filter_detections_for_camera,
    zone_object_alert_rules,
    zone_object_rule_matches,
)
from app.zone_schema import normalize_monitoring_zones


def _settings(zones: list[dict]) -> dict:
    return {'id': 'cam-1', 'name': 'Cam', 'detection': {'zones': zones}}


def _face(x: float, y: float, confidence: float = 0.9) -> dict:
    return {
        'label': 'face',
        'confidence': confidence,
        'box': {'x': x - 0.05, 'y': y - 0.05, 'width': 0.1, 'height': 0.1},
    }


def _person(x: float, y: float) -> dict:
    return {
        'label': 'person',
        'confidence': 0.9,
        'box': {'x': x - 0.05, 'y': y - 0.05, 'width': 0.1, 'height': 0.1},
    }


def _face_zone(**overrides) -> dict:
    zone = {
        'id': 'door',
        'name': 'Door',
        'x': 0.0,
        'y': 0.0,
        'width': 0.5,
        'height': 1.0,
        'enabled': True,
        'monitor_objects': True,
        'object_rules': [
            # email_enabled=True so the rule is reachable on the alert action
            # (zone_object_rule_matches skips inert rules for alerts).
            {'label': 'face', 'enabled': True, 'min_confidence': 0.6, 'max_confidence': 1.0, 'email_enabled': True},
        ],
    }
    zone.update(overrides)
    return zone


def test_face_zone_keys_reports_scoped_zones():
    settings = _settings([_face_zone(), {'id': 'yard', 'name': 'Yard', 'x': 0.5, 'y': 0.0, 'width': 0.5, 'height': 1.0}])
    assert camera_face_zone_keys(settings) == {'door'}


def test_face_inside_face_zone_passes_outside_dropped():
    settings = _settings([
        _face_zone(),
        {'id': 'yard', 'name': 'Yard', 'x': 0.5, 'y': 0.0, 'width': 0.5, 'height': 1.0},
    ])
    kept = filter_detections_for_camera([_face(0.25, 0.5), _face(0.75, 0.5)], settings)
    assert len(kept) == 1
    assert kept[0]['box']['x'] < 0.3


def test_no_face_rule_anywhere_keeps_legacy_behaviour():
    # Without any face rule, faces follow the generic object-label path:
    # no zones configured at all -> accept-all fallback keeps them.
    settings = _settings([])
    assert len(filter_detections_for_camera([_face(0.9, 0.9)], settings)) == 1
    # Zones exist but none carries a face rule -> faces follow the generic
    # object-label path exactly as before this feature: with an explicit
    # allow-list that excludes faces, the face is dropped.
    settings_zoned = _settings([
        {'id': 'yard', 'name': 'Yard', 'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0,
         'object_labels': ['person'],
         'object_rules': [{'label': 'person', 'enabled': True}]},
    ])
    assert filter_detections_for_camera([_face(0.5, 0.5)], settings_zoned) == []


def test_person_unaffected_by_face_zones():
    settings = _settings([_face_zone(monitor_objects=False)])
    # The only zone is face-only (monitor_objects=False): object detections
    # keep the legacy no-object-zones fallback (no camera labels -> accept
    # all), while the FACE axis is scoped to the face-only zone's geometry.
    assert len(filter_detections_for_camera([_person(0.25, 0.5)], settings)) == 1
    assert len(filter_detections_for_camera([_face(0.25, 0.5)], settings)) == 1
    assert filter_detections_for_camera([_face(0.75, 0.5)], settings) == []


def test_zone_object_rule_matches_gates_confidence_window():
    settings = _settings([_face_zone()])
    low = zone_object_rule_matches(settings, _face(0.25, 0.5, confidence=0.5), action='alert')
    inside = zone_object_rule_matches(settings, _face(0.25, 0.5, confidence=0.9), action='alert')
    assert low == []
    assert len(inside) == 1


def test_alert_rules_expose_face_rule_with_contacts():
    zone = _face_zone()
    zone['object_rules'][0].update({
        'email_enabled': True,
        'email_recipients': ['a@example.com'],
        'push_enabled': True,
        'cooldown_seconds': 30,
    })
    settings = _settings([zone])
    rules = zone_object_alert_rules(settings)
    face_rules = [r for r in rules if r['object'] == 'face']
    assert len(face_rules) == 1
    assert face_rules[0]['zone_id'] == 'door'
    assert face_rules[0]['email_recipients'] == ['a@example.com']
    assert face_rules[0]['push_enabled'] is True
    assert face_rules[0]['cooldown_seconds'] == 30


def test_engine_fires_zone_face_alert_with_geometry_stamp():
    zone = _face_zone()
    zone['object_rules'][0].update({'email_enabled': True})
    settings = _settings([zone])
    engine = AlertEngine(zone_object_alert_rules(settings))
    stamped = {**_face(0.25, 0.5), 'zone_id': 'door', 'zone_name': 'Door'}
    alerts = engine.process([stamped], zone_object_alert_rules(settings))
    assert len(alerts) == 1
    assert alerts[0]['label'] == 'face'


def test_normalize_monitoring_zones_derives_monitor_faces():
    normalized = normalize_monitoring_zones([{
        'id': 'door',
        'name': 'Door',
        'x': 0.0,
        'y': 0.0,
        'width': 0.5,
        'height': 1.0,
        'enabled': True,
        'monitor_objects': False,
        'object_rules': [{'label': 'face', 'enabled': True}],
    }])
    zone = normalized[0]
    assert zone['monitor_faces'] is True
    # Faces are not an object class: they must not leak into object_labels.
    assert 'face' not in zone['object_labels']
