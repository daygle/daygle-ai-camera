from __future__ import annotations

from datetime import datetime, timezone

import app.profile_automation as pa
import app.ptz as ptz
import app.state as state
from app.recording_settings import (
    effective_camera_live_settings,
    normalize_camera_detection_profiles,
)


def test_detection_profiles_normalize_performance_fields_and_legacy_fallback():
    profiles = normalize_camera_detection_profiles({
        'active': 'night',
        'night': {
            'detection_interval_seconds': '0.8',
            'detection_confirm_frames': 4,
            'always_run_object_detection': False,
            'object_detection_tiling': '2x2',
            'motion_frame_width': 9999,
        },
    }, {'motion_pixel_threshold': 77})
    assert profiles['active'] == 'night'
    assert profiles['day']['motion_pixel_threshold'] == 77
    assert profiles['night']['motion_pixel_threshold'] == 77
    assert profiles['night']['detection_interval_seconds'] == 0.8
    assert profiles['night']['detection_confirm_frames'] == 4
    assert profiles['night']['always_run_object_detection'] is False
    assert profiles['night']['object_detection_tiling'] == '2x2'
    assert profiles['night']['motion_frame_width'] == 640


def test_effective_camera_live_settings_overlays_active_profile():
    global_settings = {
        'detection_interval_seconds': 0.5,
        'always_run_object_detection': True,
        'motion_pixel_threshold': 30,
    }
    camera = {
        'detection_profiles': {
            'active': 'night',
            'night': {
                'detection_interval_seconds': 0.9,
                'always_run_object_detection': False,
                'motion_pixel_threshold': 70,
            },
        },
    }
    settings = effective_camera_live_settings(camera, global_settings)
    assert settings['detection_interval_seconds'] == 0.9
    assert settings['always_run_object_detection'] is False
    assert settings['motion_pixel_threshold'] == 70


def test_solar_schedule_matches_noaa_reference_times():
    """London solstice boundaries (NOAA ground truth, civil zenith 90.833).

    Regression for the missing right-ascension quadrant adjustment: without
    it the June day window collapsed to ~4.7 hours at London's latitude.
    """
    june = pa.suggest_solar_schedule(51.5072, -0.1276, 'UTC', day=datetime(2026, 6, 21).date())
    assert june['day_start'] == '03:42'
    assert june['night_start'] == '20:21'
    december = pa.suggest_solar_schedule(51.5072, -0.1276, 'UTC', day=datetime(2026, 12, 21).date())
    assert december['day_start'] == '08:03'
    assert december['night_start'] == '15:53'


def test_solar_day_window_grows_toward_summer_solstice():
    """Northern-hemisphere day windows must lengthen into June and shorten
    into September; guards whole-season regressions without pinning values."""
    def _day_length(day_: datetime) -> int:
        suggestion = pa.suggest_solar_schedule(
            51.5072, -0.1276, 'UTC', day=day_.date(),
        )
        def minutes(hhmm: str) -> int:
            hour, minute = hhmm.split(':', 1)
            return int(hour) * 60 + int(minute)
        length = minutes(suggestion['night_start']) - minutes(suggestion['day_start'])
        return length if length > 0 else length + 1440

    june = _day_length(datetime(2026, 6, 21))
    assert june > _day_length(datetime(2026, 3, 20))
    assert june > _day_length(datetime(2026, 9, 23))


def test_solar_day_window_shrinks_toward_june_solstice_in_south():
    """Southern-hemisphere mirror of the northern check: Sydney's day window
    must shorten into June and regrow by the December solstice. The quadrant
    bug distorted the seasons per-hemisphere, so both directions are guarded."""
    def _day_length(day_: datetime) -> int:
        suggestion = pa.suggest_solar_schedule(
            -33.8688, 151.2093, 'UTC', day=day_.date(),
        )
        def minutes(hhmm: str) -> int:
            hour, minute = hhmm.split(':', 1)
            return int(hour) * 60 + int(minute)
        length = minutes(suggestion['night_start']) - minutes(suggestion['day_start'])
        return length if length > 0 else length + 1440

    june = _day_length(datetime(2026, 6, 21))
    assert june < _day_length(datetime(2026, 3, 20))
    assert june < _day_length(datetime(2026, 12, 21))


def test_solar_schedule_suggests_local_times_from_coordinates():
    suggestion = pa.suggest_solar_schedule(
        -33.8688, 151.2093, 'UTC',
        day=datetime(2026, 9, 19).date(),
    )
    assert suggestion['timezone'] == 'UTC'
    assert suggestion['source'] == 'solar'
    assert len(suggestion['day_start']) == 5
    assert len(suggestion['night_start']) == 5
    assert suggestion['day_start'] != suggestion['night_start']


def test_solar_schedule_rejects_missing_coordinates():
    import pytest
    with pytest.raises(ValueError):
        pa.suggest_solar_schedule(None, 151.2, 'UTC')


def test_scheduled_profile_uses_day_and_night_boundaries():
    profiles = {'day_start': '07:00', 'night_start': '19:00'}
    assert pa.scheduled_profile(profiles, now=datetime(2026, 9, 19, 6, 59)) == 'night'
    assert pa.scheduled_profile(profiles, now=datetime(2026, 9, 19, 7, 0)) == 'day'
    assert pa.scheduled_profile(profiles, now=datetime(2026, 9, 19, 18, 59)) == 'day'
    assert pa.scheduled_profile(profiles, now=datetime(2026, 9, 19, 19, 0)) == 'night'


def test_scheduled_profile_handles_overnight_day_window():
    profiles = {'day_start': '19:00', 'night_start': '07:00'}
    assert pa.scheduled_profile(profiles, now=datetime(2026, 9, 19, 23, 0)) == 'day'
    assert pa.scheduled_profile(profiles, now=datetime(2026, 9, 19, 6, 59)) == 'day'
    assert pa.scheduled_profile(profiles, now=datetime(2026, 9, 19, 7, 0)) == 'night'
    assert pa.scheduled_profile(profiles, now=datetime(2026, 9, 19, 12, 0)) == 'night'


def test_scheduled_profile_evaluates_in_camera_timezone():
    profiles = {'day_start': '07:00', 'night_start': '19:00'}
    # 21:00 UTC is 07:00 next day in Sydney (AEST, UTC+10 in September), so the
    # camera's wall clock is exactly at day_start while the server clock in UTC
    # would evaluate the same instant as night.
    utc_instant = datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc)
    assert pa.scheduled_profile(profiles, now=utc_instant, timezone_name='Australia/Sydney') == 'day'
    assert pa.scheduled_profile(profiles, now=utc_instant, timezone_name='UTC') == 'night'
    # Unknown/missing zones keep the legacy server-clock behaviour.
    assert pa.scheduled_profile(profiles, now=utc_instant, timezone_name='Not/A_Zone') == 'night'
    assert pa.scheduled_profile(profiles, now=utc_instant) == 'night'


def test_scheduled_profile_evaluates_dst_correctly_in_camera_timezone():
    """Sydney in January is AEDT (UTC+11), not the AEST (UTC+10) the September
    test exercises: 20:00 UTC is 07:00 next day under DST (day) but 06:00
    under a DST-blind fixed offset (night)."""
    profiles = {'day_start': '07:00', 'night_start': '19:00'}
    dst_instant = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
    assert pa.scheduled_profile(profiles, now=dst_instant, timezone_name='Australia/Sydney') == 'day'


def test_onvif_imaging_probe_reads_ircut_filter(monkeypatch):
    monkeypatch.setattr(ptz, '_get_video_source_token', lambda *args: 'video-token')

    def fake_soap(url, body, username, password):
        assert url.endswith('/onvif/imaging_service')
        assert 'video-token' in body
        return '<tt:ImagingSettings><tt:IrCutFilter>OFF</tt:IrCutFilter></tt:ImagingSettings>'

    monkeypatch.setattr(ptz, '_soap', fake_soap)
    assert ptz.probe_onvif_day_night('192.0.2.10', 80, 'admin', 'secret') == 'night'


def test_onvif_selection_uses_ir_state(monkeypatch):
    monkeypatch.setattr(pa, 'probe_onvif_day_night', lambda *args: 'night')
    camera = {
        'backend': 'onvif', 'host': '192.0.2.10', 'username': 'admin', 'password': 'secret',
        'detection_profiles': {'source': 'onvif', 'day_start': '07:00', 'night_start': '19:00'},
    }
    target, selected_by, ir_state, error = pa._select_target(
        camera, camera['detection_profiles'],
    )
    assert (target, selected_by, ir_state, error) == ('night', 'onvif_ir', 'night', None)


def test_onvif_selection_falls_back_to_schedule_on_probe_error(monkeypatch):
    monkeypatch.setattr(pa, 'probe_onvif_day_night', lambda *args: (_ for _ in ()).throw(OSError('unsupported')))
    camera = {
        'backend': 'onvif', 'host': '192.0.2.10',
        'detection_profiles': {'source': 'onvif', 'day_start': '07:00', 'night_start': '19:00'},
    }
    target, selected_by, ir_state, error = pa._select_target(
        camera, camera['detection_profiles'],
    )
    assert target in {'day', 'night'}
    assert selected_by == 'schedule_fallback'
    assert ir_state is None
    assert error == 'OSError'


def test_poll_switches_runtime_profile_without_restarting_camera(monkeypatch):
    camera = {
        'id': 'profile-test',
        'detection_profiles': {
            'active': 'day', 'source': 'schedule',
            'day_start': '00:00', 'night_start': '00:01',
            'day': {'motion_pixel_threshold': 30},
            'night': {'motion_pixel_threshold': 120},
        },
        'motion_pixel_threshold': 30,
    }
    original_configs = state.cameras_config
    original_database = state.database
    state.cameras_config = [camera]

    class FakeDatabase:
        def __init__(self):
            self.saved = None

        def set_setting(self, key, value, timestamp):
            self.saved = (key, value, timestamp)

    database = FakeDatabase()
    state.database = database
    monkeypatch.setattr(pa, 'scheduled_profile', lambda profiles, now=None, timezone_name=None: 'night')
    try:
        pa.poll_camera_profiles()
        assert pa.profile_status('profile-test')['source'] == 'schedule'
        assert camera['detection_profiles']['active'] == 'night'
        assert camera['motion_pixel_threshold'] == 120
        assert database.saved[0] == 'cameras'
    finally:
        state.cameras_config = original_configs
        state.database = original_database
        state._camera_profile_status.pop('profile-test', None)


def test_poll_passes_camera_timezone_to_schedule_evaluation(monkeypatch):
    """Wiring regression: the poll loop must evaluate the schedule in the
    camera's own timezone, not the server clock. Spies on the real
    scheduled_profile to capture what the poll actually passes."""
    captured = {}
    real_scheduled = pa.scheduled_profile

    def spy(profiles, now=None, timezone_name=None):
        captured['timezone_name'] = timezone_name
        return real_scheduled(profiles, now=now, timezone_name=timezone_name)

    monkeypatch.setattr(pa, 'scheduled_profile', spy)
    camera = {
        'id': 'tz-poll-test',
        'timezone': 'Australia/Sydney',
        'detection_profiles': {
            'source': 'schedule', 'day_start': '07:00', 'night_start': '19:00',
        },
    }
    original_configs = state.cameras_config
    original_database = state.database
    state.cameras_config = [camera]
    state.database = None
    try:
        pa.poll_camera_profiles()
        assert captured['timezone_name'] == 'Australia/Sydney'
        status = pa.profile_status('tz-poll-test')
        assert status['selected_by'] == 'schedule'
        assert status['schedule_target'] in {'day', 'night'}
    finally:
        state.cameras_config = original_configs
        state.database = original_database
        state._camera_profile_status.pop('tz-poll-test', None)


def _schedule_camera(camera_id, **extra):
    profiles = {
        'active': 'day', 'source': 'schedule',
        'day_start': '00:00', 'night_start': '00:01',
        'day': {'motion_pixel_threshold': 30},
        'night': {'motion_pixel_threshold': 120},
    }
    camera = {'id': camera_id, 'detection_profiles': profiles, 'motion_pixel_threshold': 30}
    camera.update(extra)
    return camera


class _CaptureDatabase:
    def __init__(self):
        self.saved = None

    def set_setting(self, key, value, timestamp):
        self.saved = (key, value, timestamp)


def test_poll_does_not_revert_concurrent_source_edit(monkeypatch):
    """Race regression: the monitor must not persist its stale snapshot over a
    concurrent API edit. The API edit lands between the monitor's snapshot and
    its remerge (simulated by hooking the probe loop) and changes the camera's
    automation source to manual -- the operator's action must win and the
    stale flip must be dropped."""
    camera = _schedule_camera('race-cam', name='Before')
    original_configs = state.cameras_config
    original_database = state.database
    state.cameras_config = [camera]
    database = _CaptureDatabase()
    state.database = database
    monkeypatch.setattr(pa, 'scheduled_profile', lambda profiles, now=None, timezone_name=None: 'night')

    real_update_status = pa._update_status

    def edit_during_probe(camera_id, **values):
        real_update_status(camera_id, **values)
        if not edit_during_probe.landed:
            edit_during_probe.landed = True
            # Simulate the settings API committing an edit mid-poll: source
            # flipped to manual and the name changed.
            camera['name'] = 'After'
            camera['detection_profiles']['source'] = 'manual'

    edit_during_probe.landed = False
    monkeypatch.setattr(pa, '_update_status', edit_during_probe)
    try:
        pa.poll_camera_profiles()
        assert edit_during_probe.landed
        assert database.saved is not None
        persisted = database.saved[1][0]
        assert persisted['name'] == 'After'  # edit survived
        assert persisted['detection_profiles']['source'] == 'manual'  # edit survived
        assert persisted['detection_profiles']['active'] == 'day'  # stale flip dropped
    finally:
        state.cameras_config = original_configs
        state.database = original_database
        state._camera_profile_status.pop('race-cam', None)


def test_poll_preserves_concurrent_value_edit_and_still_flips(monkeypatch):
    """A concurrent edit of profile VALUES must survive the remerge while the
    due active-profile flip still applies (the flip is spliced, not snapshotted)."""
    camera = _schedule_camera('value-cam')
    original_configs = state.cameras_config
    original_database = state.database
    state.cameras_config = [camera]
    database = _CaptureDatabase()
    state.database = database
    monkeypatch.setattr(pa, 'scheduled_profile', lambda profiles, now=None, timezone_name=None: 'night')

    real_update_status = pa._update_status

    def edit_during_probe(camera_id, **values):
        real_update_status(camera_id, **values)
        if not edit_during_probe.landed:
            edit_during_probe.landed = True
            # Operator edits a night-profile VALUE mid-poll (source unchanged).
            camera['detection_profiles']['night']['motion_pixel_threshold'] = 200

    edit_during_probe.landed = False
    monkeypatch.setattr(pa, '_update_status', edit_during_probe)
    try:
        pa.poll_camera_profiles()
        assert edit_during_probe.landed
        persisted = database.saved[1][0]
        assert persisted['detection_profiles']['active'] == 'night'  # flip applied
        assert persisted['detection_profiles']['night']['motion_pixel_threshold'] == 200  # edit survived
    finally:
        state.cameras_config = original_configs
        state.database = original_database
        state._camera_profile_status.pop('value-cam', None)


def test_poll_does_not_resurrect_camera_removed_during_poll(monkeypatch):
    """A camera deleted by a concurrent API save during the poll must not be
    resurrected by the monitor's persist."""
    camera = _schedule_camera('doomed-cam')
    original_configs = state.cameras_config
    original_database = state.database
    state.cameras_config = [camera]
    database = _CaptureDatabase()
    state.database = database
    monkeypatch.setattr(pa, 'scheduled_profile', lambda profiles, now=None, timezone_name=None: 'night')

    real_update_status = pa._update_status

    def remove_during_probe(camera_id, **values):
        real_update_status(camera_id, **values)
        if not remove_during_probe.landed:
            remove_during_probe.landed = True
            # Concurrent API save replaced the list without this camera.
            state.cameras_config = [_schedule_camera('other-cam')]

    remove_during_probe.landed = False
    monkeypatch.setattr(pa, '_update_status', remove_during_probe)
    try:
        pa.poll_camera_profiles()
        assert remove_during_probe.landed
        persisted_ids = [str(cam.get('id')) for cam in database.saved[1]]
        assert persisted_ids == ['other-cam']  # removed camera not resurrected
    finally:
        state.cameras_config = original_configs
        state.database = original_database
        state._camera_profile_status.pop('doomed-cam', None)
        state._camera_profile_status.pop('other-cam', None)
