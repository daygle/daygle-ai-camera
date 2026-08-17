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
    # Invalid default falls back to 'moving'; uppercase mode is normalised;
    # bogus/empty modes are dropped entirely (never persisted). The
    # normalised 'person': 'moving' equals the new default, so it is dropped
    # as a redundant override.
    assert out['default_mode'] == 'moving'
    assert out['labels'] == {}


def test_normalize_object_settings_drops_redundant_override():
    out = os.normalize_object_settings({
        'default_mode': 'moving',
        'labels': {'person': 'moving', 'car': 'still'},
    })
    assert out['labels'] == {'car': 'still'}


def test_normalize_object_settings_canonicalizes_labels():
    out = os.normalize_object_settings({'labels': {'Human': 'still', 'cat': 'moving'}})
    # 'cat': 'moving' equals the new default, so only the canonicalised
    # 'person': 'still' override survives.
    assert out['labels'] == {'person': 'still'}


def test_normalize_object_settings_group_modes():
    out = os.normalize_object_settings({
        'default_mode': 'moving',
        'group_modes': {'Animal': 'still', 'pet': 'moving', 'nope': 'bogus'},
    })
    # Group names canonicalize to lowercase; 'pet': 'moving' equals the default
    # so it is dropped; 'nope' is not a valid mode so it is dropped too.
    assert out['group_modes'] == {'animal': 'still'}


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
