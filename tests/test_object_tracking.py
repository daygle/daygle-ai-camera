"""Tests for the lightweight IoU object tracker (app/object_tracking.py)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.object_settings as os_  # noqa: E402
import app.object_tracking as ot  # noqa: E402
import app.state as st  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402


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


def test_approaching_subject_reports_motion_from_scale_alone():
    """Realistic head-on approach, measured rather than assumed.

    Geometry is a pinhole model: a 1.7m subject at ~8m shrinking to ~6.4m over
    8 cycles (~0.23 m/s), camera above the path so the box grows about a fixed
    foot anchor. Run against the threshold this is the band where a center-only
    measure lands UNDER it (0.0091) while the box's own growth lands OVER it
    (0.0181) -- i.e. the case this change actually corrects.

    Boundaries are asserted too, so the test cannot be mistaken for "approaching
    subjects are now always detected": at 12m the box changes by less than 1% of
    frame and BOTH measures call it still, and at 6m->3m the center-only measure
    already clears the threshold on its own. The change widens sensitivity by
    roughly 2x, it does not rescue distant approaches.
    """
    cam = 'trk-disp-approach-real'
    _reset(cam)
    out = None
    for step in range(8):
        distance = 8.0 - step * (1.6 / 7)
        height = 1.02 / distance
        out = ot.update_object_tracks(cam, [_det('person', 0.445, 0.72 - height, 0.11, height)])
    with st._object_tracks_lock:
        track = st._object_tracks[cam]['tracks'][0]
    centers = track['centers']
    mid = len(centers) // 2
    mean = lambda pts, axis: sum(p[axis] for p in pts) / len(pts)
    centre_only = max(
        abs(mean(centers[mid:], 0) - mean(centers[:mid], 0)),
        abs(mean(centers[mid:], 1) - mean(centers[:mid], 1)),
    )
    # The pre-fix measure really is below the threshold -- that is the bug.
    assert centre_only <= ot.TRACK_STILL_DISPLACEMENT
    assert out[0]['track_displacement'] > ot.TRACK_STILL_DISPLACEMENT
    quiet = np.zeros((72, 128), dtype=bool)
    assert os_.detection_motion_state(out[0], quiet, out[0]['track_displacement']) == os_.MODE_MOVING


def test_scale_term_verdict_is_independent_of_box_size():
    """The same relative growth must give the same verdict whatever the box size.

    Previously the scale term was thresholded in ABSOLUTE units while its noise
    is proportional to the box, so a small object's identical relative growth
    read as no motion and a large parked box cleared the bar on detector drift
    alone -- measured at the time as a hard visibility floor around 0.11 of
    frame. Thresholding the term in relative units removes both halves of that.
    """
    quiet = np.zeros((72, 128), dtype=bool)

    def displacement(size, growth_per_cycle):
        cam = f'trk-disp-sizeinv-{size}-{growth_per_cycle}'
        _reset(cam)
        out = None
        for step in range(8):
            grown = size * (1 + growth_per_cycle * step / 7)
            out = ot.update_object_tracks(cam, [
                _det('car', 0.50 - grown / 2, 0.50 - grown / 2, grown, grown)])
        return out[0]['track_displacement']

    # Approaching: identical relative growth, boxes 11x apart in size. The
    # signal is now identical too, not merely both above threshold.
    small, large = displacement(0.05, 0.16), displacement(0.55, 0.16)
    assert small == pytest.approx(large, abs=1e-9)
    assert small > ot.TRACK_STILL_DISPLACEMENT
    assert os_.detection_motion_state(_det('car', 0.3, 0.3), quiet, small) == os_.MODE_MOVING

    # Drifting: identical relative detector noise, again 11x apart. Both must
    # stay still -- the large box used to be the exposed one.
    small, large = displacement(0.05, 0.04), displacement(0.55, 0.04)
    assert small == pytest.approx(large, abs=1e-9)
    assert small <= ot.TRACK_STILL_DISPLACEMENT
    assert os_.detection_motion_state(_det('car', 0.3, 0.3), quiet, small) == os_.MODE_STILL


def test_long_range_slow_approach_is_the_honest_remaining_miss():
    """The real lower bound: a SLOW approach at LONG range lands just under the
    threshold and reads still.

    An earlier version of this test pinned the miss at 12m using a box with a
    FIXED width while only the height grew. The measured size is max(w, h), so
    the constant width clamped the measure and zeroed the scale term -- the
    same measurement artifact that produced a wrong distance bound in the PR
    description. With a proportional box (w = 0.45 x h, as a person actually is)
    a 12m approach is clearly detected. The genuine miss is ~20m at 0.23 m/s.
    """
    def displacement(d0, d1):
        cam = f'trk-disp-range-{d0}'
        _reset(cam)
        out = None
        for step in range(8):
            distance = d0 + (d1 - d0) * step / 7
            height = 1.02 / distance
            width = 0.45 * height
            out = ot.update_object_tracks(cam, [
                _det('person', 0.50 - width / 2, 0.72 - height, width, height)])
        return out[0]['track_displacement']

    # ~20m at walking pace: just under. This is a tuning choice, not a limit.
    assert displacement(20.0, 18.4) <= ot.TRACK_STILL_DISPLACEMENT
    # Same pace at 16m and 12m: clearly detected. Pins that the miss is a
    # distance/speed product, not "distant things are invisible".
    assert displacement(16.0, 14.4) > ot.TRACK_STILL_DISPLACEMENT
    assert displacement(12.0, 10.5) > ot.TRACK_STILL_DISPLACEMENT


def test_close_fast_approach_was_already_moving_pre_fix():
    """The honest upper bound: a 6m->3m approach moves the center far enough that
    the OLD measure already cleared the threshold. The change only widens the
    margin here; it is not what makes this case work."""
    cam = 'trk-disp-close'
    _reset(cam)
    out = None
    for step in range(8):
        distance = 6.0 - step * (3.0 / 7)
        height = 1.02 / distance
        out = ot.update_object_tracks(cam, [_det('person', 0.44, 0.72 - height, 0.12, height)])
    with st._object_tracks_lock:
        track = st._object_tracks[cam]['tracks'][0]
    centers = track['centers']
    mid = len(centers) // 2
    mean = lambda pts, axis: sum(p[axis] for p in pts) / len(pts)
    centre_only = max(
        abs(mean(centers[mid:], 0) - mean(centers[:mid], 0)),
        abs(mean(centers[mid:], 1) - mean(centers[:mid], 1)),
    )
    assert centre_only > ot.TRACK_STILL_DISPLACEMENT
    assert out[0]['track_displacement'] > centre_only


def test_approaching_subject_scale_growth_without_translation():
    """The degenerate worst case, kept as a unit check: the box center is
    EXACTLY parked and only the size grows. The center-only measure is exactly
    zero here, so anything above it came from the scale term alone."""
    cam = 'trk-disp-approach'
    _reset(cam)
    # Centre parked; the box grows 0.04 -> 0.22 walking straight at the lens.
    # Width tracks height at a fixed 0.45 aspect, as a person actually does --
    # a fixed width would change the aspect ratio several-fold and be mistaken
    # for the detector drawing two different extents of a stationary object.
    out = None
    for step in range(10):
        height = 0.04 + step * 0.02
        width = 0.45 * height
        out = ot.update_object_tracks(
            cam, [_det('person', 0.50 - width / 2, 0.60 - height / 2, width, height)])
    with st._object_tracks_lock:
        assert len(st._object_tracks[cam]['tracks']) == 1  # stayed one track
    assert out[0]['track_displacement'] is not None
    assert out[0]['track_displacement'] > ot.TRACK_STILL_DISPLACEMENT


def test_approaching_subject_is_classified_moving_despite_a_quiet_mask():
    """End-to-end: the tracker's scale-aware displacement must flip the
    still/moving verdict for an approaching walker even when the pixel mask
    reports no change inside the (correctly parked) box center."""
    cam = 'trk-disp-approach-state'
    _reset(cam)
    out = None
    for step in range(10):
        height = 0.04 + step * 0.02
        width = 0.45 * height
        out = ot.update_object_tracks(
            cam, [_det('person', 0.50 - width / 2, 0.60 - height / 2, width, height)])
    quiet = np.zeros((72, 128), dtype=bool)
    assert os_.detection_motion_state(out[0], quiet, out[0]['track_displacement']) == os_.MODE_MOVING


def test_jittery_stationary_box_stays_still_despite_scale_wobble():
    """The parked-but-wobbly car must not be rescued into *moving* by the new
    scale term: alternating +/- ~0.5%-of-frame edge noise around a fixed size
    cancels in the halves' means and must stay under the still threshold."""
    cam = 'trk-disp-sizejitter'
    _reset(cam)
    wobble = [0.002, -0.002, 0.0015, -0.0025, 0.002, -0.0015, 0.0025, -0.002]
    out = None
    for dw in wobble:
        # Wobble the WIDTH, which is the larger side -- the measured size is
        # max(w, h), so wobbling a smaller side would leave the measure constant
        # and make this test pass without exercising the scale term at all.
        out = ot.update_object_tracks(cam, [_det('car', 0.40, 0.40, 0.25 + dw, 0.15)])
    assert out[0]['track_displacement'] is not None
    assert out[0]['track_displacement'] <= ot.TRACK_STILL_DISPLACEMENT


def test_scale_noise_rejection_is_independent_of_box_size():
    """Regression guard for a size-dependent false positive.

    Detector edge jitter is proportional to the box, so a parked box half the
    frame wide has several times the absolute size wobble of a small one. With
    a fixed absolute threshold that pushes a large parked car over the still
    threshold while a small one stays under -- i.e. the false-positive risk
    concentrated on exactly the large-box case the displacement override exists
    to protect (see app/object_settings.py:18). Requiring a RELATIVE change
    makes both equally immune.

    The relative wobble here (4% of the box) is well above the 1.5-3% measured
    on a real detector's parked subject and still far below the ~13% of a real
    head-on approach.
    """
    for side, label in ((0.10, 'small'), (0.55, 'large')):
        cam = f'trk-disp-jitter-{label}'
        _reset(cam)
        # Drift +2% of the box for the first half of the window, -2% for the
        # second: a 4% half-to-half difference that is pure detector drift, not
        # motion. Centres are exactly parked, so only the scale term can fire.
        out = None
        for sign in (1, 1, 1, 1, -1, -1, -1, -1):
            size = side * (1 + 0.02 * sign)
            out = ot.update_object_tracks(cam, [_det('car', 0.50 - size / 2, 0.50 - size / 2, size, size)])
        assert out[0]['track_displacement'] is not None
        assert out[0]['track_displacement'] <= ot.TRACK_STILL_DISPLACEMENT, (
            f'{label} box (side {side}) read as moving on detector drift alone'
        )


def test_real_depth_axis_growth_survives_the_jitter_gate():
    """The jitter gate must not swallow the signal it was added around: a real
    head-on approach is ~13% relative growth per half window, an order of
    magnitude above the 5% jitter band."""
    cam = 'trk-disp-gate'
    _reset(cam)
    out = None
    for step in range(8):
        distance = 8.0 - step * (1.6 / 7)
        height = 1.02 / distance
        out = ot.update_object_tracks(cam, [_det('person', 0.445, 0.72 - height, 0.11, height)])
    assert out[0]['track_displacement'] > ot.TRACK_STILL_DISPLACEMENT


def test_approaching_subject_size_annotations_are_exposed():
    """The age-2 classifier path reads track_box/track_prev_box, so the
    tracker must stamp them on every detection, new tracks included."""
    cam = 'trk-disp-sizeannot'
    _reset(cam)
    first = ot.update_object_tracks(cam, [_det('person', 0.50, 0.58, 0.04, 0.04)])[0]
    assert tuple(first['track_box']) == pytest.approx((0.50, 0.58, 0.04, 0.04))
    assert first['track_prev_box'] is None
    second = ot.update_object_tracks(cam, [_det('person', 0.50, 0.57, 0.06, 0.06)])[0]
    assert tuple(second['track_box']) == pytest.approx((0.50, 0.57, 0.06, 0.06))
    assert tuple(second['track_prev_box']) == pytest.approx((0.50, 0.58, 0.04, 0.04))
    # The box grew 0.02 on a mean size of 0.05 -- 40% relative, far above the
    # 5% jitter band. The step is reported in the units ``_TRACK_STEP_MOVING``
    # is expressed in, so 40% of jitter-band reference == 0.24, and it clears
    # the age-2 threshold regardless of the box's absolute size.
    assert os_._two_point_step(second) == pytest.approx(0.24, abs=1e-9)
    assert os_._two_point_step(second) >= os_._TRACK_STEP_MOVING


def test_approaching_person_survives_the_age_two_bias():
    """A walker toward the camera on its SECOND sighting: the box has not
    translated, so the age-2 still bias used to swallow it under a Moving Only
    rule before the windowed displacement ever became available."""
    cam = 'trk-disp-approach-age2'
    _reset(cam)
    quiet = np.zeros((72, 128), dtype=bool)
    # Already close to the lens, so one cycle of approach is a real scale step
    # while both sightings still overlap (IoU 0.78 -> one track, not two).
    ot.update_object_tracks(cam, [_det('person', 0.35, 0.35, 0.30, 0.30)])
    second = ot.update_object_tracks(cam, [_det('person', 0.33, 0.33, 0.34, 0.34)])[0]
    assert second['track_age'] == 2
    assert second['track_displacement'] is None  # window not mature yet
    assert os_.detection_motion_state(second, quiet, None) == os_.MODE_MOVING


def test_malformed_box_history_degrades_gracefully():
    """A hand-built or half-migrated track with junk in its box history must not
    crash, and must not let a malformed entry contribute a bogus measurement."""
    cam = 'trk-disp-mismatch'
    _reset(cam)
    for _ in range(6):
        ot.update_object_tracks(cam, [_det('car', 0.50, 0.50, 0.20, 0.20)])
    track = st._object_tracks[cam]['tracks'][0]
    assert len(track['boxes']) == len(track['centers'])
    # Corrupt the history the way a hand-built track would.
    track['boxes'] = [None, 'junk', (0.5, 0.5, 0.2, 0.2)] + track['boxes'][:4]
    value = ot._recent_displacement(track)
    assert value is not None
    assert value <= ot.TRACK_STILL_DISPLACEMENT
    # A track with no box history at all simply reports no evidence.
    track['boxes'] = []
    assert ot._recent_displacement(track) is None


# ---------------------------------------------------------------------------
# Extent instability: the same object drawn at two different sizes
# ---------------------------------------------------------------------------

# These are the REAL boxes from event 47720 (Driveway, 04-10-2026 21:31),
# recovered from the recording's .track.json. A parked car was drawn whole in
# one cycle and as a roof-only rectangle in the next. The two boxes are 2.4x
# apart in aspect ratio and 6.1x in area, with centres 0.178 of the frame
# apart, and the car never moved. Both measurements read that as vigorous
# motion, so a Moving Only "car" rule kept it.

CAR_WHOLE = (0.200, 0.212, 0.506, 0.429)
CAR_ROOF = (0.374, 0.212, 0.319, 0.111)


def _as_det(box):
    return {'label': 'car', 'confidence': 0.9,
            'box': {'x': box[0], 'y': box[1], 'width': box[2], 'height': box[3]}}


def test_extent_instability_signature_is_recognised():
    assert ot._extent_unstable(CAR_ROOF, CAR_WHOLE) is True
    # A genuinely translating object separates its boxes -> not the signature.
    assert ot._extent_unstable((0.10, 0.40, 0.20, 0.12), (0.60, 0.40, 0.20, 0.12)) is False
    # A subject approaching the camera scales uniformly -> stable aspect.
    assert ot._extent_unstable((0.40, 0.40, 0.11, 0.20), (0.35, 0.30, 0.20, 0.36)) is False


def test_parked_car_drawn_at_two_extents_reads_still():
    """The age-2 path must not call a parked car moving just because the
    detector drew it whole one cycle and as a roof the next."""
    cam = 'trk-extent-age2'
    _reset(cam)
    ot.update_object_tracks(cam, [_as_det(CAR_WHOLE)])
    second = ot.update_object_tracks(cam, [_as_det(CAR_ROOF)])[0]
    assert second['track_age'] == 2
    assert os_._two_point_step(second) == 0.0
    # Production reaches the age-2 branch with NO displacement yet, so the mask
    # is the only other signal. Make the mask say MOVING -- a passing car's
    # headlights sweep changed pixels right across this large box -- and check
    # the override still wins. (A quiet mask would return 'still' regardless and
    # prove nothing.)
    assert second['track_displacement'] is None
    noisy = np.zeros((72, 128), dtype=bool)
    noisy[15:24, 48:89] = True          # the roof box's own region
    assert os_.detection_motion_state(second, noisy, None) == os_.MODE_STILL


def test_extent_instability_does_not_fragment_the_track():
    """The two boxes differ 6.1x in area, which used to fail
    ``MOTION_MATCH_AREA_RATIO`` (2.5) and mint a fresh track id. A fresh id
    means a young track, and a young track never reaches the displacement
    override, so the pixel mask decides alone -- the actual failure chain on
    event 47720. Extent instability is the same object, so it must stay one
    track."""
    cam = 'trk-extent-frag'
    _reset(cam)
    first = ot.update_object_tracks(cam, [_as_det(CAR_WHOLE)])[0]
    second = ot.update_object_tracks(cam, [_as_det(CAR_ROOF)])[0]
    assert second['track_id'] == first['track_id'], (
        'extent change minted a new track id, which strands the detection in '
        'the mask-only young-track path'
    )
    assert second['track_age'] == 2


def test_genuinely_different_objects_are_still_kept_apart():
    """The area-ratio bypass must not merge two real cars. Two same-label
    boxes that separate in space still fail the containment test and keep
    their own tracks."""
    cam = 'trk-extent-two'
    _reset(cam)
    # Two same-label boxes: 9.4x apart in area (so the ratio check is live)
    # and close enough that the distance gate PASSES (0.252 vs gate 0.45), but
    # they do not overlap at all. Only the containment test keeps them apart.
    ot.update_object_tracks(cam, [_as_det((0.10, 0.30, 0.30, 0.25))])
    out = ot.update_object_tracks(cam, [_as_det((0.45, 0.35, 0.10, 0.08))])
    with st._object_tracks_lock:
        ids = [t['id'] for t in st._object_tracks[cam]['tracks']]
    assert len(ids) == 2, 'two separate cars were merged into one track'
    assert out[0]['track_new'] is True


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
