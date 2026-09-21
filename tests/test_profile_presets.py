from __future__ import annotations

import pytest

from app.profile_presets import (
    BUILTIN_PRESETS,
    create_preset,
    custom_presets,
    get_preset,
    list_presets,
    normalize_preset,
)


def test_builtin_presets_have_day_and_night_values():
    presets = list_presets(None)
    assert len(presets) == len(BUILTIN_PRESETS)
    assert {preset['id'] for preset in presets} == {
        'cat-small-animal', 'balanced', 'maximum-recall', 'low-cpu', 'night-ir', 'fast-motion',
    }
    cat = get_preset(None, 'cat-small-animal')
    assert cat is not None
    assert cat['builtin'] is True
    assert cat['day']['object_detection_region_boost'] is True
    # Daytime tiling is off so a CPU host can sustain the fast cadence; region
    # boost still recovers small moving cats. Night keeps full-frame tiling.
    assert cat['day']['object_detection_tiling'] == 'off'
    assert cat['night']['object_detection_tiling'] == '3x3'
    assert cat['day']['detection_confirm_frames'] == 2
    assert cat['day']['detection_confirm_window'] == 3
    # Spatial IoU kept low (day matches night) so a small/distant moving cat is
    # not dropped for failing to overlap its own box across cycles.
    assert cat['day']['detection_confirm_iou'] == 0.05
    assert cat['night']['detection_confirm_frames'] == 2
    assert cat['night']['detection_confirm_window'] == 3
    assert cat['night']['detection_confirm_iou'] == 0.05
    # Cats sit still constantly: both profiles count still detections so a
    # settled cat is not dropped by the global Moving Only default.
    assert cat['day']['object_detection_motion_mode'] == 'any'
    assert cat['night']['object_detection_motion_mode'] == 'any'


def test_recall_profiles_do_not_add_confirmation_latency():
    """Built-in recall profiles should alert on the first confident detection.

    Low CPU intentionally remains confirmation-heavy because it trades latency
    for reduced inference work and noise resistance.
    """
    for preset in list_presets(None):
        if preset['id'] in {'low-cpu', 'cat-small-animal'}:
            continue
        assert preset['day']['detection_confirm_frames'] == 1
        assert preset['day']['detection_confirm_window'] == 1
        assert preset['night']['detection_confirm_frames'] == 1
        assert preset['night']['detection_confirm_window'] == 1


def test_create_preset_slugifies_and_avoids_duplicate_ids():
    existing = list_presets(None)
    first = create_preset({'name': 'Porch Cat!', 'day': {'detection_interval_seconds': 0.5}}, existing)
    second = create_preset({'name': 'Porch Cat!', 'day': {'detection_interval_seconds': 0.4}}, existing + [first])
    assert first['id'] == 'porch-cat'
    assert second['id'] == 'porch-cat-2'
    assert first['day']['detection_interval_seconds'] == 0.5
    assert first['night'] == {}


def test_custom_presets_exclude_builtins_and_invalid_entries():
    raw = [
        {'id': 'cat-small-animal', 'name': 'Spoofed built-in', 'builtin': True},
        {'id': 'my-preset', 'name': 'My Preset', 'day': {'motion_pixel_threshold': 55}},
        {'id': 'bad', 'name': '', 'day': {}},
    ]
    custom = custom_presets(raw)
    assert [preset['id'] for preset in custom] == ['my-preset']
    assert list_presets(raw)[0]['name'] == 'Cat / Small Animal'


def test_normalize_preset_rejects_bad_names_and_ids():
    with pytest.raises(ValueError, match='Preset name'):
        normalize_preset({'id': 'valid-id', 'name': '', 'day': {}})
    with pytest.raises(ValueError, match='Preset id'):
        normalize_preset({'id': 'Not Valid!', 'name': 'Preset', 'day': {}})
