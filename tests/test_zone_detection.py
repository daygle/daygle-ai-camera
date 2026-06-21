"""Phase-23 identity + behavior tests for app/zone_detection.py.

Mirrors the Phase-22 test_payload_validators.py pattern: fixture-driven,
monkeypatch-maintained Pool C dependencies, with one identity test per
extracted helper (Pool A rebind wiring) and behavior tests covering every
public path:

- ``point_in_polygon`` -- 0/1/3+ point cases, even-odd parity check.
- ``point_on_segment`` -- colinear + straddling + off-segment cases.
- ``detection_center_in_zone`` -- polygon vs rectangle dispatch.
- ``detection_overlap_ratio_with_zone_rect`` -- partial, full, disjoint.
- ``detection_matches_zone`` -- center + rect overlap paths.
- ``_zone_pixel_motion_fraction`` -- numpy slice + points-only fallback.
- ``zone_motion_detections`` -- enabled/disabled zones, motion rule
  threshold, default-arg lazy resolution.
- ``detection_label_allowed_for_zone`` -- zone allow-list vs camera
  allow-list fall-through.
- ``filter_detections_for_camera_zones`` -- per-monitor-key dispatch,
  empty-zones camera-labels fallback, require_zones=True.
- ``filter_detections_for_camera`` -- object_detection_enabled=False
  short-circuit.
- ``zone_object_rule_matches`` -- label + confidence min + action
  split, _LABEL_ALIASES reach.
- ``zone_object_alert_rules`` -- cooldown_key shape + email_recipients
  + notify_window passthrough.
- ``zone_rule_name`` -- camera / zone / label composition.
- ``zone_alert_detections`` -- zone_id/zone_name stamping + dedupe.
- ``zone_name_for_detection`` -- FIRST match (alert over record).
- ``zone_record_on_detect`` -- bare alias of zone_object_rule_matches.
- ``zone_motion_record_on_detect`` -- motion-axis (monitor_motion=True)
  finding rule with record_on_detect=True.
- ``zone_detection_alert_rule_names`` -- set of matched names.
- ``detection_has_matching_record_rule`` -- label/confidence coverage,
  cooldown ignored, motion min_conf=0 default branch.
- ``normalize_detection_boxes_for_frame`` -- pixel->normalized,
  already-normalized pass-through, missing dimensions pass-through.
- ``get_camera_instance`` -- 404 raise, camera_instances lookup,
  HTTPException detail shape.

The cross-router reach audit (23 main.<attr> test_api.py calls) rely on
the Pool A rebind, so identity tests verify both ``hasattr(main, name)``
and that the binding points at ``app.zone_detection``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def zd():
    """Module handle -- pure import, no fresh load required."""
    from app import zone_detection as _zd
    return _zd


@pytest.fixture
def main_module():
    """Main module handle -- assert the rebind wired every export."""
    import app.main as _main
    return _main


# ---------------------------------------------------------------------------
# Pool A rebind identity tests -- 21 names must resolve to zone_detection
# ---------------------------------------------------------------------------

IDENTITY_NAMES = [
    'get_camera_instance',
    'detection_center_in_zone',
    'detection_overlap_ratio_with_zone_rect',
    'detection_matches_zone',
    'point_in_polygon',
    'point_on_segment',
    'filter_detections_for_camera',
    '_zone_pixel_motion_fraction',
    'zone_motion_detections',
    'detection_label_allowed_for_zone',
    'filter_detections_for_camera_zones',
    'zone_object_rule_matches',
    'zone_object_alert_rules',
    'zone_rule_name',
    'zone_alert_detections',
    'zone_name_for_detection',
    'zone_record_on_detect',
    'zone_motion_record_on_detect',
    'zone_detection_alert_rule_names',
    'detection_has_matching_record_rule',
    'normalize_detection_boxes_for_frame',
]


@pytest.mark.parametrize('name', IDENTITY_NAMES)
def test_pool_a_rebind_wires_helper_to_main(name, main_module, zd):
    """Each Pool A rebind must keep ``main.<name> is zone_detection.<name>``."""
    assert hasattr(main_module, name), f'main missing helper {name}'
    assert getattr(main_module, name) is getattr(zd, name), (
        f'main.{name} resolves to {getattr(main_module, name).__module__!r}, '
        f'expected zone_detection'
    )


# ---------------------------------------------------------------------------
# point_in_polygon / point_on_segment -- geometry primitives
# ---------------------------------------------------------------------------

SQUARE = [
    {'x': 0, 'y': 0},
    {'x': 1, 'y': 0},
    {'x': 1, 'y': 1},
    {'x': 0, 'y': 1},
]


def test_point_in_polygon_rejects_two_points(zd):
    assert zd.point_in_polygon(0.5, 0.5, SQUARE[:2]) is False


def test_point_in_polygon_inside_and_outside_square(zd):
    assert zd.point_in_polygon(0.5, 0.5, SQUARE) is True
    assert zd.point_in_polygon(1.5, 0.5, SQUARE) is False
    assert zd.point_in_polygon(-0.1, 0.5, SQUARE) is False


def test_point_in_polygon_handles_non_numeric_point_dicts(zd):
    """Non-numeric points must be skipped without raising."""
    bad = SQUARE + [{'x': 'x', 'y': 'y'}]
    assert zd.point_in_polygon(0.5, 0.5, bad) is True


def test_point_on_segment_colinear(zd):
    # Point on the segment from (0,0) to (1,0)
    assert zd.point_on_segment(0.5, 0.0, 0, 0, 1, 0) is True


def test_point_on_segment_off_segment(zd):
    assert zd.point_on_segment(0.5, 0.5, 0, 0, 1, 0) is False


# ---------------------------------------------------------------------------
# detection_center_in_zone + detection_overlap_ratio_with_zone_rect
# ---------------------------------------------------------------------------

def test_detection_center_in_zone_polygon_vs_rectangle(zd):
    detection = {'box': {'x': 0.4, 'y': 0.4, 'width': 0.2, 'height': 0.2}}
    polygon_zone = {'points': SQUARE}
    rect_zone = {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}
    assert zd.detection_center_in_zone(detection, polygon_zone) is True
    assert zd.detection_center_in_zone(detection, rect_zone) is True

    outside_detection = {'box': {'x': 5, 'y': 5, 'width': 0.1, 'height': 0.1}}
    assert zd.detection_center_in_zone(outside_detection, polygon_zone) is False
    assert zd.detection_center_in_zone(outside_detection, rect_zone) is False


def test_detection_overlap_ratio_disjoint_returns_zero(zd):
    detection = {'box': {'x': 5, 'y': 5, 'width': 0.1, 'height': 0.1}}
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
    assert zd.detection_overlap_ratio_with_zone_rect(detection, zone) == 0.0


def test_detection_overlap_ratio_fully_inside_returns_one(zd):
    detection = {'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
    assert zd.detection_overlap_ratio_with_zone_rect(detection, zone) == pytest.approx(1.0)


def test_detection_overlap_ratio_partial(zd):
    # Half-and-half overlap: detection [0..0.5], zone [0.25..0.75] in width-height 1x1 square
    detection = {'box': {'x': 0, 'y': 0, 'width': 0.5, 'height': 0.5}}
    zone = {'x': 0.25, 'y': 0.25, 'width': 0.5, 'height': 0.5}
    # Intersection is 0.25 x 0.25 = 0.0625; detection area is 0.25; ratio = 0.25
    assert zd.detection_overlap_ratio_with_zone_rect(detection, zone) == pytest.approx(0.25)


def test_detection_overlap_ratio_zero_dim_returns_zero(zd):
    detection = {'box': {'x': 0, 'y': 0, 'width': 0, 'height': 0}}
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
    assert zd.detection_overlap_ratio_with_zone_rect(detection, zone) == 0.0


def test_detection_matches_zone_center_overrides_overlap(zd):
    # In rectangle zone by center but slight overlap < 0.2
    detection = {'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}}
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
    assert zd.detection_matches_zone(detection, zone) is True


def test_detection_matches_zone_rect_min_overlap_threshold(zd):
    # Center outside rectangle [0..1]x[0..1]; detection {x:0.85, w:0.5} -> center 1.1 OUT,
    # overlap = [0.85..1]x[0.85..1] = 0.0225, detection area = 0.25, ratio = 0.09 (below 0.2).
    detection = {'box': {'x': 0.85, 'y': 0.85, 'width': 0.5, 'height': 0.5}}
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
    assert zd.detection_matches_zone(detection, zone, min_overlap_ratio=0.2) is False
    # Lowered threshold accepts
    assert zd.detection_matches_zone(detection, zone, min_overlap_ratio=0.01) is True


def test_detection_matches_zone_polygon_center_only(zd):
    # Polygon zones are matched by center ONLY -- partial overlap heuristics do not apply.
    detection = {'box': {'x': 0.01, 'y': 0.01, 'width': 0.99, 'height': 0.99}}
    zone = {'points': SQUARE}
    # Outside-the-square center -> not matched (center is at 0.505, 0.505; inside square)
    # Inside detection fully covering the square center IS still inside.
    assert zd.detection_matches_zone(detection, zone) is True


# ---------------------------------------------------------------------------
# _zone_pixel_motion_fraction -- numpy slice + polygon-points fallback
# ---------------------------------------------------------------------------

def test_zone_pixel_motion_fraction_with_rect(zd):
    np = pytest.importorskip('numpy')
    mask = np.zeros((120, 160), dtype=bool)
    mask[60:120, 80:160] = True  # Half is changed in the bottom-right quadrant
    # zone covers the bottom-right half -- bounds [0.5..1, 0.5..1]
    zone = {'x': 0.5, 'y': 0.5, 'width': 0.5, 'height': 0.5}
    fraction = zd._zone_pixel_motion_fraction(mask, zone)
    assert fraction == pytest.approx(1.0, abs=0.01)


def test_zone_pixel_motion_fraction_uses_points_when_rect_missing(zd):
    np = pytest.importorskip('numpy')
    mask = np.zeros((120, 160), dtype=bool)
    mask[40:80, 40:80] = True  # Changed center
    zone_with_points = {
        'points': [{'x': 0.1, 'y': 0.1}, {'x': 0.5, 'y': 0.1}, {'x': 0.5, 'y': 0.5}, {'x': 0.1, 'y': 0.5}],
    }
    # mask covers (40..80, 40..80) which corresponds to normalized (0.25..0.5, 0.33..0.66)
    fraction = zd._zone_pixel_motion_fraction(mask, zone_with_points)
    assert 0.0 < fraction <= 1.0


def test_zone_pixel_motion_fraction_returns_zero_on_exception(zd):
    """Garbage diff_mask must not raise -- returns 0."""
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
    fraction = zd._zone_pixel_motion_fraction('not-a-mask', zone)
    assert fraction == 0.0


# ---------------------------------------------------------------------------
# zone_motion_detections -- default-args + filter
# ---------------------------------------------------------------------------

def test_zone_motion_detections_no_monitor_zones_returns_empty(zd):
    settings = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'detection': {'zones': []},
    }
    assert zd.zone_motion_detections(settings, 0.9, diff_mask=None) == []


def test_zone_motion_detections_disabled_zone_skipped(zd):
    settings = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'detection': {'zones': [{'id': 'z', 'enabled': False, 'monitor_motion': True, 'x': 0, 'y': 0, 'width': 1, 'height': 1}]},
    }
    assert zd.zone_motion_detections(settings, 0.9, diff_mask=None) == []


def test_zone_motion_detections_threshold_filters_low_confidence():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'detection': {
            'zones': [
                {
                    'id': 'z',
                    'enabled': True,
                    'monitor_motion': True,
                    'x': 0,
                    'y': 0,
                    'width': 1,
                    'height': 1,
                    'object_rules': [{'label': 'motion', 'min_confidence': 0.9}],
                },
            ],
        },
    }
    # frame_motion_confidence=0.5 -> below zone threshold of 0.9 -> filtered
    assert zd.zone_motion_detections(settings, 0.5, diff_mask=None) == []


def test_zone_motion_detections_default_gate_fraction_resolves_via_state(
    monkeypatch, zd
):
    """gate_fraction=None must resolve to ``_state._MOTION_GATE_FRACTION`` at call time.

    Ensures the actual gate value is read from app.state by raising
    ``_state._MOTION_GATE_FRACTION`` past the diff_mask fraction and verifying
    the function THEN returns [].
    """
    import app.state as _state
    np = pytest.importorskip('numpy')
    settings = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'detection': {
            'zones': [
                {
                    'id': 'z',
                    'enabled': True,
                    'monitor_motion': True,
                    'x': 0,
                    'y': 0,
                    'width': 1,
                    'height': 1,
                    'object_rules': [{'label': 'motion', 'min_confidence': 0.0}],
                },
            ],
        },
    }
    mask = np.zeros((120, 160), dtype=bool)
    mask[10:110, 10:150] = True  # ~73% of pixels changed

    # Default resolution: gate is _state._MOTION_GATE_FRACTION (0.003) -> zone fires.
    original = _state._MOTION_GATE_FRACTION
    try:
        result_default = zd.zone_motion_detections(settings, 0.5, diff_mask=mask)
        assert len(result_default) == 1

        # Crank the gate up so the SAME mask is below threshold.
        monkeypatch.setattr(_state, '_MOTION_GATE_FRACTION', 0.99)
        result_filtered = zd.zone_motion_detections(settings, 0.5, diff_mask=mask)
        assert result_filtered == [], (
            'gate_fraction must resolve from _state._MOTION_GATE_FRACTION at call time; '
            'cranking the state constant past the diff_mask fraction should filter the zone out'
        )

        # And back to original: zone fires again.
        monkeypatch.setattr(_state, '_MOTION_GATE_FRACTION', original)
        result_restored = zd.zone_motion_detections(settings, 0.5, diff_mask=mask)
        assert len(result_restored) == 1
    finally:
        monkeypatch.setattr(_state, '_MOTION_GATE_FRACTION', original)


# ---------------------------------------------------------------------------
# detection_label_allowed_for_zone -- zone allow-list vs camera allow-list
# ---------------------------------------------------------------------------

def test_detection_label_allowed_for_zone_zones_label_match():
    from app import zone_detection as zd
    zone = {'object_labels': ['person', 'cat']}
    assert zd.detection_label_allowed_for_zone({'label': 'cat'}, zone, set()) is True
    assert zd.detection_label_allowed_for_zone({'label': 'dog'}, zone, set()) is False


def test_detection_label_allowed_for_zone_human_alised_to_person():
    from app import zone_detection as zd
    zone = {'object_labels': ['person']}
    assert zd.detection_label_allowed_for_zone({'label': 'human'}, zone, set()) is True


def test_detection_label_allowed_for_zone_camera_fallback():
    from app import zone_detection as zd
    # No zone allow-list; falls back to camera_labels
    zone = {'object_labels': []}
    assert zd.detection_label_allowed_for_zone({'label': 'cat'}, zone, {'cat', 'dog'}) is True
    assert zd.detection_label_allowed_for_zone({'label': 'bird'}, zone, {'cat', 'dog'}) is False


def test_detection_label_allowed_for_zone_no_allow_list_returns_true():
    from app import zone_detection as zd
    # Both empty -- everyone allowed (no filter)
    assert zd.detection_label_allowed_for_zone({'label': 'anything'}, {}, set()) is True


# ---------------------------------------------------------------------------
# filter_detections_for_camera_zones -- per-monitor_key + dispatch
# ---------------------------------------------------------------------------

def test_filter_for_camera_zones_monitor_objects_filter_to_zone():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'object_labels': ['person'],
            'zones': [
                {'id': 'porch', 'enabled': True, 'monitor_objects': True, 'monitor_motion': True,
                 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'object_labels': ['person']},
            ],
        },
    }
    detections = [{'label': 'person', 'box': {'x': 0.4, 'y': 0.4, 'width': 0.2, 'height': 0.2}}]
    filtered = zd.filter_detections_for_camera_zones(
        detections, settings, zone_monitor_key='monitor_objects'
    )
    assert len(filtered) == 1


def test_filter_for_camera_zones_no_zones_camera_labels_fallback(monkeypatch):
    """When the monitor_axis yields no zones, the camera-level object_labels
    allow-list still filters detections (so a zone-less camera can still
    restrict which objects raise alerts)."""
    from app import zone_detection as zd
    settings = {'id': 'cam-1', 'detection': {'object_labels': ['cat']}}
    detections = [
        {'label': 'cat', 'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}},
        {'label': 'dog', 'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}},
    ]
    # Phase-21 normalize_label_list is the Pool C dependency; defaults exist so
    # we don't need to monkeypatch.
    filtered = zd.filter_detections_for_camera_zones(
        detections, settings, zone_monitor_key='monitor_objects'
    )
    assert [d['label'] for d in filtered] == ['cat']


def test_filter_for_camera_zones_no_zones_require_zones_empty():
    from app import zone_detection as zd
    settings = {'id': 'cam-1', 'detection': {'object_labels': ['cat']}}
    detections = [{'label': 'cat'}, {'label': 'dog'}]
    filtered = zd.filter_detections_for_camera_zones(
        detections, settings, zone_monitor_key='monitor_objects', require_zones=True
    )
    assert filtered == []


def test_filter_for_camera_zones_motion_axis_passes_through_all():
    from app import zone_detection as zd
    # monitor_motion axis does NOT apply object_labels filter
    settings = {
        'id': 'cam-1',
        'detection': {
            'object_labels': ['cat'],  # restrictive camera label filter
            'zones': [],
        },
    }
    detections = [{'label': 'motion', 'box': {}}]
    filtered = zd.filter_detections_for_camera_zones(
        detections, settings, zone_monitor_key='monitor_motion'
    )
    # Without zones and monitor_motion axis, returns detections entirely
    assert filtered == detections


def test_filter_for_camera_short_circuits_when_detection_disabled():
    from app import zone_detection as zd
    settings = {'id': 'cam-1', 'detection': {'object_detection_enabled': False}}
    detections = [{'label': 'person', 'box': {}}]
    assert zd.filter_detections_for_camera(detections, settings) == []


def test_filter_for_camera_passes_through_when_enabled_no_zones(monkeypatch):
    """Without zones AND no object_labels, every detection is kept."""
    from app import zone_detection as zd
    settings = {'id': 'cam-1', 'detection': {'object_detection_enabled': True}}
    detections = [{'label': 'alien', 'box': {}}]
    assert zd.filter_detections_for_camera(detections, settings) == detections


# ---------------------------------------------------------------------------
# zone_object_rule_matches / zone_object_alert_rules
# ---------------------------------------------------------------------------

def test_zone_object_rule_matches_returns_matching_tuple():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'person', 'enabled': True, 'email_enabled': True,
                         'min_confidence': 0.5, 'record_on_detect': True},
                    ],
                },
            ],
        },
    }
    matches = zd.zone_object_rule_matches(
        settings, {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        action='alert'
    )
    assert len(matches) == 1
    zone, rule = matches[0]
    assert rule['label'] == 'person'


def test_zone_object_rule_matches_label_aliases_human_to_person():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'person', 'enabled': True, 'email_enabled': True, 'min_confidence': 0.5}],
                },
            ],
        },
    }
    matches = zd.zone_object_rule_matches(
        settings, {'label': 'human', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        action='alert',
    )
    assert len(matches) == 1


def test_zone_object_rule_matches_below_confidence_excluded():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'person', 'enabled': True, 'email_enabled': True, 'min_confidence': 0.95}],
                },
            ],
        },
    }
    matches = zd.zone_object_rule_matches(
        settings, {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        action='alert',
    )
    assert matches == []


def test_zone_object_rule_matches_action_alert_requires_notify():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    # rule with NO email/push -- alert action filters it out
                    'object_rules': [{'label': 'person', 'enabled': True, 'email_enabled': False, 'push_enabled': False, 'min_confidence': 0.5}],
                },
            ],
        },
    }
    matches = zd.zone_object_rule_matches(
        settings, {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        action='alert',
    )
    assert matches == []


def test_zone_object_alert_rules_email_recipients_cleaned(monkeypatch):
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True, 'id': 'zon', 'name': 'Zone',
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {
                            'label': 'person', 'enabled': True, 'email_enabled': True,
                            'email_recipients': ['admin@example.com', 'BadEntry'],
                            'push_enabled': True, 'min_confidence': 0.5, 'cooldown_seconds': 60,
                        },
                    ],
                },
            ],
        },
    }
    # Patch app.utils.normalize_email_recipients -- zone_object_alert_rules
    # does a runtime ``from app.utils import normalize_email_recipients``,
    # so the patch must live on app.utils (NOT on main, which is never
    # consulted for this helper). This isolates the helper from the real
    # recipient-filtering path so a behaviour change in app.utils cannot
    # mask a regression here.
    #
    # NOTE: we do NOT patch normalize_bool_setting here -- zone_object_alert_rules
    # never calls it (it uses ``bool(rule.get(...))`` directly on each flag).
    import app.utils as _utils
    monkeypatch.setattr(_utils, 'normalize_email_recipients', lambda v: ['admin@example.com'])
    rules = zd.zone_object_alert_rules(settings)
    assert len(rules) == 1
    assert rules[0]['email_recipients'] == ['admin@example.com']
    assert rules[0]['push_enabled'] is True
    assert rules[0]['cooldown_key'] == 'cam-1::zon::person'


def test_zone_rule_name_composes_camera_zone_label():
    from app import zone_detection as zd
    settings = {'name': 'Front Yard', 'id': 'fy'}
    zone = {'name': 'Porch', 'id': 'por'}
    rule = {'label': 'person'}
    assert zd.zone_rule_name(settings, zone, rule) == 'Front Yard / Porch / person'


# ---------------------------------------------------------------------------
# zone_alert_detections -- zone_id/zone_name stamping
# ---------------------------------------------------------------------------

def test_zone_alert_detections_stamps_once_per_zone_index():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True, 'id': 'porch', 'name': 'Porch',
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'person', 'enabled': True, 'email_enabled': True, 'min_confidence': 0.5}],
                },
            ],
        },
    }
    detections = [
        {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
    ]
    # Default monkeypatch for normalize_email_recipients / normalize_bool_setting
    matched = zd.zone_alert_detections(settings, detections)
    assert len(matched) == 2
    assert matched[0]['zone_id'] == 'porch'
    assert matched[0]['zone_name'] == 'Porch'


# ---------------------------------------------------------------------------
# zone_name_for_detection / zone_record_on_detect / zone_motion_record_on_detect
# ---------------------------------------------------------------------------

def test_zone_name_for_detection_returns_first_matching_zone_name():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True, 'id': 'porch', 'name': 'Porch',
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'person', 'enabled': True, 'email_enabled': True, 'min_confidence': 0.5, 'record_on_detect': True}],
                },
            ],
        },
    }
    detection = {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}}
    assert zd.zone_name_for_detection(settings, detection) == 'Porch'


def test_zone_record_on_detect_bare_alias():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'person', 'enabled': True, 'record_on_detect': True, 'min_confidence': 0.5}],
                },
            ],
        },
    }
    detection = {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}}
    assert zd.zone_record_on_detect(detection, settings) is True


def test_zone_motion_record_on_detect_finds_motion_rule():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_motion': True, 'monitor_objects': False,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'motion', 'enabled': True, 'record_on_detect': True},
                    ],
                },
            ],
        },
    }
    assert zd.zone_motion_record_on_detect(settings) is True


def test_zone_motion_record_on_detect_false_when_no_motion_rule():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_motion': True, 'monitor_objects': False,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'person', 'enabled': True, 'record_on_detect': True},
                    ],
                },
            ],
        },
    }
    assert zd.zone_motion_record_on_detect(settings) is False


def test_zone_motion_record_on_detect_skips_monitor_objects_zones():
    from app import zone_detection as zd
    # zone with monitor_objects=True (motion-axis helper should skip)
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_motion': False, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'motion', 'enabled': True, 'record_on_detect': True},
                    ],
                },
            ],
        },
    }
    assert zd.zone_motion_record_on_detect(settings) is False


# ---------------------------------------------------------------------------
# zone_detection_alert_rule_names / detection_has_matching_record_rule
# ---------------------------------------------------------------------------

def test_zone_detection_alert_rule_names_returns_set():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'name': 'Front Yard',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True, 'id': 'porch', 'name': 'Porch',
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'person', 'enabled': True, 'email_enabled': True, 'min_confidence': 0.5}],
                },
            ],
        },
    }
    detection = {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}}
    names = zd.zone_detection_alert_rule_names(settings, detection)
    assert names == {'Front Yard / Porch / person'}


def test_detection_has_matching_record_rule_emits_alias_match():
    """Human detection matches a 'person' rule via _LABEL_ALIASES."""
    from app import zone_detection as zd
    rules = [
        {'enabled': True, 'object': 'person', 'min_confidence': 0.5},
    ]
    assert zd.detection_has_matching_record_rule({'label': 'human', 'confidence': 0.8}, rules) is True


def test_detection_has_matching_record_rule_motion_min_zero():
    from app import zone_detection as zd
    rules = [{'enabled': True, 'object': 'motion', 'min_confidence': 0.0}]
    # Motion default min_confidence is 0.0: any non-falsy confidence matches (`>=`).
    assert zd.detection_has_matching_record_rule({'label': 'motion', 'confidence': 0.0}, rules) is True
    assert zd.detection_has_matching_record_rule({'label': 'motion', 'confidence': 0.3}, rules) is True
    # Negative coverage: with min_confidence > 0, a confidence below the floor is excluded.
    strict = [{'enabled': True, 'object': 'motion', 'min_confidence': 0.5}]
    assert zd.detection_has_matching_record_rule({'label': 'motion', 'confidence': 0.3}, strict) is False


def test_detection_has_matching_record_rule_disabled_skipped():
    from app import zone_detection as zd
    rules = [{'enabled': False, 'object': 'person', 'min_confidence': 0.5}]
    assert zd.detection_has_matching_record_rule({'label': 'person', 'confidence': 0.9}, rules) is False


def test_detection_has_matching_record_rule_empty_label_returns_false():
    from app import zone_detection as zd
    rules = [{'enabled': True, 'object': 'person', 'min_confidence': 0.5}]
    assert zd.detection_has_matching_record_rule({'label': '', 'confidence': 0.9}, rules) is False


def test_detection_has_matching_record_rule_unmatched_label_returns_false():
    from app import zone_detection as zd
    rules = [{'enabled': True, 'object': 'car', 'min_confidence': 0.5}]
    assert zd.detection_has_matching_record_rule({'label': 'cat', 'confidence': 0.9}, rules) is False


# ---------------------------------------------------------------------------
# normalize_detection_boxes_for_frame -- pixel -> normalized
# ---------------------------------------------------------------------------

def test_normalize_detection_boxes_for_frame_pixels_to_normalized():
    from app import zone_detection as zd
    frame = {'width': 1280, 'height': 720}
    detections = [{'label': 'person', 'box': {'x': 640, 'y': 360, 'width': 320, 'height': 180}}]
    out = zd.normalize_detection_boxes_for_frame(detections, frame)
    assert out[0]['box'] == {'x': 0.5, 'y': 0.5, 'width': 0.25, 'height': 0.25}


def test_normalize_detection_boxes_for_frame_already_normalized_returns_input():
    from app import zone_detection as zd
    frame = {'width': 1280, 'height': 720}
    detections = [{'label': 'person', 'box': {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.4}}]
    # max <= 1 -> pass-through (no new list allocated)
    out = zd.normalize_detection_boxes_for_frame(detections, frame)
    assert out == detections
    assert out[0] is detections[0]


def test_normalize_detection_boxes_for_frame_missing_dimensions_pass_through():
    from app import zone_detection as zd
    detections = [{'label': 'person', 'box': {'x': 100, 'y': 100, 'width': 50, 'height': 50}}]
    out = zd.normalize_detection_boxes_for_frame(detections, {'width': 0, 'height': 0})
    assert out == detections


def test_normalize_detection_boxes_for_frame_non_dict_box_pass_through():
    from app import zone_detection as zd
    detections = [{'label': 'person', 'box': 'not-a-dict'}]
    frame = {'width': 1280, 'height': 720}
    out = zd.normalize_detection_boxes_for_frame(detections, frame)
    assert out == detections


# ---------------------------------------------------------------------------
# get_camera_instance -- 404 raise + lookup paths
# ---------------------------------------------------------------------------

def test_get_camera_instance_returns_instance(monkeypatch, zd):
    import app.state as _state
    sentinel_instance = object()
    # get_camera_config is imported at module top in zone_detection; patch there.
    # camera_instances lives on _state; patch there.
    class FakeConfig:
        def __init__(self, source_id):
            self.source_id = source_id
        def __getitem__(self, key):
            return self.source_id
        def get(self, key, default=None):
            return self.source_id if key == 'id' else default

    monkeypatch.setattr(zd, 'get_camera_config', lambda camera_id: FakeConfig('cam-1'))
    monkeypatch.setattr(_state, 'camera_instances', {'cam-1': sentinel_instance})
    assert zd.get_camera_instance('cam-1') is sentinel_instance


def test_get_camera_instance_missing_raises_404(monkeypatch, zd):
    import app.state as _state
    from fastapi import HTTPException

    class FakeConfig:
        def __init__(self, source_id):
            self.source_id = source_id
        def __getitem__(self, key):
            return self.source_id
        def get(self, key, default=None):
            return self.source_id if key == 'id' else default

    monkeypatch.setattr(zd, 'get_camera_config', lambda camera_id: FakeConfig('cam-99'))
    monkeypatch.setattr(_state, 'camera_instances', {})
    with pytest.raises(HTTPException) as exc_info:
        zd.get_camera_instance('cam-99')
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == 'Camera not found'
