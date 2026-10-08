"""Per-rule object size / shape limits (app/zone_schema.py::detection_within_box_limits)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.alerts import AlertEngine  # noqa: E402
from app.zone_detection import (  # noqa: E402
    detection_has_matching_record_rule,
    stamp_frame_aspect,
    zone_object_alert_rules,
    zone_object_rule_matches,
)
from app.zone_schema import (  # noqa: E402
    detection_within_box_limits,
    normalize_monitoring_zones,
    normalize_zone_object_rules,
)


def _person(width, height, confidence=0.8, frame_aspect=None):
    detection = {
        'label': 'person', 'confidence': confidence,
        'box': {'x': 0.1, 'y': 0.1, 'width': width, 'height': height},
    }
    if frame_aspect is not None:
        detection['_frame_aspect'] = frame_aspect
    return detection


def _settings(**limits):
    rule = {'label': 'person', 'enabled': True, 'email_enabled': True, 'min_confidence': 0.5, **limits}
    zones = normalize_monitoring_zones([{
        'id': 'yard', 'name': 'Yard', 'enabled': True, 'monitor_objects': True,
        'x': 0, 'y': 0, 'width': 1, 'height': 1, 'object_rules': [rule],
    }])
    return {'id': 'cam-1', 'name': 'Cam 1', 'detection': {'zones': zones}}


def test_rules_without_limits_normalize_to_none_and_pass_everything():
    rule = normalize_zone_object_rules({'object_rules': [{'label': 'person'}]})[0]
    assert rule['min_box_area'] is None and rule['max_box_area'] is None
    assert rule['min_aspect_ratio'] is None and rule['max_aspect_ratio'] is None
    assert detection_within_box_limits(_person(0.9, 0.9), rule)
    assert detection_within_box_limits(_person(0.001, 0.001), rule)


def test_limits_are_clamped_and_max_never_below_min():
    rule = normalize_zone_object_rules({'object_rules': [{
        'label': 'person', 'min_box_area': 0.2, 'max_box_area': 0.05,
        'min_aspect_ratio': 99, 'max_aspect_ratio': 'junk',
    }]})[0]
    assert rule['min_box_area'] == 0.2
    assert rule['max_box_area'] == 0.2  # raised to the min
    assert rule['min_aspect_ratio'] == 20.0  # clamped to the ceiling
    assert rule['max_aspect_ratio'] is None  # unparseable -> no limit


def test_motion_and_face_rules_never_carry_limits():
    rules = normalize_zone_object_rules({'object_rules': [
        {'label': 'motion', 'min_box_area': 0.1},
        {'label': 'face', 'max_box_area': 0.1},
    ]})
    for rule in rules:
        assert rule['min_box_area'] is None and rule['max_box_area'] is None


def test_area_limits():
    rule = {'min_box_area': 0.01, 'max_box_area': 0.3}
    assert not detection_within_box_limits(_person(0.05, 0.1), rule)  # 0.5% < 1%
    assert detection_within_box_limits(_person(0.2, 0.5), rule)  # 10%
    assert not detection_within_box_limits(_person(0.7, 0.7), rule)  # 49% > 30%


def test_aspect_uses_real_pixel_proportions_when_frame_aspect_known():
    rule = {'max_aspect_ratio': 1.0}
    # Normalized 0.2 x 0.3 on a 16:9 frame is 0.2*16 : 0.3*9 = 3.2 : 2.7 -> wider than tall.
    assert not detection_within_box_limits(_person(0.2, 0.3, frame_aspect=16 / 9), rule)
    # Without the frame aspect the normalized proportions (0.67) are used.
    assert detection_within_box_limits(_person(0.2, 0.3), rule)


def test_detection_without_a_box_passes():
    assert detection_within_box_limits({'label': 'person', 'confidence': 0.9}, {'min_box_area': 0.5})


def test_stamp_frame_aspect():
    detections = [_person(0.1, 0.1)]
    stamp_frame_aspect(detections, {'width': 1920, 'height': 1080})
    assert abs(detections[0]['_frame_aspect'] - 16 / 9) < 1e-5
    stamp_frame_aspect(detections, {'width': 0, 'height': 1080})  # unusable -> unchanged
    assert abs(detections[0]['_frame_aspect'] - 16 / 9) < 1e-5


def test_zone_matcher_alert_rules_and_record_check_all_honour_limits():
    settings = _settings(max_box_area=0.25, max_aspect_ratio=1.0)
    huge = _person(0.8, 0.8, frame_aspect=1.0)
    lying_down = _person(0.4, 0.1, frame_aspect=1.0)
    standing = _person(0.1, 0.3, frame_aspect=1.0)
    for detection, expected in ((huge, False), (lying_down, False), (standing, True)):
        assert bool(zone_object_rule_matches(settings, detection, action='alert')) is expected
        assert bool(zone_object_rule_matches(settings, detection, action='record')) is expected

    alert_rules = zone_object_alert_rules(settings)
    assert alert_rules and alert_rules[0]['max_box_area'] == 0.25
    assert not detection_has_matching_record_rule(huge, alert_rules)
    assert detection_has_matching_record_rule(standing, alert_rules)

    engine = AlertEngine(alert_rules)
    assert engine.process([{**huge, 'zone_id': 'yard'}]) == []
    assert len(engine.process([{**standing, 'zone_id': 'yard'}])) == 1
