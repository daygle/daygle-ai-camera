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


def test_builtin_presets_are_separate_day_and_night_records():
    presets = list_presets(None)
    assert len(presets) == len(BUILTIN_PRESETS) == 12
    assert {preset['id'] for preset in presets} == {
        f'{group}-{mode}'
        for group in ('cat-small-animal', 'balanced', 'maximum-recall', 'low-cpu', 'night-ir', 'fast-motion')
        for mode in ('day', 'night')
    }
    assert {preset['mode'] for preset in presets} == {'day', 'night'}
    assert all(preset['settings'] for preset in presets)

    day = get_preset(None, 'cat-small-animal-day')
    night = get_preset(None, 'cat-small-animal-night')
    assert day is not None
    assert night is not None
    assert day['mode'] == 'day'
    assert night['mode'] == 'night'
    assert day['settings']['object_detection_region_boost'] is True
    assert day['settings']['object_detection_tiling'] == 'off'
    assert night['settings']['object_detection_tiling'] == '2x2'
    assert day['settings']['detection_confirm_frames'] == 2
    assert night['settings']['detection_confirm_frames'] == 2
    # Day presets let the shadow check decide per frame; IR night has no colour.
    assert all(
        preset['settings']['motion_shadow_suppression'] == ('off' if preset['mode'] == 'night' else 'auto')
        for preset in presets
    )


# Presets that chase short or fast events skip adaptive cadence: stretching the
# detection interval on a quiet scene would miss the first frames of a visit.
_NO_ADAPTIVE = {
    'cat-small-animal-night', 'maximum-recall-day', 'maximum-recall-night',
    'fast-motion-day', 'fast-motion-night',
}


def test_adaptive_cadence_is_off_only_for_short_event_presets():
    for preset in list_presets(None):
        expected = preset['id'] not in _NO_ADAPTIVE
        assert preset['settings']['adaptive_detection_enabled'] is expected, preset['id']


@pytest.mark.parametrize(('preset_id', 'second_look', 'low_light'), [
    ('cat-small-animal-day', True, None),
    ('cat-small-animal-night', True, 'auto'),
    ('maximum-recall-day', True, None),
    ('maximum-recall-night', True, 'auto'),
    ('night-ir-night', True, 'auto'),
    ('low-cpu-day', False, 'off'),
    ('low-cpu-night', False, 'off'),
    # Unset -> the camera follows the global default.
    ('balanced-day', None, None),
    ('balanced-night', None, None),
    ('fast-motion-day', None, None),
    ('fast-motion-night', None, None),
])
def test_second_look_and_low_light_per_preset(preset_id, second_look, low_light):
    settings = get_preset(None, preset_id)['settings']
    assert settings.get('object_detection_second_look') is second_look
    assert settings.get('object_detection_low_light') == low_light


def test_builtin_settings_survive_normalization():
    for preset in list_presets(None):
        normalized = normalize_preset({
            'id': 'copy-' + preset['id'], 'name': 'Copy', 'mode': preset['mode'], 'settings': preset['settings'],
        })['settings']
        for key in ('object_detection_second_look', 'object_detection_low_light',
                    'adaptive_detection_enabled', 'motion_shadow_suppression'):
            if key in preset['settings']:
                assert normalized[key] == preset['settings'][key], (preset['id'], key)


def test_recall_profiles_do_not_add_confirmation_latency():
    """Built-in recall profiles should alert on the first confident detection.

    Low CPU intentionally remains confirmation-heavy because it trades latency
    for reduced inference work and noise resistance.
    """
    for preset in list_presets(None):
        if preset['id'].startswith('low-cpu-') or preset['id'].startswith('cat-small-animal-'):
            continue
        assert preset['settings']['detection_confirm_frames'] == 1
        assert preset['settings']['detection_confirm_window'] == 1


def test_create_preset_is_mode_specific_and_avoids_duplicate_ids():
    existing = list_presets(None)
    first = create_preset({
        'name': 'Porch Cat!',
        'mode': 'day',
        'settings': {'detection_interval_seconds': 0.5},
    }, existing)
    second = create_preset({
        'name': 'Porch Cat!',
        'mode': 'day',
        'settings': {'detection_interval_seconds': 0.4},
    }, existing + [first])
    night = create_preset({
        'name': 'Porch Cat!',
        'mode': 'night',
        'settings': {'detection_interval_seconds': 0.3},
    }, existing + [first, second])

    assert first['id'] == 'porch-cat-day'
    assert second['id'] == 'porch-cat-day-2'
    assert night['id'] == 'porch-cat-night'
    assert first['mode'] == 'day'
    assert first['settings']['detection_interval_seconds'] == 0.5
    assert night['settings']['detection_interval_seconds'] == 0.3
    assert normalize_preset({
        'id': 'fixed-cadence-day',
        'name': 'Fixed Cadence',
        'mode': 'day',
        'settings': {'adaptive_detection_enabled': False},
    })['settings']['adaptive_detection_enabled'] is False


def test_legacy_combined_custom_presets_expand_into_modes():
    custom = custom_presets([{
        'id': 'porch-cat',
        'name': 'Porch Cat',
        'day': {'motion_pixel_threshold': 55},
        'night': {'motion_pixel_threshold': 80},
    }])
    assert [(preset['id'], preset['mode']) for preset in custom] == [
        ('porch-cat-day', 'day'),
        ('porch-cat-night', 'night'),
    ]
    assert custom[0]['settings']['motion_pixel_threshold'] == 55
    assert custom[1]['settings']['motion_pixel_threshold'] == 80


def test_custom_presets_exclude_builtins_and_invalid_entries():
    raw = [
        {'id': 'balanced-day', 'name': 'Spoofed built-in', 'builtin': True},
        {'id': 'my-preset-day', 'name': 'My Preset', 'mode': 'day', 'settings': {'motion_pixel_threshold': 55}},
        {'id': 'bad', 'name': '', 'mode': 'day', 'settings': {}},
    ]
    custom = custom_presets(raw)
    assert [preset['id'] for preset in custom] == ['my-preset-day']
    assert get_preset(raw, 'balanced-day')['name'] == 'Balanced (Day)'


def test_normalize_preset_rejects_bad_names_ids_and_modes():
    with pytest.raises(ValueError, match='Preset name'):
        normalize_preset({'id': 'valid-id', 'name': '', 'mode': 'day', 'settings': {}})
    with pytest.raises(ValueError, match='Preset id'):
        normalize_preset({'id': 'Not Valid!', 'name': 'Preset', 'mode': 'day', 'settings': {}})
    with pytest.raises(ValueError, match='Preset mode'):
        normalize_preset({'id': 'valid-id', 'name': 'Preset', 'mode': 'evening', 'settings': {}})


def test_presets_never_carry_legacy_per_camera_motion_overrides():
    """These moved to the global Advanced Motion Engine; a preset that carried
    them brought the hidden per-camera override back when applied."""
    from app.profile_presets import LEGACY_MOTION_OVERRIDE_KEYS
    for preset in list_presets(None):
        assert not LEGACY_MOTION_OVERRIDE_KEYS & set(preset['settings']), preset['id']
    saved = normalize_preset({
        'id': 'porch-day', 'name': 'Porch', 'mode': 'day',
        'settings': {'motion_pixel_threshold': 40, 'motion_gate_fraction': 0.002, 'motion_algorithm': 'diff'},
    })['settings']
    assert saved['motion_pixel_threshold'] == 40
    assert not LEGACY_MOTION_OVERRIDE_KEYS & set(saved)


# ---------------------------------------------------------------------------
# Linked camera profiles
# ---------------------------------------------------------------------------

from app.profile_presets import (  # noqa: E402
    cameras_using_profile,
    global_default_profile_id,
    link_camera_profiles,
    migrate_camera_profile_links,
    profile_settings,
)


def _camera(name, day=None, night=None, **ids):
    return {'id': name.lower(), 'name': name, 'detection_profiles': {
        'active': 'day', 'day': day or {}, 'night': night or {}, **ids,
    }}


def test_linking_copies_the_profile_and_drops_legacy_camera_overrides():
    custom = create_preset({'name': 'Porch', 'mode': 'night', 'settings': {'detection_interval_seconds': 0.4}}, list_presets(None))
    camera = _camera('Porch', night={'detection_interval_seconds': 2, 'motion_gate_fraction': 0.002},
                     day_preset_id='cat-small-animal-day', night_preset_id=custom['id'])
    [linked], changed = link_camera_profiles([camera], [custom])
    assert changed
    profiles = linked['detection_profiles']
    assert profiles['day'] == profile_settings(None, 'cat-small-animal-day', 'day')
    assert profiles['night'] == {'detection_interval_seconds': 0.4}
    assert linked['detection_interval_seconds'] == profiles['day']['detection_interval_seconds']  # active projection
    assert camera['detection_profiles']['night']['detection_interval_seconds'] == 2  # input untouched
    # Linking again is a no-op.
    assert link_camera_profiles([linked], [custom]) == ([linked], False)


def test_global_default_clears_the_profile_and_unknown_ids_keep_values():
    camera = _camera('Yard', day={'detection_interval_seconds': 1}, night={'ingest_frame_fps': 6},
                     day_preset_id=global_default_profile_id('day'), night_preset_id='gone-night')
    [linked], _ = link_camera_profiles([camera], None)
    assert linked['detection_profiles']['day'] == {}
    assert linked['detection_profiles']['night'] == {'ingest_frame_fps': 6}


def test_migration_keeps_what_each_camera_runs():
    cat_day = profile_settings(None, 'cat-small-animal-day', 'day')
    cameras = [
        _camera('Driveway', day=dict(cat_day), night={'detection_interval_seconds': 0.3, 'motion_pixel_threshold': 41}),
        _camera('Gate'),
        # Applied a preset, then edited a field: no longer that preset.
        _camera('Porch', day={**cat_day, 'ingest_frame_fps': 9}, day_preset_id='cat-small-animal-day'),
    ]
    migrated, customs, changed = migrate_camera_profile_links(cameras, None)
    assert changed
    driveway, gate, porch = (camera['detection_profiles'] for camera in migrated)
    assert driveway['day_preset_id'] == 'cat-small-animal-day'
    assert gate['day_preset_id'] == global_default_profile_id('day')
    assert gate['night_preset_id'] == global_default_profile_id('night')
    by_id = {preset['id']: preset for preset in customs}
    assert by_id[driveway['night_preset_id']]['name'] == 'Driveway (Night)'
    assert by_id[driveway['night_preset_id']]['settings'] == {'detection_interval_seconds': 0.3, 'motion_pixel_threshold': 41}
    assert by_id[porch['day_preset_id']]['name'] == 'Porch (Day)'
    # Re-linking from the new profiles reproduces every camera's values exactly.
    relinked, _ = link_camera_profiles(migrated, customs)
    for before, after in zip(cameras, relinked):
        for mode in ('day', 'night'):
            assert after['detection_profiles'][mode] == before['detection_profiles'][mode], (before['name'], mode)
    # A second migration changes nothing.
    assert migrate_camera_profile_links(relinked, customs)[2] is False


def test_two_cameras_with_the_same_custom_values_share_one_profile():
    night = {'detection_interval_seconds': 0.3}
    migrated, customs, _ = migrate_camera_profile_links([_camera('A', night=night), _camera('B', night=night)], None)
    assert len(customs) == 1
    assert cameras_using_profile(migrated, customs[0]['id']) == ['A', 'B']


def test_a_new_camera_with_nothing_set_links_to_global_default():
    [linked], changed = link_camera_profiles([_camera('New')], None)
    assert changed
    profiles = linked['detection_profiles']
    assert profiles['day_preset_id'] == global_default_profile_id('day')
    assert profiles['night_preset_id'] == global_default_profile_id('night')


def test_unlinked_and_flat_legacy_overrides_are_cleared_too():
    camera = {**_camera('Old', day={'ingest_frame_fps': 5, 'motion_background_alpha': 0.02}), 'motion_background_alpha': 0.02}
    [linked], changed = link_camera_profiles([camera], None)
    assert changed
    assert linked['detection_profiles']['day'] == {'ingest_frame_fps': 5}
    assert 'motion_background_alpha' not in linked
