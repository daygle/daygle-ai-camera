"""Plain-language motion settings: one trigger per zone, "Must last: N
checks", and the per-zone live meter.

A zone's trigger ("trigger when 2% of this zone moves") replaces the old gate /
scale / Sensitivity trio. Zones saved before it existed must keep firing at
exactly the same point, and the live meter must report each zone in the same
units as its trigger, with a Quiet / Moving / Triggered state and a ten-minute
peak.
"""

from __future__ import annotations

import numpy as np

import app.state as state
from app.detection_state import confirm_motion_detections
from app.motion_levels import PEAK_WINDOW_SECONDS, forget_camera, motion_zone_levels
from app.zone_detection import motion_zone_confirm_cycles, zone_motion_detections
from app.zone_schema import (
    normalize_zone_object_rules,
    zone_motion_confirm_cycles,
    zone_motion_effective_trigger,
)

FRAME = (100, 100)  # (width, height) of the motion mask


def _zone(rule: dict, zone_id: str = 'drive') -> dict:
    return {
        'id': zone_id, 'name': zone_id.title(), 'enabled': True, 'monitor_motion': True,
        'x': 0, 'y': 0, 'width': 1, 'height': 1,
        'object_rules': [{'label': 'motion', 'enabled': True, **rule}],
    }


def _mask(share: float) -> np.ndarray:
    """A full-frame mask with ``share`` of its pixels changed."""
    mask = np.zeros((FRAME[1], FRAME[0]), dtype=bool)
    mask.flat[: int(round(share * mask.size))] = True
    return mask


def _detect(zones: list[dict], share: float, levels: list | None = None, **kwargs) -> list[dict]:
    return zone_motion_detections(
        {'detection': {'zones': zones}}, diff_mask=_mask(share), frame_size=FRAME,
        gate_fraction=kwargs.get('gate', 0.005), scale_fraction=kwargs.get('scale', 0.03), levels=levels,
    )


def test_normalised_trigger_opens_the_confidence_window():
    [rule] = normalize_zone_object_rules({'object_rules': [
        {'label': 'motion', 'min_confidence': 0.6, 'max_confidence': 0.8, 'trigger_fraction': 0.04, 'confirm_cycles': 9},
    ]})
    assert rule['trigger_fraction'] == 0.04
    # The trigger already gated the zone; the alert / record axes must not
    # re-gate on a scaled confidence the operator no longer sees.
    assert (rule['min_confidence'], rule['max_confidence']) == (0.0, 1.0)
    assert rule['confirm_cycles'] == 5, 'clamped to the maximum'

    [legacy] = normalize_zone_object_rules({'object_rules': [{'label': 'motion', 'min_confidence': 0.6}]})
    assert legacy['trigger_fraction'] is None
    assert legacy['min_confidence'] == 0.6, 'legacy rules keep their Sensitivity'
    assert legacy['confirm_cycles'] == 2, 'two checks was the old hard-coded rule'

    [person] = normalize_zone_object_rules({'object_rules': [{'label': 'person', 'trigger_fraction': 0.04}]})
    assert person['trigger_fraction'] is None and person['confirm_cycles'] is None


def test_legacy_zone_effective_trigger_matches_the_old_maths():
    zone = _zone({'min_confidence': 0.45})
    # max(gate, Sensitivity x scale) = max(0.005, 0.45 * 0.03) = 1.35%
    assert abs(zone_motion_effective_trigger(zone, 0.005, 0.03) - 0.0135) < 1e-9
    assert abs(zone_motion_effective_trigger(_zone({'min_confidence': 0.1}), 0.005, 0.03) - 0.005) < 1e-9
    assert zone_motion_effective_trigger(_zone({'trigger_fraction': 0.02}), 0.005, 0.03) == 0.02


def test_legacy_zone_fires_exactly_as_before():
    zone = _zone({'min_confidence': 0.45})  # effective trigger 1.35%
    assert _detect([zone], 0.012) == []
    assert [item['zone_id'] for item in _detect([zone], 0.014)] == ['drive']


def test_trigger_zone_fires_at_its_trigger_whatever_the_scale():
    # A 4% trigger is above the 3% scale, which the old Sensitivity (a share of
    # the scale) could never express.
    zone = _zone({'trigger_fraction': 0.04, 'min_confidence': 0.0})
    assert _detect([zone], 0.035) == []
    [detection] = _detect([zone], 0.045)
    assert detection['motion_fraction'] == 0.045
    sensitive = _zone({'trigger_fraction': 0.005, 'min_confidence': 0.0})
    assert _detect([sensitive], 0.006) != [], 'fires below the old 0.45 x scale floor'


def test_trigger_zone_without_a_mask_never_fires_on_a_zero_confidence_scan():
    settings = {'detection': {'zones': [_zone({'trigger_fraction': 0.01, 'min_confidence': 0.0})]}}
    assert zone_motion_detections(settings, 0.0, diff_mask=None) == []
    assert zone_motion_detections(settings, 0.9, diff_mask=None) != []


def test_levels_report_every_motion_zone_in_trigger_units():
    zones = [_zone({'trigger_fraction': 0.02}, 'drive'), _zone({'min_confidence': 0.45}, 'path')]
    levels: list = []
    _detect(zones, 0.004, levels=levels)
    assert levels == [
        {'zone_id': 'drive', 'zone_name': 'Drive', 'fraction': 0.004, 'trigger': 0.02},
        {'zone_id': 'path', 'zone_name': 'Path', 'fraction': 0.004, 'trigger': 0.0135},
    ]


def test_confirm_cycles_are_per_zone():
    camera = 'cam-confirm-cycles'
    with state._motion_confirm_lock:
        state._motion_confirm_streaks.pop(camera, None)
    quick = {'zone_id': 'quick'}
    slow = {'zone_id': 'slow'}
    required = {'quick': 1, 'slow': 3}
    assert confirm_motion_detections(camera, [quick, slow], required_by_zone=required) == [quick]
    assert confirm_motion_detections(camera, [quick, slow], required_by_zone=required) == [quick]
    assert confirm_motion_detections(camera, [quick, slow], required_by_zone=required) == [quick, slow]
    # A gap resets the streak.
    assert confirm_motion_detections(camera, [quick], required_by_zone=required) == [quick]
    assert confirm_motion_detections(camera, [quick, slow], required_by_zone=required) == [quick]


def test_confirm_cycles_read_from_zone_settings():
    zones = [_zone({'confirm_cycles': 3}, 'a'), _zone({}, 'b'), {**_zone({}, 'c'), 'enabled': False}]
    assert motion_zone_confirm_cycles({'detection': {'zones': zones}}) == {'a': 3, 'b': 2}
    assert zone_motion_confirm_cycles(_zone({'confirm_cycles': 'x'})) == 2


def test_meter_states_and_ten_minute_peak():
    camera = 'cam-levels'
    forget_camera(camera)
    level = {'zone_id': 'drive', 'zone_name': 'Drive', 'trigger': 0.02}

    def publish(fraction, now, triggered=False, streak=0):
        [entry] = motion_zone_levels(
            camera, [{**level, 'fraction': fraction}],
            triggered_zone_ids={'drive'} if triggered else set(),
            streaks={'drive': streak}, confirm_cycles={'drive': 2}, now=now,
        )
        return entry

    quiet = publish(0.0002, 1000.0)
    assert (quiet['state'], quiet['above_trigger']) == ('quiet', False)
    moving = publish(0.004, 1010.0)
    assert moving['state'] == 'moving' and not moving['above_trigger']
    waiting = publish(0.03, 1020.0, streak=1)
    assert waiting['state'] == 'moving' and waiting['above_trigger']
    assert (waiting['checks_seen'], waiting['checks_needed']) == (1, 2)
    fired = publish(0.03, 1030.0, triggered=True, streak=2)
    assert fired['state'] == 'triggered'
    assert fired['peak_fraction'] == 0.03
    assert fired['peak_window_seconds'] == PEAK_WINDOW_SECONDS
    # The peak ages out after the window.
    later = publish(0.001, 1030.0 + PEAK_WINDOW_SECONDS + 60)
    assert later['peak_fraction'] == 0.001
    # A zone missing from a check is forgotten.
    assert motion_zone_levels(camera, [], triggered_zone_ids=set(), now=2000.0) == []
    assert publish(0.0, 2001.0)['peak_fraction'] == 0.0
    forget_camera(camera)
