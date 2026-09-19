from __future__ import annotations

from datetime import datetime, timezone

import pytest

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


def _zone_available(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:  # Windows hosts may lack the optional tzdata package
        return False


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


@pytest.mark.skipif(
    not _zone_available('Australia/Sydney'),
    reason='IANA tzdata package not available on this host',
)
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
