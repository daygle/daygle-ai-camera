"""Settings payload validators extracted from ``app/main.py`` (Phase-22).

The 9 helpers shipped here cluster around settings-router payload
validation -- the small, mostly-pure functions called by every mutating
settings endpoint to coerce and bounds-check an inbound ``dict`` payload
before the route writes it back to the database / record-store.

Like the prior-cluster extractions (``app/auth_gates.py`` Phase-16,
``app/config_facades.py`` Phase-17, ``app/camera_config.py`` Phase-18,
``app/recording_settings.py`` Phase-19, ``app/ai_settings.py`` Phase-20,
``app/zone_schema.py`` Phase-21), these are extracted using the
**hybrid-pattern template**:

- Cluster functions reach ``main.<attr>`` at *call time* (NOT import
  time) for their cross-module dependencies, so they continue to work
  seamlessly when ``app/main.py`` is partially loaded during the
  Pool A rebind loop.
- The Pool A from-import rebinds live at the TOP of ``app/main.py``
  (alphabetically sorted, in the existing rebind section) so that the
  eager-evaluation order at module load has ``main.<name>`` wired
  correctly before any sibling body references it as a bare name.
- Intra-cluster references (``_int_field`` -> ``validate_camera_settings``
  and siblings; ``validate_camera_settings`` -> ``validate_cameras_settings``)
  resolve as bare names within the same module, no rebind needed.

Cluster membership:

- ``_int_field`` -- the small int-coercion + range-check helper used
  by every other validator (~4 call sites per validator that uses
  it). Raises ``fastapi.HTTPException(400, ...)`` for non-numeric
  input or out-of-range values.

- ``validate_alert_email_settings`` -- the alert-email settings
  payload validator (SMTP host/port/TLS/SSL/from_address/etc.).
  Raises ``HTTPException(400)`` for bad port, missing host when
  enabled, invalid from-address, and TLS/SSL mutual-exclusion.

- ``validate_push_notification_settings`` -- the ntfy-style push
  payload validator (server_url / topic / priority / etc.).
  Raises ``HTTPException(400)`` for unknown priority and missing
  topic when enabled. Default ``server_url`` to ntfy.sh and
  ``priority`` to ``default`` when missing.

- ``validate_camera_settings`` -- the heaviest cluster member
  (per-camera full settings validator: backend / dims / FPS / flip /
  detection / recording / PTZ / per-camera motion overrides). Reaches
  the most Pool C sites (~7 main.<attr> reach surfaces).

- ``validate_cameras_settings`` -- the multi-camera list-validator
  that delegates each row to ``validate_camera_settings`` and enforces
  duplicate-id rejection.

- ``validate_recording_settings`` -- recording format + pre/post-event
  buffer + max-clip + chunk-duration + retention validator.

- ``validate_storage_settings`` -- storage data/snapshot/events/recordings
  directory validator (preserves the on-disk DB path from
  ``main.config['storage']['database']`` even when the override
  attempts to change it).

- ``validate_auth_settings`` -- session-timeout + max-login-attempts +
  lockout-minutes validator.

- ``validate_live_settings`` -- the second-heaviest cluster member
  (per-camera live framerates + detection interval + motion-tuner
  fields + periodic-scan interval).

Pool C reach sites (resolved via ``main.<attr>`` at call time):

- ``main.effective_email_alert_settings`` (validate_alert_email_settings)
- ``main.effective_push_notification_settings`` (validate_push_notification_settings)
- ``main.normalize_bool_setting`` (validate_push_notification_settings +
  validate_camera_settings + validate_recording_settings +
  validate_live_settings)
- ``main.normalize_camera_id`` (validate_camera_settings) -- reaches
  via Phase-18 camera_config rebind
- ``main.camera_default_name`` (validate_camera_settings)
- ``main.default_camera_detection_settings`` (validate_camera_settings)
- ``main.build_stream_url`` (validate_camera_settings)
- ``main.normalize_label_list`` (validate_camera_settings) -- reaches
  via Phase-21 zone_schema rebind
- ``main.normalize_monitoring_zones`` (validate_camera_settings) --
  reaches via Phase-21 zone_schema rebind
- ``main._migrate_legacy_camera_motion`` (validate_camera_settings) --
  reaches via Phase-19 recording_settings rebind
- ``main.normalize_camera_recording_settings`` (validate_camera_settings)
- ``main.normalize_camera_ptz_settings`` (validate_camera_settings)
- ``main.effective_recording_config`` (validate_recording_settings)
- ``main.effective_storage_config`` (validate_storage_settings)
- ``main.config`` (validate_storage_settings) -- for the
  ``database`` path preservation
- ``main.effective_auth_config`` (validate_auth_settings)
- ``main.effective_live_config`` (validate_live_settings)
- ``main.cameras_config`` (validate_cameras_settings) -- runtime
  set at module-load via Phase-17 config_facades rebind
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.camera_config import normalize_camera_id
from app.config_facades import (
    effective_auth_config,
    effective_email_alert_settings,
    effective_live_config,
    effective_push_notification_settings,
    effective_recording_config,
    effective_storage_config,
)
from app.recording_settings import (
    _migrate_legacy_camera_motion,
    normalize_camera_ptz_settings,
    normalize_camera_recording_settings,
)
from app.utils import (
    build_stream_url,
    camera_default_name,
    default_camera_detection_settings,
    normalize_bool_setting,
)
from app.zone_schema import normalize_label_list, normalize_monitoring_zones


def _int_field(payload: dict[str, Any], field: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(payload.get(field, default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f'{field} must be an integer.') from exc
    if value < minimum or value > maximum:
        raise HTTPException(status_code=400, detail=f'{field} must be between {minimum} and {maximum}.')
    return value


def validate_alert_email_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_email_alert_settings()
    allowed = {'enabled', 'host', 'port', 'username', 'password', 'from_address', 'use_tls', 'use_ssl'}
    updated = {key: current.get(key) for key in allowed if key in current}
    for key, value in payload.items():
        if key in allowed:
            updated[key] = value
    for key in ('enabled', 'use_tls', 'use_ssl'):
        value = updated.get(key, False)
        updated[key] = value.lower() in {'1', 'true', 'yes', 'on'} if isinstance(value, str) else bool(value)
    try:
        updated['port'] = int(updated.get('port') or 587)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='SMTP port must be an integer.') from exc
    if not 1 <= updated['port'] <= 65535:
        raise HTTPException(status_code=400, detail='SMTP port must be between 1 and 65535.')
    for key in ('host', 'username', 'password', 'from_address'):
        updated[key] = str(updated.get(key) or '').strip()
    if updated['enabled'] and (not updated['host']):
        raise HTTPException(status_code=400, detail='SMTP host is required when email alerts are enabled.')
    if updated['enabled'] and (not updated['from_address']):
        raise HTTPException(status_code=400, detail='From address is required when email alerts are enabled.')
    if updated['from_address'] and '@' not in updated['from_address']:
        raise HTTPException(status_code=400, detail='From address must be a valid email address.')
    if updated['use_ssl']:
        updated['use_tls'] = False
    return updated


def validate_push_notification_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_push_notification_settings()
    allowed = {'enabled', 'server_url', 'topic', 'priority', 'username', 'password'}
    updated = {key: current.get(key) for key in allowed if key in current}
    for key, value in payload.items():
        if key in allowed:
            updated[key] = value
    updated['enabled'] = normalize_bool_setting(updated.get('enabled', False))
    for key in ('server_url', 'topic', 'priority', 'username', 'password'):
        updated[key] = str(updated.get(key) or '').strip()
    if not updated['server_url']:
        updated['server_url'] = 'https://ntfy.sh'
    if not updated['priority']:
        updated['priority'] = 'default'
    valid_priorities = {'min', 'low', 'default', 'high', 'urgent'}
    if updated['priority'] not in valid_priorities:
        raise HTTPException(
            status_code=400,
            detail=f"priority must be one of: {', '.join(sorted(valid_priorities))}.",
        )
    if updated['enabled'] and (not updated['topic']):
        raise HTTPException(status_code=400, detail='Topic is required when push notifications are enabled.')
    return updated


def validate_camera_settings(payload: dict[str, Any], current: dict[str, Any] | None=None, index: int=1) -> dict[str, Any]:
    current = current or {}
    updated = {key: current.get(key) for key in ('id', 'name', 'backend', 'device', 'width', 'height', 'fps', 'flip', 'stream_url', 'host', 'port', 'path', 'username', 'password') if key in current}
    updated.update({key: payload[key] for key in ('id', 'name', 'backend', 'device', 'flip', 'stream_url', 'host', 'port', 'path', 'username', 'password') if key in payload})
    backend = str(updated.get('backend', 'onvif')).lower()
    if backend not in {'onvif', 'rtsp'}:
        raise HTTPException(status_code=400, detail='Camera backend must be onvif or rtsp.')
    updated['backend'] = backend
    updated['id'] = normalize_camera_id(updated.get('id'), f'camera-{index}')
    updated['name'] = camera_default_name(updated, f'Camera {index}')
    updated['device'] = payload.get('device', current.get('device', 0))
    updated['width'] = _int_field({**current, **payload}, 'width', 1280, 160, 7680)
    updated['height'] = _int_field({**current, **payload}, 'height', 720, 120, 4320)
    updated['fps'] = _int_field({**current, **payload}, 'fps', 15, 1, 120)
    if 'port' in updated or 'port' in payload:
        updated['port'] = _int_field({**current, **payload}, 'port', 554, 1, 65535)
    for key in ('stream_url', 'host', 'path', 'username', 'password'):
        if key in updated:
            updated[key] = str(updated.get(key) or '').strip()
    if not updated.get('password') and current.get('password'):
        updated['password'] = current['password']
    if backend in {'onvif', 'rtsp'} and (not build_stream_url(updated)):
        raise HTTPException(status_code=400, detail='stream_url is required for ONVIF/RTSP cameras, or provide host plus optional username, password, port, and path.')
    flip = str(updated.get('flip', 'none')).lower()
    if flip not in {'none', 'horizontal', 'vertical', 'both'}:
        raise HTTPException(status_code=400, detail='flip must be none, horizontal, vertical, or both.')
    updated['flip'] = flip
    detection = default_camera_detection_settings()
    existing_detection = current.get('detection') if isinstance(current.get('detection'), dict) else {}
    payload_detection = payload.get('detection') if isinstance(payload.get('detection'), dict) else {}
    detection.update(existing_detection)
    detection.update(payload_detection)
    detection['object_detection_enabled'] = normalize_bool_setting(detection.get('object_detection_enabled', True), True)
    detection['object_labels'] = normalize_label_list(detection.get('object_labels', []))
    detection['zones'] = normalize_monitoring_zones(detection.get('zones', []))
    _migrate_legacy_camera_motion(detection)
    updated['detection'] = detection
    existing_recording = current.get('recording') if isinstance(current.get('recording'), dict) else {}
    payload_recording = payload.get('recording') if isinstance(payload.get('recording'), dict) else {}
    updated['recording'] = normalize_camera_recording_settings({**existing_recording, **payload_recording})
    existing_ptz = current.get('ptz') if isinstance(current.get('ptz'), dict) else {}
    payload_ptz = payload.get('ptz') if isinstance(payload.get('ptz'), dict) else {}
    updated['ptz'] = normalize_camera_ptz_settings({**existing_ptz, **payload_ptz})
    # Per-camera motion overrides. Accept flat motion_* keys (current UI format)
    # or the legacy nested motion dict (old format). Always persist flat motion_*
    # keys so the naming convention matches global live settings.
    #
    # Resolution order for each key:
    #   1. Flat key present in payload with a non-None value → use it
    #   2. Flat key present in payload as None (explicit clear) → omit the key
    #   3. Legacy nested motion dict in payload with a non-None value → use it
    #   4. Existing flat key in stored config → carry it forward (preserve)
    #   5. Existing nested motion dict in stored config → carry it forward
    _payload_motion_nest = payload.get('motion') if isinstance(payload.get('motion'), dict) else {}
    _cur_motion_nest = current.get('motion') if isinstance(current.get('motion'), dict) else {}
    _flat_keys = ('motion_pixel_threshold', 'motion_gate_fraction', 'motion_scale_fraction', 'motion_background_alpha')
    _flat_in_payload = any(k in payload for k in _flat_keys) or bool(_payload_motion_nest)

    for _flat_key, _short_key in (
        ('motion_pixel_threshold', 'pixel_threshold'),
        ('motion_gate_fraction', 'gate_fraction'),
        ('motion_scale_fraction', 'scale_fraction'),
        ('motion_background_alpha', 'background_alpha'),
    ):
        if _flat_key in payload:
            # Explicit send from UI: None means "clear override", value means "set"
            _v = payload[_flat_key]
            if _v is None:
                continue  # cleared - omit from updated
            try:
                if _flat_key == 'motion_pixel_threshold':
                    updated[_flat_key] = max(1, min(255, int(_v)))
                else:
                    updated[_flat_key] = round(float(_v), 6)
            except (TypeError, ValueError):
                pass
        elif _payload_motion_nest.get(_short_key) is not None:
            # Legacy nested dict in payload
            _v = _payload_motion_nest[_short_key]
            try:
                if _flat_key == 'motion_pixel_threshold':
                    updated[_flat_key] = max(1, min(255, int(_v)))
                else:
                    updated[_flat_key] = round(float(_v), 6)
            except (TypeError, ValueError):
                pass
        elif not _flat_in_payload:
            # Payload has no motion keys at all - preserve stored override
            _cur_v = current.get(_flat_key) if current.get(_flat_key) is not None else _cur_motion_nest.get(_short_key)
            if _cur_v is not None:
                try:
                    if _flat_key == 'motion_pixel_threshold':
                        updated[_flat_key] = max(1, min(255, int(_cur_v)))
                    else:
                        updated[_flat_key] = round(float(_cur_v), 6)
                except (TypeError, ValueError):
                    pass
    return updated


def validate_cameras_settings(payload: Any) -> list[dict[str, Any]]:
    raw_cameras = payload.get('cameras') if isinstance(payload, dict) else payload
    if not isinstance(raw_cameras, list):
        raise HTTPException(status_code=400, detail='cameras must be a list.')
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    current_by_id = {str(camera_settings.get('id')): camera_settings for camera_settings in _state.cameras_config}
    for index, raw_camera in enumerate(raw_cameras, start=1):
        if not isinstance(raw_camera, dict):
            raise HTTPException(status_code=400, detail='Each camera must be an object.')
        current = current_by_id.get(str(raw_camera.get('id'))) or (_state.cameras_config[index - 1] if index <= len(_state.cameras_config) else {})
        camera_settings = validate_camera_settings(raw_camera, current=current, index=index)
        if camera_settings['id'] in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate camera id: {camera_settings['id']}.")
        seen.add(camera_settings['id'])
        validated.append(camera_settings)
    return validated


def validate_recording_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_recording_config()
    merged = {**current, **payload}
    fmt = str(merged.get('format', 'mp4')).strip().lstrip('.').lower() or 'mp4'
    if fmt == 'avi':
        fmt = 'mp4'
    if fmt != 'mp4':
        raise HTTPException(status_code=400, detail='Recording format must be mp4 for browser playback.')
    return {'pre_event_seconds': _int_field(merged, 'pre_event_seconds', 10, 0, 300), 'post_event_seconds': _int_field(merged, 'post_event_seconds', 15, 0, 300), 'extension_step_seconds': _int_field(merged, 'extension_step_seconds', 45, 0, 300), 'max_clip_seconds': _int_field(merged, 'max_clip_seconds', 300, 1, 3600), 'format': fmt, 'chunk_duration_seconds': _int_field(merged, 'chunk_duration_seconds', 3600, 60, 86400), 'retention_days': _int_field(merged, 'retention_days', 14, 1, 3650), 'max_storage_gb': _int_field(merged, 'max_storage_gb', 20, 1, 100000), 'auto_purge_enabled': normalize_bool_setting(merged.get('auto_purge_enabled', True), True)}


def _resolve_within_data_envelope(value: str, *, key: str) -> str:
    """C1 fix: canonicalise ``value`` and reject paths that escape the data envelope.

    Allowed envelope:

    1. The startup ``data_dir`` itself, or any descendant of it.
    2. The parent of the startup ``data_dir`` (so an admin can relocate the
       entire data root to a sibling like ``/srv/daygle-data`` without bypassing
       the validator).

    Anything else (``/etc/foo``, ``/var/spool/cron``, ``/root``, ancestors of
    the data parent) is rejected with HTTP 400.

    The resolver also collapses ``..``, ``~``, and symlink hops via
    ``Path.resolve()`` so a payload like ``data/../../../etc/cron.d`` does
    not pass the descendant check.

    The anchor is captured at module load (``_STARTUP_DATA_DIR`` /
    ``_STARTUP_DATA_PARENT``) so that an attacker who later mutates
    ``_state.config['storage']['data_dir']`` via a previous settings update
    cannot pivot the anchor to a directory of their choosing - the captured
    anchor reflects the on-disk YAML config at process start.
    """
    stripped = str(value or '').strip()
    if not stripped:
        raise HTTPException(status_code=400, detail=f'{key} cannot be blank.')
    candidate = Path(stripped).expanduser().resolve()
    # Best-effort rejection: refuse paths that DO NOT resolve to a real or
    # creatable filesystem location under the envelope. Path.resolve() does
    # NOT require the path to exist, so this guard treats non-existent
    # "future" paths the same as existing ones -- what matters is whether
    # the resolved location is within the captured envelope.
    if candidate == _STARTUP_DATA_DIR or _is_within(candidate, _STARTUP_DATA_DIR):
        return str(candidate)
    if _STARTUP_DATA_PARENT is not None and (
        candidate == _STARTUP_DATA_PARENT or _is_within(candidate, _STARTUP_DATA_PARENT)
    ):
        return str(candidate)
    raise HTTPException(
        status_code=400,
        detail=(
            f'{key} must be inside the application data directory '
            f'({_STARTUP_DATA_DIR}) or a sibling under its parent '
            f'({_STARTUP_DATA_PARENT}). Got: {value!r}.'
        ),
    )


def _is_within(candidate: Path, anchor: Path) -> bool:
    try:
        candidate.relative_to(anchor)
        return True
    except ValueError:
        return False


_STARTUP_DATA_DIR: Path = Path(
    str(_state.config.get('storage', {}).get('data_dir') or 'data')
).expanduser().resolve()
_STARTUP_DATA_PARENT: Path | None = (
    _STARTUP_DATA_DIR.parent
    if _STARTUP_DATA_DIR.parent != _STARTUP_DATA_DIR
    else None
)


def validate_storage_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_storage_config()
    updated = {key: str(current.get(key) or '') for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir', 'database')}
    for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir'):
        if key in payload:
            updated[key] = _resolve_within_data_envelope(
                str(payload.get(key) or '').strip(), key=key,
            )
    updated['database'] = str(_state.config.get('storage', {}).get('database') or updated.get('database') or 'data/daygle_ai_camera.sqlite3')
    return updated


def validate_auth_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_auth_config()
    merged = {**current, **payload}
    try:
        session_timeout_hours = float(merged.get('session_timeout_hours', 12))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='session_timeout_hours must be a number.') from exc
    if session_timeout_hours < 0.25 or session_timeout_hours > 720:
        raise HTTPException(status_code=400, detail='session_timeout_hours must be between 0.25 and 720.')
    try:
        rate_limit_base_delay = float(merged.get('rate_limit_base_delay', 2.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='rate_limit_base_delay must be a number.') from exc
    if rate_limit_base_delay < 0.5 or rate_limit_base_delay > 60.0:
        raise HTTPException(status_code=400, detail='rate_limit_base_delay must be between 0.5 and 60.')
    try:
        rate_limit_max_delay = float(merged.get('rate_limit_max_delay', 300.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='rate_limit_max_delay must be a number.') from exc
    if rate_limit_max_delay < 5 or rate_limit_max_delay > 3600:
        raise HTTPException(status_code=400, detail='rate_limit_max_delay must be between 5 and 3600.')
    return {
        'session_timeout_hours': session_timeout_hours,
        'max_login_attempts': _int_field(merged, 'max_login_attempts', 5, 1, 100),
        'lockout_minutes': _int_field(merged, 'lockout_minutes', 15, 1, 1440),
        'rate_limit_max_attempts': _int_field(merged, 'rate_limit_max_attempts', 5, 1, 100),
        'rate_limit_window_seconds': _int_field(merged, 'rate_limit_window_seconds', 60, 10, 3600),
        'rate_limit_base_delay': rate_limit_base_delay,
        'rate_limit_max_delay': rate_limit_max_delay,
    }


def validate_live_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_live_config()
    merged = {**current, **payload}
    snapshot_refresh_ms = _int_field(merged, 'snapshot_refresh_ms', 500, 150, 5000)
    detection_status_refresh_ms = _int_field(merged, 'detection_status_refresh_ms', 2000, 100, 15000)
    background_detection_enabled = normalize_bool_setting(merged.get('background_detection_enabled'), True)
    try:
        detection_interval_seconds = float(merged.get('detection_interval_seconds', 0.25))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='detection_interval_seconds must be a number.') from exc
    if detection_interval_seconds < 0.1 or detection_interval_seconds > 10:
        raise HTTPException(status_code=400, detail='detection_interval_seconds must be between 0.1 and 10.')
    try:
        event_debounce_seconds = float(merged.get('event_debounce_seconds', 10.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='event_debounce_seconds must be a number.') from exc
    if event_debounce_seconds < 0 or event_debounce_seconds > 300:
        raise HTTPException(status_code=400, detail='event_debounce_seconds must be between 0 and 300.')
    try:
        detection_history_minutes = int(float(merged.get('detection_history_minutes', 10)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='detection_history_minutes must be a whole number.') from exc
    if detection_history_minutes < 1 or detection_history_minutes > 120:
        raise HTTPException(status_code=400, detail='detection_history_minutes must be between 1 and 120.')
    motion_pixel_threshold = _int_field(merged, 'motion_pixel_threshold', 30, 1, 255)
    try:
        motion_gate_fraction = float(merged.get('motion_gate_fraction', 0.003))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='motion_gate_fraction must be a number.') from exc
    if not 0.0001 <= motion_gate_fraction <= 0.5:
        raise HTTPException(status_code=400, detail='motion_gate_fraction must be between 0.0001 and 0.5.')
    try:
        motion_scale_fraction = float(merged.get('motion_scale_fraction', 0.1))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='motion_scale_fraction must be a number.') from exc
    if not 0.001 <= motion_scale_fraction <= 1.0:
        raise HTTPException(status_code=400, detail='motion_scale_fraction must be between 0.001 and 1.0.')
    try:
        motion_background_alpha = float(merged.get('motion_background_alpha', 0.05))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='motion_background_alpha must be a number.') from exc
    if not 0.001 <= motion_background_alpha <= 0.5:
        raise HTTPException(status_code=400, detail='motion_background_alpha must be between 0.001 and 0.5.')
    periodic_scan_interval_seconds = _int_field(merged, 'periodic_scan_interval_seconds', 0, 0, 3600)
    motion_frame_width = _int_field(merged, 'motion_frame_width', 160, 40, 640)
    motion_frame_height = _int_field(merged, 'motion_frame_height', 120, 30, 480)
    ingest_frame_fps = _int_field(merged, 'ingest_frame_fps', 4, 1, 30)
    return {'snapshot_refresh_ms': snapshot_refresh_ms, 'detection_status_refresh_ms': detection_status_refresh_ms, 'detection_interval_seconds': detection_interval_seconds, 'event_debounce_seconds': event_debounce_seconds, 'background_detection_enabled': background_detection_enabled, 'detection_history_minutes': detection_history_minutes, 'motion_pixel_threshold': motion_pixel_threshold, 'motion_gate_fraction': round(motion_gate_fraction, 6), 'motion_scale_fraction': round(motion_scale_fraction, 4), 'motion_background_alpha': round(motion_background_alpha, 4), 'motion_frame_width': motion_frame_width, 'motion_frame_height': motion_frame_height, 'ingest_frame_fps': ingest_frame_fps, 'periodic_scan_interval_seconds': periodic_scan_interval_seconds}
