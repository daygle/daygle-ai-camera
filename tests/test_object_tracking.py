"""Tests for the lightweight IoU object tracker (app/object_tracking.py)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.object_tracking as ot  # noqa: E402
import app.state as st  # noqa: E402


def _det(label, x, y, w=0.1, h=0.1, conf=0.9):
    return {'label': label, 'confidence': conf, 'box': {'x': x, 'y': y, 'width': w, 'height': h}}


def _reset(cam):
    with st._object_tracks_lock:
        st._object_tracks.pop(cam, None)


def test_same_object_keeps_track_id_across_cycles():
    cam = 'trk-same'
    _reset(cam)
    d1 = ot.update_object_tracks(cam, [_det('person', 0.40, 0.40)])
    tid = d1[0]['track_id']
    assert d1[0]['track_new'] is True and d1[0]['track_age'] == 1
    # Next cycle: the box drifts slightly (overlaps) -> same id, age grows.
    d2 = ot.update_object_tracks(cam, [_det('person', 0.42, 0.41)])
    assert d2[0]['track_id'] == tid
    assert d2[0]['track_new'] is False and d2[0]['track_age'] == 2


def test_distinct_objects_get_distinct_ids():
    cam = 'trk-distinct'
    _reset(cam)
    dets = ot.update_object_tracks(cam, [_det('person', 0.1, 0.1), _det('person', 0.8, 0.8)])
    ids = {d['track_id'] for d in dets}
    assert len(ids) == 2


def test_different_label_does_not_reuse_track():
    cam = 'trk-label'
    _reset(cam)
    a = ot.update_object_tracks(cam, [_det('person', 0.4, 0.4)])
    # A cat in the same spot must NOT inherit the person's track id.
    b = ot.update_object_tracks(cam, [_det('cat', 0.4, 0.4)])
    assert b[0]['track_id'] != a[0]['track_id']
    assert b[0]['track_new'] is True


def test_two_same_label_vehicles_keep_track_identity_when_result_order_changes():
    cam = 'trk-two-cars-order'
    _reset(cam)
    first = ot.update_object_tracks(cam, [
        _det('car', 0.18, 0.45, w=0.22, h=0.14),
        _det('car', 0.62, 0.45, w=0.22, h=0.14),
    ])
    parked_id, moving_id = first[0]['track_id'], first[1]['track_id']

    # The detector commonly changes confidence/order as two same-class cars
    # approach. The boxes move slightly, but the returned order is reversed.
    second = ot.update_object_tracks(cam, [
        _det('car', 0.60, 0.45, w=0.22, h=0.14),
        _det('car', 0.20, 0.45, w=0.22, h=0.14),
    ])
    assert second[0]['track_id'] == moving_id
    assert second[1]['track_id'] == parked_id


def test_track_retired_after_max_age_then_new_id():
    cam = 'trk-age'
    _reset(cam)
    first = ot.update_object_tracks(cam, [_det('car', 0.5, 0.5)])[0]['track_id']
    # Object leaves for more than max_age empty cycles.
    for _ in range(6):
        ot.update_object_tracks(cam, [], max_age=5)
    # Reappearing in the same place gets a fresh id (the old track was retired).
    reappeared = ot.update_object_tracks(cam, [_det('car', 0.5, 0.5)])[0]
    assert reappeared['track_id'] != first
    assert reappeared['track_new'] is True


def test_empty_detections_returns_empty_without_error():
    cam = 'trk-empty'
    _reset(cam)
    assert ot.update_object_tracks(cam, []) == []


def test_iou_basic():
    a = {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}
    assert ot._iou(a, a) == 1.0
    disjoint = {'x': 0.9, 'y': 0.9, 'width': 0.05, 'height': 0.05}
    assert ot._iou(a, disjoint) < 0.01


# ---------------------------------------------------------------------------
# track_displacement annotation (feeds the still/moving classifier)
# ---------------------------------------------------------------------------


def test_displacement_none_until_enough_history():
    cam = 'trk-disp-young'
    _reset(cam)
    first = ot.update_object_tracks(cam, [_det('car', 0.5, 0.5, 0.2, 0.2)])
    assert first[0]['track_displacement'] is None  # one center is no motion evidence
    second = ot.update_object_tracks(cam, [_det('car', 0.505, 0.5, 0.2, 0.2)])
    assert second[0]['track_displacement'] is None  # still under MIN_AGE=3
    third = ot.update_object_tracks(cam, [_det('car', 0.508, 0.5, 0.2, 0.2)])
    # Three matched cycles: tiny jitter -> near-zero displacement.
    assert third[0]['track_displacement'] is not None
    assert third[0]['track_displacement'] <= ot.TRACK_STILL_DISPLACEMENT


def test_stationary_track_reports_near_zero_displacement():
    cam = 'trk-disp-still'
    _reset(cam)
    positions = [(0.4, 0.4)] * 5
    out = None
    for x, y in positions:
        out = ot.update_object_tracks(cam, [_det('car', x, y, 0.25, 0.15)])
    assert out[0]['track_displacement'] is not None
    assert out[0]['track_displacement'] <= ot.TRACK_STILL_DISPLACEMENT


def test_jittery_stationary_track_stays_still():
    """A parked car whose box wobbles a little each frame (routine detector
    noise on a large box) must still read as stationary. The old max-deviation
    measure summed two opposite jitter spikes and reported ~0.014 (> the 0.01
    still threshold) -> a Moving Only rule kept alerting on the parked car."""
    cam = 'trk-disp-jitter'
    _reset(cam)
    # Alternating +/- ~0.8%-of-frame wobble around a fixed center.
    jitter = [0.008, -0.007, 0.006, -0.008, 0.007, -0.006, 0.008, -0.007]
    out = None
    for dx in jitter:
        out = ot.update_object_tracks(cam, [_det('car', 0.40 + dx, 0.40, 0.25, 0.15)])
    assert out[0]['track_displacement'] is not None
    assert out[0]['track_displacement'] <= ot.TRACK_STILL_DISPLACEMENT


def test_moving_track_reports_real_displacement():
    cam = 'trk-disp-moving'
    _reset(cam)
    # A car sweeping left-to-right across the frame, matched by IoU each
    # cycle (0.04 steps on a 0.2-wide box keep IoU ~0.5 per step; larger
    # steps fragment into separate tracks at the 0.3 IoU threshold).
    out = None
    for step in range(10):
        x = 0.05 + step * 0.04
        out = ot.update_object_tracks(cam, [_det('car', x, 0.5, 0.2, 0.1)])
    with st._object_tracks_lock:
        assert len(st._object_tracks[cam]['tracks']) == 1  # stayed one track
    assert out[0]['track_displacement'] is not None
    assert out[0]['track_displacement'] > 0.05


def test_displacement_window_drops_old_positions():
    cam = 'trk-disp-window'
    _reset(cam)
    # Sweep far across the frame, then park. The old traverse must age out of
    # the bounded history so a parked-after-moving subject reads still again.
    moving = None
    for step in range(10):
        moving = ot.update_object_tracks(cam, [_det('car', 0.05 + step * 0.09, 0.5, 0.1, 0.1)])
    parked = None
    # A full history window of parked cycles flushes the traverse out.
    for _ in range(ot.TRACK_DISPLACEMENT_HISTORY):
        parked = ot.update_object_tracks(cam, [_det('car', 0.95, 0.5, 0.1, 0.1)])
    # The traverse and the parked car are one track (motion-gated matching),
    # so this really exercises the window rather than a fresh track.
    assert parked[0]['track_id'] == moving[0]['track_id']
    assert parked[0]['track_displacement'] is not None
    assert parked[0]['track_displacement'] <= ot.TRACK_STILL_DISPLACEMENT


def test_broken_box_chain_does_not_crash_displacement():
    cam = 'trk-disp-nobox'
    _reset(cam)
    # A track whose boxes disappear entirely (detector returned no box) must
    # not crash the displacement math; the annotation just stays None.
    no_box = {'label': 'car', 'confidence': 0.9, 'box': {}}
    out = None
    for _ in range(5):
        out = ot.update_object_tracks(cam, [no_box])
    assert out[0]['track_displacement'] is None


def test_centers_are_bounded_per_track():
    cam = 'trk-disp-bound'
    _reset(cam)
    for step in range(40):
        ot.update_object_tracks(cam, [_det('person', 0.1 + step * 0.01, 0.5, 0.05, 0.05)])
    with st._object_tracks_lock:
        tracks = st._object_tracks[cam]['tracks']
        assert len(tracks) == 1
        assert len(tracks[0]['centers']) <= ot.TRACK_DISPLACEMENT_HISTORY


# ---------------------------------------------------------------------------
# Motion-gated matching: subjects that move further than their own box.
# ---------------------------------------------------------------------------

def test_walking_person_keeps_one_id_when_boxes_stop_overlapping():
    # Recording #16646: a person 3% of the frame wide stepping ~5.5% per cycle
    # never overlaps their previous box, and used to get a new id every sample.
    cam = 'trk-walker'
    _reset(cam)
    ids = []
    for step in range(6):
        out = ot.update_object_tracks(cam, [_det('person', 0.10 + step * 0.055, 0.30, 0.03, 0.15)])
        ids.append(out[0]['track_id'])
    assert len(set(ids)) == 1
    assert out[0]['track_age'] == 6


def test_walking_person_survives_a_missed_cycle():
    cam = 'trk-walker-miss'
    _reset(cam)
    first = ot.update_object_tracks(cam, [_det('person', 0.10, 0.30, 0.03, 0.15)])
    ot.update_object_tracks(cam, [_det('person', 0.155, 0.30, 0.03, 0.15)])
    ot.update_object_tracks(cam, [])  # detector missed this cycle
    out = ot.update_object_tracks(cam, [_det('person', 0.265, 0.30, 0.03, 0.15)])
    assert out[0]['track_id'] == first[0]['track_id']


def test_crossing_walkers_keep_their_own_ids():
    cam = 'trk-crossing'
    _reset(cam)
    left_ids, right_ids = set(), set()
    for step in range(8):
        out = ot.update_object_tracks(cam, [
            _det('person', 0.20 + step * 0.06, 0.30, 0.03, 0.15),
            _det('person', 0.70 - step * 0.06, 0.32, 0.03, 0.15),
        ])
        left_ids.add(out[0]['track_id'])
        right_ids.add(out[1]['track_id'])
    assert len(left_ids) == 1 and len(right_ids) == 1
    assert left_ids != right_ids


def test_motion_match_rejects_a_differently_sized_box():
    cam = 'trk-size-gate'
    _reset(cam)
    car = ot.update_object_tracks(cam, [_det('car', 0.10, 0.50, 0.20, 0.10)])
    out = ot.update_object_tracks(cam, [_det('car', 0.30, 0.50, 0.04, 0.03)])
    assert out[0]['track_id'] != car[0]['track_id']


def test_motion_match_ignores_stale_tracks():
    cam = 'trk-stale-gate'
    _reset(cam)
    first = ot.update_object_tracks(cam, [_det('person', 0.10, 0.30, 0.03, 0.15)])
    for _ in range(ot.MOTION_MATCH_MAX_MISSES + 1):
        ot.update_object_tracks(cam, [])
    out = ot.update_object_tracks(cam, [_det('person', 0.16, 0.30, 0.03, 0.15)])
    assert out[0]['track_id'] != first[0]['track_id']


def test_velocity_is_per_cycle_across_intermittent_misses():
    # Review case: steady 0.04/cycle with misses between sightings. The step
    # observed after a miss spans two cycles; extrapolating it per cycle
    # over-predicted and churned the id on the next intermittent miss.
    cam = 'trk-intermittent'
    _reset(cam)
    ids = []
    for x in (0.10, 0.14, None, 0.22, None, 0.30):
        if x is None:
            ot.update_object_tracks(cam, [])
            continue
        out = ot.update_object_tracks(cam, [_det('person', x, 0.30, 0.03, 0.03)])
        ids.append(out[0]['track_id'])
    assert len(set(ids)) == 1, ids
