"""Tests for per-label still/moving object detection settings
(app/object_settings.py)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import app.object_settings as os  # noqa: E402
import app.state as _state  # noqa: E402


def _det(label, x=0.3, y=0.3, w=0.2, h=0.2, conf=0.9):
    return {'label': label, 'confidence': conf, 'box': {'x': x, 'y': y, 'width': w, 'height': h}}


def _mask_all_changed():
    return np.ones((240, 320), dtype=bool)


def _mask_none_changed():
    return np.zeros((240, 320), dtype=bool)


def _mask_changed_inside_box():
    mask = np.zeros((240, 320), dtype=bool)
    # Box is [0.3, 0.3, 0.2, 0.2] -> thumbnail rows 72..119, cols 96..159.
    mask[72:120, 96:160] = True
    return mask


class _FakeDatabase:
    def __init__(self, setting=None):
        self.setting = setting

    def get_setting(self, key):
        return self.setting if key == 'objects' else None


# ---------------------------------------------------------------------------
# normalize_object_settings
# ---------------------------------------------------------------------------


def test_normalize_object_settings_defaults():
    defaults = {'default_mode': 'moving', 'labels': {}, 'group_modes': {}, 'still_alerts': {}}
    assert os.normalize_object_settings(None) == defaults
    assert os.normalize_object_settings({}) == defaults
    assert os.normalize_object_settings('junk') == defaults
    assert os.normalize_object_settings([]) == defaults


def test_normalize_object_settings_round_trip():
    raw = {'default_mode': 'moving', 'labels': {'person': 'still', 'car': 'still'}, 'group_modes': {}, 'still_alerts': {}}
    assert os.normalize_object_settings(raw) == raw


def test_normalize_object_settings_coerces_invalid_modes():
    out = os.normalize_object_settings({
        'default_mode': 'sometimes',
        'labels': {'person': 'MOVING', 'car': 'bogus', 'bird': ''},
    })
    # Invalid default falls back to 'moving'; uppercase mode is normalised and
    # kept (an explicit valid override, even one equal to the default, is
    # preserved so it can override a covering group); bogus/empty modes are
    # dropped entirely so they never become a spurious override.
    assert out['default_mode'] == 'moving'
    assert out['labels'] == {'person': 'moving'}


def test_normalize_object_settings_keeps_explicit_default_override():
    # An explicit override equal to the default is NOT dropped: with group
    # modes between the per-label and default layers, it is meaningful (it
    # overrides a covering group's non-default mode back to the default value).
    out = os.normalize_object_settings({
        'default_mode': 'moving',
        'labels': {'person': 'moving', 'car': 'still'},
    })
    assert out['labels'] == {'person': 'moving', 'car': 'still'}


def test_normalize_object_settings_canonicalizes_labels():
    out = os.normalize_object_settings({'labels': {'Human': 'still', 'cat': 'moving'}})
    # Human canonicalizes to person; cat: moving is an explicit valid override
    # and is kept even though it equals the default.
    assert out['labels'] == {'person': 'still', 'cat': 'moving'}


def test_normalize_object_settings_group_modes():
    out = os.normalize_object_settings({
        'default_mode': 'moving',
        'group_modes': {'Animal': 'still', 'pet': 'moving', 'nope': 'bogus'},
    })
    # Group names canonicalize to lowercase; 'pet': 'moving' is kept even though
    # it equals the default (a more specific group overriding a broader one back
    # to the default is the point of overlapping groups); 'nope' is an invalid
    # mode so it is dropped.
    assert out['group_modes'] == {'animal': 'still', 'pet': 'moving'}


# ---------------------------------------------------------------------------
# effective_object_settings
# ---------------------------------------------------------------------------


def test_effective_object_settings_reads_database(monkeypatch):
    previous = _state.database
    try:
        _state.database = _FakeDatabase({'default_mode': 'moving', 'labels': {'car': 'still'}})
        effective = os.effective_object_settings()
        assert effective['default_mode'] == 'moving'
        assert effective['labels'] == {'car': 'still'}
    finally:
        _state.database = previous


def test_effective_object_settings_defaults_without_database(monkeypatch):
    previous = _state.database
    try:
        _state.database = None
        assert os.effective_object_settings() == {'default_mode': 'moving', 'labels': {}, 'group_modes': {}, 'still_alerts': {}}
    finally:
        _state.database = previous


# ---------------------------------------------------------------------------
# motion_mode_for_label
# ---------------------------------------------------------------------------


def test_motion_mode_for_label_resolution():
    settings = {'default_mode': 'moving', 'labels': {'car': 'still'}}
    assert os.motion_mode_for_label('person', settings) == 'moving'
    assert os.motion_mode_for_label('car', settings) == 'still'
    assert os.motion_mode_for_label('Human', settings) == 'moving'  # alias -> no override


def test_motion_mode_for_label_defaults_to_moving():
    assert os.motion_mode_for_label('person', {'default_mode': 'bogus', 'labels': {}}) == 'moving'


def test_motion_mode_for_label_group_mode_applies():
    settings = {'default_mode': 'moving', 'labels': {}, 'group_modes': {'animal': 'still'}}
    assert os.motion_mode_for_label('cat', settings) == 'still'
    assert os.motion_mode_for_label('horse', settings) == 'still'
    assert os.motion_mode_for_label('person', settings) == 'moving'  # not an animal


def test_motion_mode_for_label_most_specific_group_wins():
    settings = {'default_mode': 'moving', 'labels': {}, 'group_modes': {'animal': 'still', 'pet': 'moving'}}
    # cat is in both animal (10 members) and pet (3 members): the smaller pet
    # umbrella is more specific and wins.
    assert os.motion_mode_for_label('cat', settings) == 'moving'
    # horse is only in animal.
    assert os.motion_mode_for_label('horse', settings) == 'still'


def test_motion_mode_for_label_per_label_override_beats_group():
    settings = {'default_mode': 'moving', 'labels': {'cat': 'any'}, 'group_modes': {'animal': 'still'}}
    assert os.motion_mode_for_label('cat', settings) == 'any'
    assert os.motion_mode_for_label('dog', settings) == 'still'


def test_specific_group_overrides_broader_group_through_normalize():
    # Regression: a more specific group set back to the default value must
    # survive normalization and win at resolution. default=moving, animal=still,
    # pet=moving -> pets stay moving while other animals go still. Previously
    # pet=moving was collapsed as "redundant", flipping cats/dogs to still.
    settings = os.normalize_object_settings({
        'default_mode': 'moving',
        'group_modes': {'animal': 'still', 'pet': 'moving'},
    })
    assert settings['group_modes'] == {'animal': 'still', 'pet': 'moving'}
    assert os.motion_mode_for_label('cat', settings) == 'moving'   # pet wins (smaller)
    assert os.motion_mode_for_label('dog', settings) == 'moving'   # pet wins
    assert os.motion_mode_for_label('horse', settings) == 'still'  # only animal


# ---------------------------------------------------------------------------
# detection_motion_state
# ---------------------------------------------------------------------------


def test_detection_motion_state_moving():
    assert os.detection_motion_state(_det('person'), _mask_changed_inside_box()) == 'moving'


def test_detection_motion_state_still():
    assert os.detection_motion_state(_det('person'), _mask_none_changed()) == 'still'


def test_detection_motion_state_no_mask_means_still():
    assert os.detection_motion_state(_det('person'), None) == 'still'


def test_detection_motion_state_ignores_tiny_noise():
    # A single changed pixel inside a large box must not count as moving.
    mask = np.zeros((240, 320), dtype=bool)
    mask[100, 120] = True
    assert os.detection_motion_state(_det('person'), mask) == 'still'


def test_detection_motion_state_degenerate_box():
    det = {'label': 'person', 'box': {'x': 0.0, 'y': 0.0, 'width': 0.0, 'height': 0.0}}
    assert os.detection_motion_state(det, _mask_all_changed()) == 'still'
    assert os.detection_motion_state({'label': 'person'}, _mask_all_changed()) == 'still'


def test_detection_motion_state_box_off_mask_edges():
    # Box near the right/bottom edge must clamp without raising.
    det = _det('person', x=0.9, y=0.9, w=0.2, h=0.2)
    assert os.detection_motion_state(det, _mask_all_changed()) == 'moving'
    assert os.detection_motion_state(det, _mask_none_changed()) == 'still'


# ---------------------------------------------------------------------------
# filter_detections_by_motion_mode
# ---------------------------------------------------------------------------


def test_filter_any_mode_keeps_everything_but_annotates_motion_state():
    detections = [_det('person'), _det('car')]
    out = os.filter_detections_by_motion_mode(
        detections, _mask_all_changed(),
        {'default_mode': 'any', 'labels': {}},
    )
    # Every detection survives, but each now carries its classification so the
    # live view / timeline can tag it (previously the list passed through
    # unannotated when nothing was restricted).
    assert [d['label'] for d in out] == ['person', 'car']
    assert all(d['motion_state'] == 'moving' for d in out)


def test_filter_any_mode_with_no_mask_annotates_still():
    detections = [_det('person'), _det('car')]
    out = os.filter_detections_by_motion_mode(
        detections, None,
        {'default_mode': 'any', 'labels': {}},
    )
    assert [d['label'] for d in out] == ['person', 'car']
    assert all(d['motion_state'] == 'still' for d in out)


def test_filter_annotates_actual_state_for_unrestricted_labels():
    # A 'still' override for car must not force every other label's annotation
    # to 'any' -- unrestricted labels still get their real moving/still state.
    settings = {'default_mode': 'any', 'labels': {'car': 'still'}}
    moving_person = _det('person', x=0.3, y=0.3, w=0.2, h=0.2)
    out = os.filter_detections_by_motion_mode(
        [moving_person], _mask_changed_inside_box(), settings,
    )
    assert len(out) == 1
    assert out[0]['motion_state'] == 'moving'


def test_filter_moving_only_keeps_moving_drops_still():
    settings = {'default_mode': 'any', 'labels': {'car': 'moving'}}
    moving = _det('car', x=0.3, y=0.3, w=0.2, h=0.2)
    still = _det('car', x=0.6, y=0.6, w=0.2, h=0.2)
    out = os.filter_detections_by_motion_mode(
        [moving, still], _mask_changed_inside_box(), settings,
    )
    assert len(out) == 1
    assert out[0]['motion_state'] == 'moving'
    assert out[0]['box'] == moving['box']


def test_filter_still_only_keeps_still_drops_moving():
    settings = {'default_mode': 'any', 'labels': {'car': 'still'}}
    moving = _det('car', x=0.3, y=0.3, w=0.2, h=0.2)
    still = _det('car', x=0.6, y=0.6, w=0.2, h=0.2)
    out = os.filter_detections_by_motion_mode(
        [moving, still], _mask_changed_inside_box(), settings,
    )
    assert len(out) == 1
    assert out[0]['motion_state'] == 'still'


def test_filter_mixed_labels():
    settings = {'default_mode': 'any', 'labels': {'car': 'moving', 'person': 'still'}}
    moving_car = _det('car', x=0.3, y=0.3, w=0.2, h=0.2)
    still_person = _det('person', x=0.6, y=0.6, w=0.2, h=0.2)
    still_car = _det('car', x=0.6, y=0.6, w=0.2, h=0.2)
    out = os.filter_detections_by_motion_mode(
        [moving_car, still_person, still_car], _mask_changed_inside_box(), settings,
    )
    assert [d['label'] for d in out] == ['car', 'person']


def test_filter_respects_group_mode():
    settings = {'default_mode': 'any', 'labels': {}, 'group_modes': {'pet': 'moving'}}
    moving = _det('cat', x=0.3, y=0.3, w=0.2, h=0.2)
    still = _det('cat', x=0.6, y=0.6, w=0.2, h=0.2)
    out = os.filter_detections_by_motion_mode(
        [moving, still], _mask_changed_inside_box(), settings,
    )
    assert len(out) == 1
    assert out[0]['motion_state'] == 'moving'
    assert out[0]['box'] == moving['box']


def test_filter_no_mask_classifies_still():
    settings = {'default_mode': 'any', 'labels': {'car': 'moving'}}
    out = os.filter_detections_by_motion_mode([_det('car')], None, settings)
    assert out == []


def test_filter_empty_detections():
    assert os.filter_detections_by_motion_mode([], None, {'default_mode': 'moving', 'labels': {}}) == []


# ---------------------------------------------------------------------------
# track-displacement override (detection_motion_state 3rd arg)
# ---------------------------------------------------------------------------


def test_displacement_override_marks_parked_car_still():
    # Regression for the parked-car flap: a large stationary box full of
    # background change (mask says moving) must read still when the tracker's
    # net box displacement is ~zero.
    parked = _det('car', x=0.55, y=0.25, w=0.3, h=0.2)
    assert os.detection_motion_state(parked, _mask_all_changed(), 0.0) == 'still'
    assert os.detection_motion_state(parked, _mask_changed_inside_box(), 0.005) == 'still'


def test_displacement_threshold_boundary_counts_as_still():
    parked = _det('car')
    # Exactly at the threshold is still (<=); just over it is moving.
    assert os.detection_motion_state(parked, _mask_all_changed(), os._TRACK_DISPLACEMENT_STILL) == 'still'
    assert os.detection_motion_state(parked, _mask_none_changed(), os._TRACK_DISPLACEMENT_STILL + 0.001) == 'moving'


def test_displacement_override_marks_traversing_track_moving():
    # A track that has swept the frame is moving even with NO mask available
    # this cycle (periodic scan / motion-gate error would otherwise say still).
    car = _det('car', x=0.1, y=0.5, w=0.15, h=0.1)
    assert os.detection_motion_state(car, None, 0.3) == 'moving'
    assert os.detection_motion_state(car, _mask_none_changed(), 0.2) == 'moving'


def test_young_track_falls_back_to_mask_verdict():
    car = _det('car')  # no track_id -> an untracked detection
    # None displacement (too young) -> mask decides, exactly as before.
    assert os.detection_motion_state(car, _mask_changed_inside_box(), None) == 'moving'
    assert os.detection_motion_state(car, _mask_none_changed(), None) == 'still'
    assert os.detection_motion_state(car, None, None) == 'still'


def test_persistent_immature_track_biases_to_still():
    """A car that has PERSISTED a couple of cycles but is not yet old enough for
    a trustworthy displacement must NOT be called moving by the hyper-sensitive
    mask -- a parked car whose box catches a passing car's pixels would
    otherwise alert under Moving Only. It reads still through the maturity
    window, then the displacement override takes over."""
    persistent = {**_det('car'), 'track_id': 5, 'track_age': 2}
    # Mask says moving, but a persistent-yet-immature track is held still.
    assert os.detection_motion_state(persistent, _mask_all_changed(), None) == 'still'
    assert os.detection_motion_state(persistent, _mask_changed_inside_box(), None) == 'still'


def test_brand_new_track_still_uses_mask_so_fast_cars_are_not_lost():
    """A brand-new (age 1) detection keeps the mask verdict. This is critical:
    a fast car moves too far to IoU-match its own prior box, so it opens a new
    age-1 track every cycle; biasing age 1 to still would drop it under Moving
    Only forever. Its box is full of changed pixels, so the mask reads moving."""
    fresh = {**_det('car'), 'track_id': 9, 'track_age': 1, 'track_new': True}
    assert os.detection_motion_state(fresh, _mask_all_changed(), None) == 'moving'
    # ...and a genuinely quiet brand-new box still reads still via the mask.
    assert os.detection_motion_state(fresh, _mask_none_changed(), None) == 'still'


def test_mature_tracked_detection_uses_mask_when_displacement_unavailable():
    """Once a track is old enough (>= MIN_AGE) but this cycle has no
    displacement value (e.g. rebuilt history after a restart), the mask verdict
    is trusted again -- the still bias is scoped to the maturity window."""
    mature = {**_det('car'), 'track_id': 5, 'track_age': os._TRACK_DISPLACEMENT_MIN_AGE}
    assert os.detection_motion_state(mature, _mask_changed_inside_box(), None) == 'moving'
    assert os.detection_motion_state(mature, _mask_none_changed(), None) == 'still'


def test_displacement_junk_value_falls_back_to_mask():
    car = _det('car')
    assert os.detection_motion_state(car, _mask_changed_inside_box(), 'junk') == 'moving'


def test_group_mode_applies_when_partial_settings_omit_label_map():
    assert os.motion_mode_for_label(
        'cat', {'default_mode': 'any', 'group_modes': {'animal': 'moving'}},
    ) == os.MODE_MOVING


def test_any_mode_allows_object_detection_during_camera_motion():
    detection = _det('car')
    assert os.object_detection_allowed_during_camera_motion(
        detection, {'default_mode': 'any', 'labels': {}},
    ) is True
    assert os.object_detection_allowed_during_camera_motion(
        detection, {'default_mode': 'moving', 'labels': {}},
    ) is False


def test_camera_motion_marks_object_state_unknown_without_dropping_detection():
    detection = {**_det('car'), 'track_displacement': 0.4}
    out = os.filter_detections_by_motion_mode(
        [detection], _mask_all_changed(),
        {'default_mode': 'moving', 'labels': {}},
        camera_motion=True,
    )
    assert len(out) == 1
    assert out[0]['motion_state'] == 'unknown'
    assert out[0]['camera_motion'] is True


def test_camera_motion_does_not_create_still_dwell_candidate():
    detection = _det('car')
    assert os.still_dwell_candidates(
        [detection], _mask_none_changed(),
        {'still_alerts': {'car': 5}}, camera_motion=True,
    ) == []


def test_filter_honours_track_displacement():
    # Moving-only cars: two boxes, both full of mask change, but one track is
    # parked (displacement ~0) and one is traversing. The parked one is
    # dropped by the override even though the mask alone would keep both.
    settings = {'default_mode': 'any', 'labels': {'car': 'moving'}}
    parked = {**_det('car', x=0.55, y=0.25, w=0.3, h=0.2), 'track_displacement': 0.0}
    traversing = {**_det('car', x=0.2, y=0.5, w=0.15, h=0.1), 'track_displacement': 0.25}
    out = os.filter_detections_by_motion_mode(
        [parked, traversing], _mask_all_changed(), settings,
    )
    assert [d['box'] for d in out] == [traversing['box']]
    assert out[0]['motion_state'] == 'moving'


def test_filter_fast_path_honours_displacement_without_mask():
    # No restricted labels would normally mean the no-mask fast path stamps
    # everything still; a traversing track must still read moving there.
    settings = {'default_mode': 'any', 'labels': {}}
    moving_person = {**_det('person'), 'track_displacement': 0.4}
    out = os.filter_detections_by_motion_mode([moving_person], None, settings)
    assert len(out) == 1
    assert out[0]['motion_state'] == 'moving'


def test_still_dwell_candidates_honour_displacement():
    # A parked car with a noisy mask still accrues its dwell streak because
    # the candidate picker classifies via the displacement override too.
    parked = {**_det('car', x=0.55, y=0.25, w=0.3, h=0.2), 'track_displacement': 0.0}
    candidates = os.still_dwell_candidates([parked], _mask_all_changed(), {'still_alerts': {'car': 5}})
    assert len(candidates) == 1
    assert candidates[0]['motion_state'] == 'still'


# ---------------------------------------------------------------------------
# still_alerts normalization + thresholds
# ---------------------------------------------------------------------------


def test_normalize_object_settings_still_alerts():
    out = os.normalize_object_settings({
        'default_mode': 'any',
        'still_alerts': {'package': 10, 'person': 0, 'cat': 0.5, 'car': 'bogus', 'Human': 3},
    })
    # 0 and sub-floor values are off; junk is dropped; labels are canonicalized.
    assert out['still_alerts'] == {'package': 10, 'person': 3}


def test_normalize_object_settings_still_alerts_caps():
    out = os.normalize_object_settings({'still_alerts': {'package': 999999}})
    assert out['still_alerts'] == {'package': 1440}


def test_still_alert_thresholds_filters_invalid():
    assert os.still_alert_thresholds({'still_alerts': {'package': 10, 'cat': 'junk', 'dog': 0}}) == {'package': 10}
    assert os.still_alert_thresholds({'still_alerts': None}) == {}
    assert os.still_alert_thresholds({}) == {}


# ---------------------------------------------------------------------------
# update_still_dwell_alerts
# ---------------------------------------------------------------------------


def _still_det(label, x=0.3, y=0.3, w=0.2, h=0.2, conf=0.9):
    return {'label': label, 'confidence': conf, 'box': {'x': x, 'y': y, 'width': w, 'height': h}, 'motion_state': 'still'}


def _moving_det(label, x=0.3, y=0.3, w=0.2, h=0.2, conf=0.9):
    return {'label': label, 'confidence': conf, 'box': {'x': x, 'y': y, 'width': w, 'height': h}, 'motion_state': 'moving'}


@pytest.fixture(autouse=True)
def _clear_still_dwell_state():
    """Every dwell test starts with a clean streak table."""
    yield
    with _state._still_dwell_lock:
        _state._still_dwell.clear()


def test_dwell_alert_fires_when_streak_crosses_threshold():
    # First cycle starts the streak; the crossing cycle emits the alert.
    out1 = os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0)
    assert out1 == []
    out2 = os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0 + 5 * 60)
    assert len(out2) == 1
    assert out2[0]['label'] == 'package'
    assert out2[0]['still_alert'] is True
    assert out2[0]['still_alert_minutes'] == 5
    assert out2[0]['motion_state'] == 'still'


def test_dwell_alert_does_not_refire_while_still():
    alerts = []
    for t in (1000.0, 1000.0 + 6 * 60, 1000.0 + 12 * 60, 1000.0 + 30 * 60):
        alerts.extend(os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=t))
    assert len(alerts) == 1  # fired once at 6 min, never again while still


def test_dwell_streak_resets_when_subject_moves():
    os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0)
    # Subject moves -> streak breaks.
    assert os.update_still_dwell_alerts('cam-1', [_moving_det('package')], {'package': 5}, now=1100.0) == []
    # A fresh still run restarts from zero and can alert again.
    assert os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1200.0) == []
    out = os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1200.0 + 5 * 60)
    assert len(out) == 1


def test_dwell_streak_resets_when_subject_absent():
    os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0)
    # A cycle without the label (or with nothing detected) breaks the streak.
    assert os.update_still_dwell_alerts('cam-1', [], {'package': 5}, now=1100.0) == []
    os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1200.0)
    out = os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1200.0 + 5 * 60)
    assert len(out) == 1


def test_dwell_ignores_labels_without_threshold_and_moving():
    # No threshold -> no tracking; moving detections never start a streak.
    out = os.update_still_dwell_alerts('cam-1', [_still_det('cat')], {'package': 5}, now=1000.0)
    assert out == []
    out = os.update_still_dwell_alerts('cam-1', [_moving_det('package')], {'package': 5}, now=1000.0)
    assert out == []
    with _state._still_dwell_lock:
        assert _state._still_dwell.get('cam-1') in (None, {})


def test_dwell_streaks_are_per_camera():
    os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0)
    assert os.update_still_dwell_alerts('cam-2', [_still_det('package')], {'package': 5}, now=1000.0 + 5 * 60) == []
    out = os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0 + 5 * 60)
    assert len(out) == 1


def test_dwell_noop_without_thresholds_or_detections():
    assert os.update_still_dwell_alerts('cam-1', [_still_det('package')], {}, now=1000.0) == []
    assert os.update_still_dwell_alerts('cam-1', [], {'package': 5}, now=1000.0) == []


def test_dwell_streak_resets_on_fully_empty_frame():
    # Regression: an empty detection list must still drop an existing streak
    # (subject left an otherwise-empty frame), not preserve it via an early
    # return. The dwell tracker is fed only the still-alert-label stills, so an
    # empty list is the normal "subject gone / moved" signal.
    os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0)
    # Frame empties for a while; the streak must not survive it.
    assert os.update_still_dwell_alerts('cam-1', [], {'package': 5}, now=1000.0 + 3 * 60) == []
    assert os.update_still_dwell_alerts('cam-1', [], {'package': 5}, now=1000.0 + 10 * 60) == []
    # A fresh still run restarts from zero: no early alert from the stale streak.
    assert os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0 + 12 * 60) == []
    out = os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0 + 17 * 60)
    assert len(out) == 1


def test_dwell_streaks_pause_during_camera_motion():
    """Regression: a PTZ nudge or auto-tracking pan produced empty still
    candidates, which hit the reset loop and wiped every long-dwell streak to
    zero. Camera motion is not evidence the subject moved -- pause instead."""
    os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0)
    # Camera moves for a while; streaks are paused, elapsed time keeps counting.
    assert os.update_still_dwell_alerts('cam-1', [], {'package': 5}, now=1000.0 + 2 * 60, camera_motion=True) == []
    assert os.update_still_dwell_alerts('cam-1', [], {'package': 5}, now=1000.0 + 4 * 60, camera_motion=True) == []
    # Camera settles; the streak resumes from its original still_since and
    # crosses its threshold without a fresh 5-minute wait.
    out = os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0 + 6 * 60)
    assert len(out) == 1
    assert out[0]['still_alert'] is True


def test_dwell_alert_cannot_cross_entirely_during_camera_motion():
    # The pause never completes a streak by itself: a dwell alert must be
    # emitted by a clear cycle that actually sees the subject still.
    os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0)
    # The whole threshold window elapses while the camera is moving.
    assert os.update_still_dwell_alerts('cam-1', [], {'package': 5}, now=1000.0 + 10 * 60, camera_motion=True) == []
    # First clear cycle after the motion: the still subject crosses now.
    out = os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0 + 11 * 60)
    assert len(out) == 1


def test_dwell_subject_lost_during_camera_motion_still_resets():
    # The pause is not amnesia: a subject that genuinely left (or moved) while
    # the camera was panning resets on the first clear cycle, exactly like the
    # no-motion path.
    os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0)
    assert os.update_still_dwell_alerts('cam-1', [], {'package': 5}, now=1000.0 + 60, camera_motion=True) == []
    # First clear cycle: subject gone -> streak breaks.
    assert os.update_still_dwell_alerts('cam-1', [], {'package': 5}, now=1000.0 + 120) == []
    # A fresh still run needs a full fresh 5 minutes: no early alert from the
    # pre-motion streak.
    os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0 + 180)
    assert os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0 + 300) == []
    out = os.update_still_dwell_alerts('cam-1', [_still_det('package')], {'package': 5}, now=1000.0 + 480)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# still_dwell_candidates
# ---------------------------------------------------------------------------


def test_still_dwell_candidates_selects_still_alert_labels_regardless_of_mode():
    # Default "moving" mode would drop still detections in the main filter, but
    # a label with a still-alert threshold must still reach the dwell tracker.
    settings = {'default_mode': 'moving', 'labels': {}, 'still_alerts': {'package': 5}}
    dets = [_det('package'), _det('car')]
    out = os.still_dwell_candidates(dets, _mask_none_changed(), settings)
    assert [d['label'] for d in out] == ['package']  # 'car' has no threshold
    assert out[0]['motion_state'] == 'still'


def test_still_dwell_candidates_excludes_moving_subjects():
    # A subject whose pixels are changing is 'moving' and must not appear, so
    # the tracker treats it as a streak break.
    settings = {'default_mode': 'moving', 'labels': {}, 'still_alerts': {'package': 5}}
    out = os.still_dwell_candidates([_det('package')], _mask_changed_inside_box(), settings)
    assert out == []


def test_still_dwell_candidates_empty_without_thresholds():
    settings = {'default_mode': 'moving', 'labels': {}, 'still_alerts': {}}
    assert os.still_dwell_candidates([_det('package')], _mask_none_changed(), settings) == []
    assert os.still_dwell_candidates([], _mask_none_changed(), settings) == []
