"""Phase-23 identity + behavior tests for app/zone_detection.py.

Mirrors the Phase-22 test_payload_validators.py pattern: fixture-driven,
monkeypatch-maintained Pool C dependencies, with one identity test per
extracted helper (Pool A rebind wiring) and behavior tests covering every
public path:

- ``point_in_polygon`` -- 0/1/3+ point cases, even-odd parity check.
- ``point_on_segment`` -- colinear + straddling + off-segment cases.
- ``detection_center_in_zone`` -- polygon vs rectangle dispatch.
- ``detection_matches_zone`` -- center + rect overlap paths.
- ``_zone_pixel_motion_fraction`` -- numpy slice + points-only fallback.
- ``zone_motion_detections`` -- enabled/disabled zones, motion rule
  threshold, default-arg lazy resolution.
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
# detection_center_in_zone + detection_matches_zone
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


def test_detection_matches_zone_center_inside_rect(zd):
    detection = {'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}}
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
    assert zd.detection_matches_zone(detection, zone) is True


def test_detection_matches_zone_rect_is_centre_only(zd):
    # Rectangle zones match by centre exactly like polygons: a box that mostly
    # overlaps the zone but whose centre (0.65, 0.65) is outside it does not match.
    detection = {'box': {'x': 0.3, 'y': 0.3, 'width': 0.7, 'height': 0.7}}
    zone = {'x': 0, 'y': 0, 'width': 0.6, 'height': 0.6}
    assert zd.detection_matches_zone(detection, zone) is False
    assert zd.detection_matches_zone(detection, {'points': [
        {'x': 0, 'y': 0}, {'x': 0.6, 'y': 0}, {'x': 0.6, 'y': 0.6}, {'x': 0, 'y': 0.6},
    ]}) is False


def test_detection_matches_zone_polygon_center_only(zd):
    # Polygon zones are matched by center ONLY -- partial overlap heuristics do not apply.
    detection = {'box': {'x': 0.01, 'y': 0.01, 'width': 0.99, 'height': 0.99}}
    zone = {'points': SQUARE}
    # Outside-the-square center -> not matched (center is at 0.505, 0.505; inside square)
    # Inside detection fully covering the square center IS still inside.
    assert zd.detection_matches_zone(detection, zone) is True


def test_detection_matches_zone_memoizes_per_detection(zd, monkeypatch):
    """The verdict is cached per (zone, box) so repeated tests within a cycle
    compute the geometry once; a box change recomputes."""
    detection = {'box': {'x': 0.0, 'y': 0.0, 'width': 0.1, 'height': 0.1}}
    zone = {'id': 'z1', 'x': 0, 'y': 0, 'width': 1, 'height': 1}
    calls = {'n': 0}
    real = zd._detection_matches_zone_uncached

    def counting(det, z):
        calls['n'] += 1
        return real(det, z)
    monkeypatch.setattr(zd, '_detection_matches_zone_uncached', counting)

    assert zd.detection_matches_zone(detection, zone) is True
    assert zd.detection_matches_zone(detection, zone) is True  # served from the memo
    assert calls['n'] == 1
    assert '_zone_match_memo' in detection

    # A {**det} copy shares the memo and, keeping its box, reuses the verdict.
    assert zd.detection_matches_zone({**detection}, zone) is True
    assert calls['n'] == 1

    # Changing the box busts the cache (different key) and recomputes.
    moved = {**detection, 'box': {'x': 0.9, 'y': 0.9, 'width': 0.05, 'height': 0.05}}
    zd.detection_matches_zone(moved, zone)
    assert calls['n'] == 2


def test_detection_matches_zone_memo_matches_uncached(zd):
    """Memoized result is identical to the uncached geometry for both a rect
    and a polygon zone."""
    for zone in ({'id': 'r', 'x': 0, 'y': 0, 'width': 1, 'height': 1},
                 {'id': 'p', 'points': SQUARE}):
        for box in ({'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1},
                    {'x': 0.9, 'y': 0.9, 'width': 0.4, 'height': 0.4}):
            det = {'box': dict(box)}
            expected = zd._detection_matches_zone_uncached(det, zone)
            assert zd.detection_matches_zone({'box': dict(box)}, zone) is expected


# ---------------------------------------------------------------------------
# _zone_pixel_motion_fraction -- numpy slice + polygon-points fallback
# ---------------------------------------------------------------------------

def test_zone_pixel_motion_fraction_with_rect(zd):
    np = pytest.importorskip('numpy')
    # Mask matches the default thumbnail geometry (240 rows x 320 cols, the
    # 320x240 motion frame).
    mask = np.zeros((240, 320), dtype=bool)
    mask[120:240, 160:320] = True  # Half is changed in the bottom-right quadrant
    # zone covers the bottom-right half -- bounds [0.5..1, 0.5..1]
    zone = {'x': 0.5, 'y': 0.5, 'width': 0.5, 'height': 0.5}
    fraction = zd._zone_pixel_motion_fraction(mask, zone)
    assert fraction == pytest.approx(1.0, abs=0.01)


def test_zone_pixel_motion_fraction_uses_points_when_rect_missing(zd):
    np = pytest.importorskip('numpy')
    mask = np.zeros((240, 320), dtype=bool)
    mask[80:160, 80:160] = True  # Changed center
    zone_with_points = {
        'points': [{'x': 0.1, 'y': 0.1}, {'x': 0.5, 'y': 0.1}, {'x': 0.5, 'y': 0.5}, {'x': 0.1, 'y': 0.5}],
    }
    # mask covers (80..160, 80..160) which corresponds to normalized (0.25..0.5, 0.33..0.66)
    fraction = zd._zone_pixel_motion_fraction(mask, zone_with_points)
    assert 0.0 < fraction <= 1.0


def test_zone_pixel_motion_fraction_returns_zero_on_exception(zd):
    """Garbage diff_mask must not raise -- returns 0."""
    zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
    fraction = zd._zone_pixel_motion_fraction('not-a-mask', zone)
    assert fraction == 0.0


def test_zone_pixel_bounds_edge_zone_is_non_empty(zd):
    """A degenerate zone at the far edge must not yield an empty slice.

    x=1.0 (or y=1.0) with zero width used to map to px1==W, giving an empty
    ``[W:W]`` slice whose ``np.mean`` is NaN -- and NaN passes every gate in
    ``zone_motion_detections`` to emit a bogus NaN-confidence detection."""
    np = pytest.importorskip('numpy')
    mask = np.zeros((240, 320), dtype=bool)
    for zone in (
        {'x': 1.0, 'y': 0.0, 'width': 0.0, 'height': 1.0},
        {'x': 0.0, 'y': 1.0, 'width': 1.0, 'height': 0.0},
    ):
        px1, py1, px2, py2 = zd._zone_pixel_bounds(mask, zone)
        assert px2 > px1 and py2 > py1


def test_zone_pixel_motion_fraction_edge_zone_is_finite(zd):
    """The fraction for a far-edge zone stays finite (never NaN)."""
    np = pytest.importorskip('numpy')
    mask = np.zeros((240, 320), dtype=bool)
    mask[10:20, 10:20] = True
    for zone in (
        {'x': 1.0, 'y': 0.0, 'width': 0.0, 'height': 1.0},
        {'x': 0.0, 'y': 1.0, 'width': 1.0, 'height': 0.0},
    ):
        fraction = zd._zone_pixel_motion_fraction(mask, zone)
        assert not np.isnan(fraction)
        assert 0.0 <= fraction <= 1.0


def test_zone_motion_detections_edge_zone_no_nan_confidence(zd):
    """A far-edge motion zone must not emit a NaN-confidence pseudo-detection."""
    np = pytest.importorskip('numpy')
    mask = np.zeros((240, 320), dtype=bool)
    mask[0:240, 315:320] = True  # activity in the far-right column
    settings = {
        'detection': {
            'zones': [
                {
                    'id': 'edge',
                    'name': 'Edge',
                    'enabled': True,
                    'monitor_motion': True,
                    'x': 1.0, 'y': 0.0, 'width': 0.0, 'height': 1.0,
                }
            ]
        }
    }
    result = zd.zone_motion_detections(settings, 0.9, diff_mask=mask)
    for detection in result:
        assert not np.isnan(float(detection['confidence']))


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


def test_zone_motion_detections_stamps_zone_name_and_id():
    """Motion pseudo-detections must carry the zone name so the playback cards
    can render a Zone row for motion-triggered events."""
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'detection': {
            'zones': [
                {
                    'id': 'porch',
                    'name': 'Porch',
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
    result = zd.zone_motion_detections(settings, 0.9, diff_mask=None)
    assert len(result) == 1
    assert result[0]['zone_id'] == 'porch'
    assert result[0]['zone_name'] == 'Porch'


def test_zone_motion_detections_zone_name_falls_back_to_id():
    """A zone without a human name must still stamp its id as the zone name."""
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'id': 'z1',
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
    result = zd.zone_motion_detections(settings, 0.9, diff_mask=None)
    assert result[0]['zone_name'] == 'z1'


def test_zone_motion_detections_box_tightens_to_changed_pixels(zd):
    """When a diff mask is available the motion detection's box is the
    bounding box of the changed pixels inside the zone, not the zone's full
    rectangle -- so the green overlay square points at where movement
    happened instead of covering the whole area."""
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
    # Changed pixels confined to the top-left quarter of the 240x320 mask.
    mask = np.zeros((240, 320), dtype=bool)
    mask[0:120, 0:160] = True
    result = zd.zone_motion_detections(settings, 0.5, diff_mask=mask)
    assert len(result) == 1
    box = result[0]['box']
    assert box['x'] == 0.0
    assert box['y'] == 0.0
    assert box['width'] == pytest.approx(0.5, abs=0.01)
    assert box['height'] == pytest.approx(0.5, abs=0.01)


def test_zone_motion_detections_box_uses_zone_rect_without_mask(zd):
    """Without a diff mask (first frame, fail-open, forced scan) the box
    falls back to the zone's full rectangle, preserving the historical
    behavior for every downstream overlay."""
    settings = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'detection': {
            'zones': [
                {
                    'id': 'z',
                    'enabled': True,
                    'monitor_motion': True,
                    'x': 0.1,
                    'y': 0.2,
                    'width': 0.5,
                    'height': 0.4,
                    'object_rules': [{'label': 'motion', 'min_confidence': 0.0}],
                },
            ],
        },
    }
    result = zd.zone_motion_detections(settings, 0.9, diff_mask=None)
    assert len(result) == 1
    assert result[0]['box'] == {'x': 0.1, 'y': 0.2, 'width': 0.5, 'height': 0.4}


# ---------------------------------------------------------------------------
# zone allow-list vs camera allow-list
# ---------------------------------------------------------------------------

def test_filter_for_camera_zones_zone_without_labels_uses_camera_labels():
    """A zone with its own ``object_labels`` filters by them; one without
    falls back to the camera's allow-list."""
    from app import zone_detection as zd
    box = {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}
    detections = [{'label': label, 'box': dict(box)} for label in ('cat', 'dog', 'bird')]

    def labels_for(zone_labels):
        settings = {'detection': {'object_labels': ['cat', 'dog'], 'zones': [{
            'id': 'z', 'enabled': True, 'monitor_objects': True,
            'x': 0, 'y': 0, 'width': 1, 'height': 1, 'object_labels': zone_labels,
        }]}}
        kept = zd.filter_detections_for_camera_zones(
            [dict(d) for d in detections], settings, zone_monitor_key='monitor_objects',
        )
        return [d['label'] for d in kept]

    assert labels_for(['cat']) == ['cat']
    assert labels_for([]) == ['cat', 'dog']


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


def test_filter_for_camera_zones_preserves_track_id():
    """The live overlay renders each box's stable tracker id beside its label,
    so ``filter_detections_for_camera_zones`` must pass the ``track_id``
    annotation through unmodified on every path that keeps a detection (zones
    match, zone-less camera-label fallback, face-scope keep, motion axis)."""
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
    detections = [{
        'label': 'person',
        'confidence': 0.9,
        'box': {'x': 0.4, 'y': 0.4, 'width': 0.2, 'height': 0.2},
        'track_id': 7,
        'track_age': 3,
    }]
    # Zones path.
    filtered = zd.filter_detections_for_camera_zones(
        detections, settings, zone_monitor_key='monitor_objects'
    )
    assert filtered and filtered[0]['track_id'] == 7
    # Zone-less camera-label fallback path.
    zoneless = zd.filter_detections_for_camera_zones(
        detections, {'id': 'cam-1', 'detection': {'object_labels': ['person']}},
        zone_monitor_key='monitor_objects',
    )
    assert zoneless and zoneless[0]['track_id'] == 7
    # Motion axis pass-through.
    motion_axis = zd.filter_detections_for_camera_zones(
        detections, settings, zone_monitor_key='monitor_motion'
    )
    assert motion_axis and motion_axis[0]['track_id'] == 7


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


def test_filter_for_camera_zones_no_zones_fallback_aliases_detection_labels():
    """The zone-less camera-label fallback must alias detection labels exactly
    like the zones path: a camera with
    ``object_labels: ['person']`` should accept a detection labeled 'human'.

    Regression: the fallback previously compared the RAW lowercase label
    against the already-aliased ``camera_labels`` set, silently dropping
    'human'/'people'/'pedestrian' detections that the same camera accepted
    once a zone was configured."""
    from app import zone_detection as zd
    settings = {'id': 'cam-1', 'detection': {'object_labels': ['person']}}
    detections = [
        {'label': 'human', 'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}},
        {'label': 'dog', 'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}},
    ]
    filtered = zd.filter_detections_for_camera_zones(
        detections, settings, zone_monitor_key='monitor_objects'
    )
    assert [d['label'] for d in filtered] == ['human']


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

def _one_person_zone_settings():
    return {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'record_on_detect': True, 'alert_schedules': [{'email_enabled': True}]},
                    ],
                },
            ],
        },
    }


def test_zone_object_rule_matches_returns_matching_tuple():
    from app import zone_detection as zd
    settings = _one_person_zone_settings()
    matches = zd.zone_object_rule_matches(
        settings, {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        action='alert'
    )
    assert len(matches) == 1
    zone, rule = matches[0]
    assert rule['label'] == 'person'


def test_zone_object_rule_matches_memoizes_per_detection(monkeypatch):
    """Repeated (settings, action) calls for the same detection compute once;
    a changed confidence, box, action, or settings object recomputes."""
    from app import zone_detection as zd
    settings = _one_person_zone_settings()
    det = {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}}
    calls = {'n': 0}
    real = zd._zone_object_rule_matches_uncached

    def counting(s, d, action):
        calls['n'] += 1
        return real(s, d, action)
    monkeypatch.setattr(zd, '_zone_object_rule_matches_uncached', counting)

    assert len(zd.zone_object_rule_matches(settings, det, action='alert')) == 1
    assert len(zd.zone_object_rule_matches(settings, det, action='alert')) == 1  # memo hit
    assert zd.zone_record_on_detect(det, settings) is True  # action='record' -> one more compute
    assert zd.zone_record_on_detect(det, settings) is True  # memo hit
    assert calls['n'] == 2  # one per (action) only

    # A {**det} copy that keeps its box/label/confidence reuses the memo.
    assert len(zd.zone_object_rule_matches(settings, {**det}, action='alert')) == 1
    assert calls['n'] == 2
    # Changing confidence below the rule minimum recomputes AND changes the result.
    assert zd.zone_object_rule_matches(settings, {**det, 'confidence': 0.1}, action='alert') == []
    assert calls['n'] == 3
    # A different settings object recomputes.
    zd.zone_object_rule_matches(_one_person_zone_settings(), det, action='alert')
    assert calls['n'] == 4


def test_zone_object_rule_matches_label_aliases_human_to_person():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'alert_schedules': [{'email_enabled': True}]}],
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
                    'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.95, 'alert_schedules': [{'email_enabled': True}]}],
                },
            ],
        },
    }
    matches = zd.zone_object_rule_matches(
        settings, {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        action='alert',
    )
    assert matches == []


def test_zone_object_rule_matches_above_max_confidence_excluded():
    """A detection above the rule's ``max_confidence`` upper bound is excluded
    even though it clears ``min_confidence`` -- the per-zone window overrides
    the global slider on the high end too."""
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.4, 'max_confidence': 0.7, 'alert_schedules': [{'email_enabled': True}]}],
                },
            ],
        },
    }
    box = {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}
    # 0.9 is inside [0.4, 1.0] but ABOVE the 0.7 max -> excluded.
    assert zd.zone_object_rule_matches(
        settings, {'label': 'person', 'confidence': 0.9, 'box': box}, action='alert',
    ) == []
    # 0.6 sits inside [0.4, 0.7] -> matches.
    assert len(zd.zone_object_rule_matches(
        settings, {'label': 'person', 'confidence': 0.6, 'box': box}, action='alert',
    )) == 1


def test_zone_object_rule_matches_default_max_confidence_is_no_upper_limit():
    """A rule without ``max_confidence`` keeps its legacy behavior: any
    confidence at or above ``min_confidence`` matches (upper bound = 1.0)."""
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'alert_schedules': [{'email_enabled': True}]}],
                },
            ],
        },
    }
    matches = zd.zone_object_rule_matches(
        settings, {'label': 'person', 'confidence': 0.99, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        action='alert',
    )
    assert len(matches) == 1


def test_zone_object_rule_matches_invalid_action_raises():
    from app import zone_detection as zd
    settings = {'id': 'cam-1', 'detection': {'zones': []}}
    with pytest.raises(ValueError, match="action must be 'alert' or 'record'"):
        zd.zone_object_rule_matches(settings, {}, action='invalid')


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
                    'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'alert_schedules': [{'email_enabled': False, 'push_enabled': False}]}],
                },
            ],
        },
    }
    matches = zd.zone_object_rule_matches(
        settings, {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        action='alert',
    )
    assert matches == []


def test_zone_object_alert_rules_includes_motion_rules_from_motion_only_zones():
    """Motion-only zones (monitor_objects=False, monitor_motion=True) must still
    contribute their MOTION rules to the AlertEngine rule list so email/push
    motion alerts can fire there. The recording axis already honours these
    zones (``zone_motion_record_on_detect``); the alert axis silently skipped
    them before this fix."""
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': False, 'monitor_motion': True,
                    'id': 'porch', 'name': 'Porch',
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'motion', 'enabled': True, 'min_confidence': 0.45, 'cooldown_seconds': 30, 'alert_schedules': [{'email_enabled': True}]},
                    ],
                },
            ],
        },
    }
    rules = zd.zone_object_alert_rules(settings)
    assert len(rules) == 1
    assert rules[0]['object'] == 'motion'
    assert rules[0]['email_enabled'] is True
    assert rules[0]['zone_id'] == 'porch'


def test_zone_object_alert_rules_expands_schedules_with_independent_windows(monkeypatch):
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1', 'name': 'Cam 1',
        'detection': {'zones': [{
            'enabled': True, 'monitor_objects': True, 'id': 'porch', 'name': 'Porch',
            'x': 0, 'y': 0, 'width': 1, 'height': 1,
            'object_rules': [{
                'id': 'person-main', 'label': 'person', 'enabled': True,
                'cooldown_seconds': 45, 'min_confidence': 0.6,
                'alert_schedules': [
                    {'id': 'day', 'email_enabled': True, 'email_recipients': ['day@example.com'], 'active_start': '06:00', 'active_end': '18:00', 'notify_start': '07:00', 'notify_end': '17:00'},
                    {'id': 'night', 'push_enabled': True, 'active_start': '18:00', 'active_end': '06:00', 'notify_start': '19:00', 'notify_end': '05:00'},
                ],
            }],
        }]},
    }
    rules = zd.zone_object_alert_rules(settings)
    assert [rule['name'] for rule in rules] == [
        'Cam 1 / Porch / person [Schedule 1]',
        'Cam 1 / Porch / person [Schedule 2]',
    ]
    assert [rule['schedule_id'] for rule in rules] == ['day', 'night']
    assert [rule['active_start'] for rule in rules] == ['06:00', '18:00']
    assert rules[0]['email_recipients'] == ['day@example.com']
    assert rules[1]['push_enabled'] is True
    assert all(rule['min_confidence'] == 0.6 and 'cooldown_seconds' not in rule for rule in rules)

    from app.alerts import AlertEngine
    import app.alerts as alerts_module
    now = {'value': '10:00'}
    monkeypatch.setattr(alerts_module, '_now_hm_in_admin_tz', lambda: now['value'])
    engine = AlertEngine(rules)
    detection = {'label': 'person', 'confidence': 0.9, 'zone_id': 'porch'}
    assert [alert['rule_name'] for alert in engine.process([detection])] == [rules[0]['name']]
    now['value'] = '20:00'
    assert [alert['rule_name'] for alert in engine.process([detection])] == [rules[1]['name']]


def test_zone_object_alert_rules_keeps_object_rules_inert_in_motion_only_zones():
    """An OBJECT rule inside a motion-only zone stays out of the alert list:
    object detections are filtered through the monitor_objects axis and can
    never carry this zone's id, so exposing the rule would only add dead
    entries to the engine's list."""
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': False, 'monitor_motion': True,
                    'id': 'porch', 'name': 'Porch',
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'alert_schedules': [{'email_enabled': True}]},
                    ],
                },
            ],
        },
    }
    assert zd.zone_object_alert_rules(settings) == []


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
                        {'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'cooldown_seconds': 60, 'alert_schedules': [{'email_enabled': True, 'email_recipients': ['admin@example.com', 'BadEntry'], 'push_enabled': True}]},
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


def test_zone_detection_alert_rule_names_includes_each_configured_schedule():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1', 'name': 'Cam 1',
        'detection': {'zones': [{
            'enabled': True, 'monitor_objects': True, 'id': 'porch', 'name': 'Porch',
            'x': 0, 'y': 0, 'width': 1, 'height': 1,
            'object_rules': [{'id': 'person-main', 'label': 'person', 'enabled': True,
                'alert_schedules': [
                    {'id': 'day', 'email_enabled': True},
                    {'id': 'night', 'push_enabled': True},
                    {'id': 'off'},
                ]}],
        }]},
    }
    detection = {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}}
    assert zd.zone_detection_alert_rule_names(settings, detection) == {
        'Cam 1 / Porch / person [Schedule 1]',
        'Cam 1 / Porch / person [Schedule 2]',
    }


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
                    'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'alert_schedules': [{'email_enabled': True}]}],
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
                    'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'record_on_detect': True, 'alert_schedules': [{'email_enabled': True}]}],
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
                    'id': 'z', 'enabled': True, 'monitor_motion': True, 'monitor_objects': False,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'motion', 'enabled': True, 'record_on_detect': True},
                    ],
                },
            ],
        },
    }
    assert zd.zone_motion_record_on_detect(settings, 'z') is True
    assert zd.zone_motion_record_on_detect(settings, 'other') is False, 'per-zone only'


def test_zone_motion_record_on_detect_false_when_no_motion_rule():
    from app import zone_detection as zd
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'id': 'z', 'enabled': True, 'monitor_motion': True, 'monitor_objects': False,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'person', 'enabled': True, 'record_on_detect': True},
                    ],
                },
            ],
        },
    }
    assert zd.zone_motion_record_on_detect(settings, 'z') is False


def test_zone_motion_record_on_detect_skips_monitor_objects_zones():
    from app import zone_detection as zd
    # zone with monitor_objects=True (motion-axis helper should skip)
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'id': 'z', 'enabled': True, 'monitor_motion': False, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [
                        {'label': 'motion', 'enabled': True, 'record_on_detect': True},
                    ],
                },
            ],
        },
    }
    assert zd.zone_motion_record_on_detect(settings, 'z') is False


# ---------------------------------------------------------------------------
# zone_detection_alert_rule_names
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
                    'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5, 'alert_schedules': [{'email_enabled': True}]}],
                },
            ],
        },
    }
    detection = {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}}
    names = zd.zone_detection_alert_rule_names(settings, detection)
    assert names == {'Front Yard / Porch / person'}


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


# ---------------------------------------------------------------------------
# Umbrella label groups end-to-end through the zone matching pipeline
# ---------------------------------------------------------------------------

def test_filter_for_camera_zones_group_camera_label_fallback(zd):
    settings = {'id': 'cam-1', 'detection': {'object_labels': ['pet']}}
    detections = [
        {'label': 'cat', 'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}},
        {'label': 'dog', 'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}},
        {'label': 'car', 'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}},
    ]
    filtered = zd.filter_detections_for_camera_zones(
        detections, settings, zone_monitor_key='monitor_objects'
    )
    assert sorted(d['label'] for d in filtered) == ['cat', 'dog']


def test_zone_object_rule_matches_group_rule_matches_member(zd):
    settings = {
        'id': 'cam-1',
        'detection': {
            'zones': [
                {
                    'enabled': True, 'monitor_objects': True,
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'object_rules': [{'label': 'animal', 'enabled': True, 'min_confidence': 0.3, 'alert_schedules': [{'email_enabled': True}]}],
                },
            ],
        },
    }
    box = {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}
    assert len(zd.zone_object_rule_matches(settings, {'label': 'cat', 'confidence': 0.8, 'box': box}, action='alert')) == 1
    assert len(zd.zone_object_rule_matches(settings, {'label': 'dog', 'confidence': 0.8, 'box': box}, action='alert')) == 1
    # person is outside the animal group -> no match
    assert zd.zone_object_rule_matches(settings, {'label': 'person', 'confidence': 0.8, 'box': box}, action='alert') == []


def test_rule_rejection_reason_names_the_failed_check():
    """Diagnostics for the recording detection log: say which rule check a
    detection failed rather than just "dropped"."""
    from app import zone_detection as zd
    settings = _one_person_zone_settings()
    rule = settings['detection']['zones'][0]['object_rules'][0]
    rule['min_confidence'] = 0.65
    person = {'label': 'person', 'confidence': 0.55, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.2}}
    assert zd.rule_rejection_reason(settings, person) == 'Below the person rule minimum (0.65)'
    rule['min_confidence'] = 0.5
    rule['max_confidence'] = 0.5
    assert zd.rule_rejection_reason(settings, person) == 'Above the person rule maximum (0.50)'
    rule['max_confidence'] = 1.0
    rule['min_box_area'] = 0.5
    assert zd.rule_rejection_reason(settings, person) == 'Outside the person rule size/shape limits'
    cat = {'label': 'cat', 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}}
    assert zd.rule_rejection_reason(settings, cat) == 'No enabled cat rule in this zone'
    settings['detection']['zones'][0].update({'width': 0.3, 'height': 0.3})
    assert zd.rule_rejection_reason(settings, person) == 'Outside every zone'


def test_camera_watches_label_follows_rules_and_label_lists():
    from app import zone_detection as zd
    settings = _one_person_zone_settings()
    assert zd.camera_watches_label(settings, 'person')
    assert zd.camera_watches_label(settings, 'human')  # alias of person
    assert not zd.camera_watches_label(settings, 'potted plant')
    with_labels = _one_person_zone_settings()
    with_labels['detection']['zones'][0]['object_labels'] = ['car']
    assert zd.camera_watches_label(with_labels, 'car')
    # A camera with nothing configured accepts every label.
    assert zd.camera_watches_label({'id': 'bare', 'detection': {'zones': []}}, 'bench')
