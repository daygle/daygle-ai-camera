"""Automatic day/night profile selection for configured cameras.

The monitor is deliberately best-effort: schedule selection is deterministic,
while ONVIF imaging status is optional and falls back to the schedule whenever a
camera does not expose a usable IrCutFilter value.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from math import acos, asin, atan, cos, degrees, floor, radians, sin, tan
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import app.state as _state
from app.auth import utc_now
from app.config_facades import effective_cameras_config
from app.ptz import probe_onvif_day_night
from app.recording_settings import (
    apply_active_camera_detection_profile,
    normalize_camera_detection_profiles,
)

logger = logging.getLogger('daygle.ai')

_PROFILE_POLL_SECONDS = 30.0
_ONVIF_POLL_SECONDS = 60.0


def suggest_solar_schedule(
    latitude: float,
    longitude: float,
    timezone_name: str,
    *,
    day: date | None = None,
) -> dict[str, Any]:
    """Suggest local sunrise/sunset profile times using the NOAA algorithm.

    No network or third-party astronomy package is required. The result uses a
    civil-sunrise/sunset zenith of 90.833 degrees and rounds to the nearest
    minute in the camera's IANA timezone.
    """
    try:
        lat = float(latitude)
        lon = float(longitude)
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError('coordinates out of range')
        timezone_value = str(timezone_name or 'UTC')
        # ``UTC`` is always available even on Windows images that do not ship
        # the optional IANA tzdata package. Other zones still use ZoneInfo so
        # DST-aware local sunrise/sunset calculations remain accurate.
        zone = timezone.utc if timezone_value.upper() == 'UTC' else ZoneInfo(timezone_value)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:

        raise ValueError('Valid latitude, longitude, and IANA timezone are required.') from exc
    target_date = day or datetime.now(zone).date()

    def _event(is_sunrise: bool) -> str | None:
        ordinal = target_date.timetuple().tm_yday
        lng_hour = lon / 15.0
        base_hour = 6.0 if is_sunrise else 18.0
        approx = ordinal + ((base_hour - lng_hour) / 24.0)
        mean_anomaly = (0.9856 * approx) - 3.289
        true_longitude = mean_anomaly + (1.916 * sin(radians(mean_anomaly))) + (0.020 * sin(radians(2 * mean_anomaly))) + 282.634
        true_longitude %= 360.0
        right_ascension = degrees(atan(0.91764 * tan(radians(true_longitude)))) % 360.0
        # NOAA quadrant adjustment: atan() folds RA into (-90, 90], so raise RA
        # into the same quadrant as the sun's true longitude. Without this the
        # suggested windows are hours off for whole seasons/latitudes (London in
        # June evaluated to a 4.7-hour day).
        true_longitude_quadrant = floor(true_longitude / 90.0) * 90.0
        right_ascension_quadrant = floor(right_ascension / 90.0) * 90.0
        right_ascension = (right_ascension + true_longitude_quadrant - right_ascension_quadrant) / 15.0
        sin_declination = 0.39782 * sin(radians(true_longitude))
        cos_declination = cos(asin(sin_declination))
        cos_hour_angle = (cos(radians(90.833)) - (sin_declination * sin(radians(lat)))) / (cos_declination * cos(radians(lat)))
        if cos_hour_angle < -1.0 or cos_hour_angle > 1.0:
            return None
        hour_angle = degrees(acos(cos_hour_angle))
        if is_sunrise:
            hour_angle = 360.0 - hour_angle
        hour_angle /= 15.0
        local_mean_time = hour_angle + right_ascension - (0.06571 * approx) - 6.622
        utc_hour = (local_mean_time - lng_hour) % 24.0
        utc_dt = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=utc_hour)
        local_dt = utc_dt.astimezone(zone)
        return local_dt.strftime('%H:%M')

    sunrise = _event(True)
    sunset = _event(False)
    if sunrise is None or sunset is None:
        raise ValueError('No sunrise or sunset is available for these coordinates on this date.')
    return {
        'date': target_date.isoformat(),
        'timezone': str(zone),
        'day_start': sunrise,
        'night_start': sunset,
        'source': 'solar',
    }


def _resolve_timezone(name: Any) -> tzinfo | None:
    """Resolve an IANA timezone name, tolerating unknown/missing tzdata zones."""
    text = str(name or '').strip()
    if not text:
        return None
    try:
        return timezone.utc if text.upper() == 'UTC' else ZoneInfo(text)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None


def scheduled_profile(
    profiles: dict[str, Any],
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> str:
    """Return the schedule-selected profile for the camera's local clock.

    Boundaries are wall-clock HH:MM strings in the camera's IANA timezone, so
    ``timezone_name`` (the camera's stored zone) converts the evaluation
    instant before comparing. An aware ``now`` is converted into that zone; a
    naive ``now`` is treated as already being the wall clock to evaluate.
    Without a resolvable zone the call keeps the legacy server-clock behaviour.
    The normal case is day_start <= night_start. The comparison also handles an
    overnight day window (for example day=19:00, night=07:00) without special
    configuration.
    """
    zone = _resolve_timezone(timezone_name)
    current = now
    if zone is not None:
        if current is None:
            current = datetime.now(zone)
        elif current.tzinfo is not None:
            current = current.astimezone(zone)
    elif current is None:
        current = datetime.now()
    current_minutes = current.hour * 60 + current.minute

    def _minutes(value: Any, fallback: int) -> int:
        try:
            hour, minute = str(value).split(':', 1)
            return max(0, min(1439, int(hour) * 60 + int(minute)))
        except (TypeError, ValueError):
            return fallback

    day_start = _minutes(profiles.get('day_start'), 7 * 60)
    night_start = _minutes(profiles.get('night_start'), 19 * 60)
    if day_start < night_start:
        return 'day' if day_start <= current_minutes < night_start else 'night'
    if day_start > night_start:
        return 'night' if night_start <= current_minutes < day_start else 'day'
    return 'day'


def profile_status(camera_id: str) -> dict[str, Any]:
    """Return a copy of the latest profile-selection diagnostics."""
    with _state._camera_profile_status_lock:
        status = dict(_state._camera_profile_status.get(str(camera_id), {}))
    return status or {
        'camera_id': str(camera_id),
        'active': None,
        'source': 'manual',
        'selected_by': 'manual',
        'ir_state': None,
        'error': None,
    }


def _camera_host(camera: dict[str, Any]) -> str:
    host = str(camera.get('host') or '').strip()
    if host:
        return host
    stream_url = str(camera.get('stream_url') or '')
    if '://' not in stream_url:
        return ''
    try:
        from urllib.parse import urlsplit
        return str(urlsplit(stream_url).hostname or '')
    except ValueError:
        return ''


def _update_status(camera_id: str, **values: Any) -> None:
    with _state._camera_profile_status_lock:
        current = _state._camera_profile_status.setdefault(str(camera_id), {})
        current.update(values)
        current['camera_id'] = str(camera_id)


def record_ir_probe(camera_id: str, ir_state: str | None, error: str | None = None) -> dict[str, Any]:
    """Publish the result of an on-demand IR-state check for the API/UI."""
    _update_status(
        camera_id,
        ir_state=ir_state,
        ir_checked_at=datetime.now().isoformat(timespec='seconds'),
        ir_error=error,
    )
    return profile_status(camera_id)


def _select_target(camera: dict[str, Any], profiles: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    source = profiles.get('source', 'manual')
    scheduled = scheduled_profile(profiles, timezone_name=camera.get('timezone'))
    if source == 'schedule':
        return scheduled, 'schedule', None, None
    if source != 'onvif' or str(camera.get('backend') or '').lower() not in {'onvif', 'rtsp'}:
        return profiles['active'], 'manual', None, None

    host = _camera_host(camera)
    if not host:
        return scheduled, 'schedule_fallback', None, 'Camera host is unavailable for ONVIF imaging status.'
    ptz = camera.get('ptz') if isinstance(camera.get('ptz'), dict) else {}
    http_port = int(ptz.get('http_port') or camera.get('http_port') or 80)
    try:
        ir_state = probe_onvif_day_night(
            host,
            http_port,
            str(camera.get('username') or ''),
            str(camera.get('password') or ''),
        )
    except Exception as exc:  # camera-specific ONVIF support is optional
        return scheduled, 'schedule_fallback', None, type(exc).__name__
    if ir_state in {'day', 'night'}:
        return ir_state, 'onvif_ir', ir_state, None
    return scheduled, 'schedule_fallback', None, 'ONVIF camera did not report a usable IrCutFilter.'


def poll_camera_profiles() -> None:
    """Apply any due automatic profile changes without restarting streams.

    Structured snapshot -> probe -> remerge -> persist: the config snapshot is
    taken under ``_cameras_config_write_lock``, the (potentially slow) ONVIF
    probes run WITHOUT the lock so a stalled camera cannot block camera
    settings writes, and only the short remerge+persist section re-takes the
    lock and re-reads live state so a concurrent API edit is never clobbered
    by a stale whole-list write.
    """
    with _state._cameras_config_write_lock:
        configs = list(_state.cameras_config or effective_cameras_config())
    # Mutate deep COPIES, never the live list: the remerge step below decides
    # per-camera whether automation results or a concurrent API edit wins.
    working = [copy.deepcopy(camera) for camera in configs]
    changed: dict[str, dict[str, Any]] = {}
    for camera in working:
        camera_id = str(camera.get('id') or '')
        if not camera_id:
            continue
        profiles = normalize_camera_detection_profiles(
            camera.get('detection_profiles'), camera,
        )
        target, selected_by, ir_state, error = _select_target(camera, profiles)
        active = profiles['active']
        if selected_by != 'manual' and target != active:
            profiles['active'] = target
            camera['detection_profiles'] = profiles
            apply_active_camera_detection_profile(camera)
            # Keep the automation metadata authoritative even if a legacy
            # normalizer receives a partially shaped profile object.
            camera['detection_profiles']['active'] = target
            changed[camera_id] = {
                'target': target,
                'source': str(profiles.get('source') or 'manual'),
            }
            active = target
            logger.info(
                'Camera %s switched to %s profile (%s).',
                camera_id, target, selected_by,
            )
        _update_status(
            camera_id,
            active=active,
            source=profiles.get('source', 'manual'),
            selected_by=selected_by,
            schedule_target=scheduled_profile(profiles, timezone_name=camera.get('timezone')),
            ir_state=ir_state,
            error=error,
            checked_at=datetime.now().isoformat(timespec='seconds'),
        )

    if not changed or _state.database is None:
        return
    # Remerge: rebuild the list from LIVE state (a concurrent API edit may
    # have replaced cameras since the snapshot), splice in only the active-
    # profile flip per camera, then persist atomically under the write lock.
    with _state._cameras_config_write_lock:
        live = list(_state.cameras_config or effective_cameras_config())
        for camera in live:
            decision = changed.pop(str(camera.get('id') or ''), None)
            if decision is None:
                continue
            current_profiles = normalize_camera_detection_profiles(
                camera.get('detection_profiles'), camera,
            )
            if str(current_profiles.get('source') or 'manual') != decision['source']:
                # A concurrent edit changed this camera's automation mode
                # mid-poll: the operator's action wins, and the (now stale)
                # automation decision is dropped without touching the camera.
                # The next poll re-evaluates under the new source.
                continue
            # Splice ONLY the active-profile flip onto the live profiles so a
            # concurrent edit of profile VALUES (thresholds, intervals, ...)
            # is preserved, not overwritten by the snapshot's copies.
            current_profiles['active'] = decision['target']
            camera['detection_profiles'] = current_profiles
            apply_active_camera_detection_profile(camera)
        persisted = copy.deepcopy(live)
        if changed:
            # Cameras the snapshot had that live state no longer does (removed
            # by a concurrent API save): drop their automation decisions -- a
            # deleted camera must not be resurrected by the monitor.
            logger.info(
                'Skipping automatic profile persist for %d camera(s) removed during poll.',
                len(changed),
            )
        try:
            _state.database.set_setting('cameras', persisted, utc_now())
        except Exception as exc:  # persistence failure must not stop detection
            logger.warning('Could not persist automatic day/night profile change: %s', exc)


def _monitor_loop() -> None:
    next_poll = 0.0
    while not _state._camera_profile_monitor_stop.is_set():
        now = time.monotonic()
        if now >= next_poll:
            try:
                poll_camera_profiles()
            except Exception:
                logger.warning('Automatic camera profile poll failed.', exc_info=True)
            next_poll = now + _PROFILE_POLL_SECONDS
        _state._camera_profile_monitor_stop.wait(1.0)


def start_profile_monitor() -> None:
    """Start the process-wide automatic profile monitor once."""
    thread = _state._camera_profile_monitor_thread
    if isinstance(thread, threading.Thread) and thread.is_alive():
        return
    _state._camera_profile_monitor_stop.clear()
    thread = threading.Thread(target=_monitor_loop, name='camera-profile-monitor', daemon=True)
    _state._camera_profile_monitor_thread = thread
    thread.start()


def stop_profile_monitor() -> None:
    """Stop the profile monitor during application shutdown."""
    _state._camera_profile_monitor_stop.set()
    thread = _state._camera_profile_monitor_thread
    if isinstance(thread, threading.Thread) and thread.is_alive():
        thread.join(timeout=3.0)
    _state._camera_profile_monitor_thread = None
