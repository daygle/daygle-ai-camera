"""Timezone-aware day filtering for the camera-log and application-log pages.

The camera-log / application-log ``date_from`` / ``date_to`` filters used to
resolve to the UTC calendar day (camera-log) or the server's local day
(application-log), so a viewer whose timezone straddles UTC midnight saw the
wrong day's entries. Both endpoints now accept ``tz_offset_minutes`` (the
browser's ``Date.getTimezoneOffset()``) and resolve the requested days in the
viewer's timezone, via the shared ``local_day_bounds_to_utc`` helper - matching
the ``/api/recordings/timeline`` endpoint.
"""
from datetime import datetime, timezone

from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports

from app.utils import local_day_bounds_to_utc


def test_local_day_bounds_default_is_utc_day():
    start, end = local_day_bounds_to_utc('2026-08-18', '2026-08-18', None)
    assert start == datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)


def test_local_day_bounds_east_of_utc():
    # UTC+10 -> getTimezoneOffset() == -600. The viewer's Aug 18 starts at
    # 2026-08-17T14:00Z and ends at 2026-08-18T14:00Z.
    start, end = local_day_bounds_to_utc('2026-08-18', '2026-08-18', -600)
    assert start == datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc)


def test_local_day_bounds_west_of_utc():
    # UTC-5 -> getTimezoneOffset() == +300. The viewer's Aug 18 starts at
    # 2026-08-18T05:00Z and ends at 2026-08-19T05:00Z.
    start, end = local_day_bounds_to_utc('2026-08-18', '2026-08-18', 300)
    assert start == datetime(2026, 8, 18, 5, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)


def test_local_day_bounds_none_inputs():
    assert local_day_bounds_to_utc(None, None, None) == (None, None)
    start, end = local_day_bounds_to_utc('2026-08-18', None, None)
    assert start == datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
    assert end is None


def test_camera_log_filter_uses_viewer_timezone_day(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    db = main.database

    # A diagnostic at 02:00 UTC on Aug 18. For a UTC-5 viewer that instant is
    # 21:00 on Aug 17 (their previous local day).
    db.add_camera_diagnostic(
        created_at='2026-08-18T02:00:00+00:00', camera_id='cam', camera_name='Cam',
        event_type='detection_backoff', severity='warning', message='straddle',
    )

    def bounds(day, tz_offset):
        start, end = local_day_bounds_to_utc(day, day, tz_offset)
        return {
            'created_after': start.isoformat() if start else None,
            'created_before': end.isoformat() if end else None,
        }

    # UTC day: the row belongs to Aug 18.
    assert db.count_camera_diagnostics(**bounds('2026-08-18', None)) == 1
    assert db.count_camera_diagnostics(**bounds('2026-08-17', None)) == 0

    # UTC-5 viewer (getTimezoneOffset() == +300): the same instant is on Aug 17.
    assert db.count_camera_diagnostics(**bounds('2026-08-18', 300)) == 0
    assert db.count_camera_diagnostics(**bounds('2026-08-17', 300)) == 1
