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

from typing import Any

from fastapi import HTTPException


def _int_field(payload: dict[str, Any], field: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(payload.get(field, default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f'{field} must be an integer.') from exc
    if value < minimum or value > maximum:
        raise HTTPException(status_code=400, detail=f'{field} must be between {minimum} and {maximum}.')
    return value


def validate_alert_email_settings(payload: dict[str, Any]) -> dict[str, Any]:
    from app.main import effective_email_alert_settings
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
    from app.main import effective_push_notification_settings, normalize_bool_setting
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
    from app.main import (normalize_bool_setting, normalize_camera_id, camera_default_name,
        default_camera_detection_settings, build_stream_url, normalize_label_list,
        normalize_monitoring_zones, _migrate_legacy_camera_motion,
        normalize_camera_recording_settings, normalize_camera_ptz_settings)
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
    if 'motion' in payload:
        raw_motion = payload.get('motion') if isinstance(payload.get('motion'), dict) else {}
        cam_motion: dict[str, Any] = {}
        if raw_motion.get('pixel_threshold') is not None:
            cam_motion['pixel_threshold'] = _int_field({'pixel_threshold': raw_motion['pixel_threshold']}, 'pixel_threshold', 30, 1, 255)
        for _key in ('gate_fraction', 'scale_fraction', 'background_alpha'):
            if raw_motion.get(_key) is not None:
                try:
                    cam_motion[_key] = round(float(raw_motion[_key]), 6)
                except (TypeError, ValueError):
                    pass
        if cam_motion:
            updated['motion'] = cam_motion
    elif current.get('motion'):
        updated['motion'] = current['motion']
    return updated


def validate_cameras_settings(payload: Any) -> list[dict[str, Any]]:
    import app.main as main
    raw_cameras = payload.get('cameras') if isinstance(payload, dict) else payload
    if not isinstance(raw_cameras, list):
        raise HTTPException(status_code=400, detail='cameras must be a list.')
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    current_by_id = {str(camera_settings.get('id')): camera_settings for camera_settings in main.cameras_config}
    for index, raw_camera in enumerate(raw_cameras, start=1):
        if not isinstance(raw_camera, dict):
            raise HTTPException(status_code=400, detail='Each camera must be an object.')
        current = current_by_id.get(str(raw_camera.get('id'))) or (main.cameras_config[index - 1] if index <= len(main.cameras_config) else {})
        camera_settings = validate_camera_settings(raw_camera, current=current, index=index)
        if camera_settings['id'] in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate camera id: {camera_settings['id']}.")
        seen.add(camera_settings['id'])
        validated.append(camera_settings)
    return validated


def validate_recording_settings(payload: dict[str, Any]) -> dict[str, Any]:
    from app.main import effective_recording_config, normalize_bool_setting
    current = effective_recording_config()
    merged = {**current, **payload}
    fmt = str(merged.get('format', 'mp4')).strip().lstrip('.').lower() or 'mp4'
    if fmt == 'avi':
        fmt = 'mp4'
    if fmt != 'mp4':
        raise HTTPException(status_code=400, detail='Recording format must be mp4 for browser playback.')
    return {'pre_event_seconds': _int_field(merged, 'pre_event_seconds', 10, 0, 300), 'post_event_seconds': _int_field(merged, 'post_event_seconds', 15, 0, 300), 'extension_step_seconds': _int_field(merged, 'extension_step_seconds', 45, 0, 300), 'max_clip_seconds': _int_field(merged, 'max_clip_seconds', 300, 1, 3600), 'format': fmt, 'chunk_duration_seconds': _int_field(merged, 'chunk_duration_seconds', 3600, 60, 86400), 'retention_days': _int_field(merged, 'retention_days', 14, 1, 3650), 'max_storage_gb': _int_field(merged, 'max_storage_gb', 20, 1, 100000), 'auto_purge_enabled': normalize_bool_setting(merged.get('auto_purge_enabled', True), True)}


def validate_storage_settings(payload: dict[str, Any]) -> dict[str, Any]:
    from app.main import config, effective_storage_config
    current = effective_storage_config()
    updated = {key: str(current.get(key) or '') for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir', 'database')}
    for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir'):
        if key in payload:
            value = str(payload.get(key) or '').strip()
            if not value:
                raise HTTPException(status_code=400, detail=f'{key} cannot be blank.')
            updated[key] = value
    updated['database'] = str(config.get('storage', {}).get('database') or updated.get('database') or 'data/daygle_ai_camera.sqlite3')
    return updated


def validate_auth_settings(payload: dict[str, Any]) -> dict[str, Any]:
    import app.main as main
    current = main.effective_auth_config()
    merged = {**current, **payload}
    try:
        session_timeout_hours = float(merged.get('session_timeout_hours', 12))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='session_timeout_hours must be a number.') from exc
    if session_timeout_hours < 0.25 or session_timeout_hours > 720:
        raise HTTPException(status_code=400, detail='session_timeout_hours must be between 0.25 and 720.')
    return {'session_timeout_hours': session_timeout_hours, 'max_login_attempts': _int_field(merged, 'max_login_attempts', 5, 1, 100), 'lockout_minutes': _int_field(merged, 'lockout_minutes', 15, 1, 1440)}


def validate_live_settings(payload: dict[str, Any]) -> dict[str, Any]:
    import app.main as main
    current = main.effective_live_config()
    merged = {**current, **payload}
    snapshot_refresh_ms = _int_field(merged, 'snapshot_refresh_ms', 500, 150, 5000)
    detection_status_refresh_ms = _int_field(merged, 'detection_status_refresh_ms', 2000, 100, 15000)
    background_detection_enabled = main.normalize_bool_setting(merged.get('background_detection_enabled'), True)
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
    return {'snapshot_refresh_ms': snapshot_refresh_ms, 'detection_status_refresh_ms': detection_status_refresh_ms, 'detection_interval_seconds': detection_interval_seconds, 'event_debounce_seconds': event_debounce_seconds, 'background_detection_enabled': background_detection_enabled, 'detection_history_minutes': detection_history_minutes, 'motion_pixel_threshold': motion_pixel_threshold, 'motion_gate_fraction': round(motion_gate_fraction, 6), 'motion_scale_fraction': round(motion_scale_fraction, 4), 'motion_background_alpha': round(motion_background_alpha, 4), 'periodic_scan_interval_seconds': periodic_scan_interval_seconds}
