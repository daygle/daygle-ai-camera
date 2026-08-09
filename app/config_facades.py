"""Config-facade helpers that merge on-disk config with database overrides.

Each of the 9 functions merges a runtime snapshot from
``_state.database.get_setting(<name>)`` with ``_state.config`` (and for
some helpers a hardcoded default dict), returning a single flattened dict
routers can consume directly.

Functions:

- ``effective_ai_config`` - merges ``config['ai']`` with the ``'ai'``
  database override; ``copy.deepcopy`` prevents callers from mutating
  the source dict.
- ``effective_recording_config`` - same shape as AI.
- ``effective_live_config`` - layers ``DEFAULT_LIVE_CONFIG`` defaults,
  then ``config['live']``, then the ``'live'`` database override.
- ``effective_storage_config`` - merges ``config['storage']`` with the
  ``'storage'`` database override but always preserves the startup
  ``database`` path (the DB file path must not be hot-reloaded).
- ``effective_auth_config`` - merges ``_state.auth_config`` with the
  ``'auth'`` database override.
- ``effective_email_alert_settings`` - merges ``config['alerts']['email']``
  with the ``'alert_email'`` database override.
- ``effective_push_notification_settings`` - merges
  ``config['alerts']['push_notification']`` with the ``'alert_push'``
  database override.
- ``effective_cameras_config`` - reads the ``'cameras'`` database
  override only; normalizes each entry via ``normalize_camera_settings``.
  Returns ``[]`` when no override is present (cameras are managed via
  the ``/api/cameras`` mutators, not the on-disk config).
- ``get_camera_config`` - resolves a camera's runtime config dict by id
  from ``_state.cameras_config``; falls back to ``_state.camera_config``
  when the runtime list is empty; raises ``HTTPException(404)`` when the
  list is non-empty but the requested id is missing.
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.camera_config import normalize_camera_id, normalize_camera_settings


def _database_setting(key: str) -> Any:
    """Safely read a setting from the database when it is initialised.

    During startup (and in some test paths) ``_state.database`` may still be
    ``None`` when background threads first call into the config facades.
    Returning ``None`` lets the caller fall back to the on-disk config or
    hard-coded defaults, avoiding ``AttributeError`` races.
    """
    db = _state.database
    if db is None:
        return None
    return db.get_setting(key)


# Source of truth for the live-config defaults. Kept as a module
# constant (not module-level on main.py) because it is purely a
# default-set under the extract's responsibility; promoting it out of
# the function body makes it greppable + trivially overridable in a
# future Phase-17+ test.
DEFAULT_LIVE_CONFIG: dict[str, Any] = {
    'snapshot_refresh_ms': 500,
    'detection_status_refresh_ms': 2000,
    'detection_interval_seconds': 0.5,
    'event_debounce_seconds': 10.0,
    # Temporal confirmation gate (all object labels). ``1`` = disabled
    # (single-frame behavior); higher requires the label to persist across
    # ``detection_confirm_frames`` of the last ``detection_confirm_window``
    # detection cycles before it can alert or record.
    'detection_confirm_frames': 1,
    'detection_confirm_window': 3,
    'background_detection_enabled': True,
    'detection_history_minutes': 10,
    'motion_pixel_threshold': 30,
    'motion_gate_fraction': 0.005,
    'motion_scale_fraction': 0.03,
    'motion_background_alpha': 0.05,
    'motion_frame_width': 320,
    'motion_frame_height': 240,
    'ingest_frame_fps': 4,
    'snapshot_quality': 2,
    'periodic_scan_interval_seconds': 0,
}


def effective_ai_config() -> dict[str, Any]:
    settings = copy.deepcopy(_state.config.get('ai', {}))
    override = _database_setting('ai')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_recording_config() -> dict[str, Any]:
    settings = copy.deepcopy(_state.config.get('recording', {}))
    override = _database_setting('recording')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_live_config() -> dict[str, Any]:
    settings = copy.deepcopy(DEFAULT_LIVE_CONFIG)
    config_live = _state.config.get('live', {})
    if isinstance(config_live, dict):
        settings.update(config_live)
    override = _database_setting('live')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_storage_config() -> dict[str, Any]:
    settings = copy.deepcopy(_state.config.get('storage', {}))
    override = _database_setting('storage')
    if isinstance(override, dict):
        database_path = settings.get('database')
        settings.update(override)
        # The on-disk DB path is set at startup and must NOT be
        # hot-reloadable; preserve it from the source dict even if the
        # override attempted to set a different value.
        settings['database'] = database_path
    return settings


def effective_auth_config() -> dict[str, Any]:
    settings = copy.deepcopy(_state.auth_config)
    override = _database_setting('auth')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_email_alert_settings() -> dict[str, Any]:
    settings = copy.deepcopy(_state.config.get('alerts', {}).get('email', {}))
    override = _database_setting('alert_email')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_push_notification_settings() -> dict[str, Any]:
    settings = copy.deepcopy(_state.config.get('alerts', {}).get('push_notification', {}))
    override = _database_setting('alert_push')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_cameras_config() -> list[dict[str, Any]]:
    override = _database_setting('cameras')
    if isinstance(override, list) and override:
        return [
            normalize_camera_settings(camera_settings, index)
            for index, camera_settings in enumerate(override, start=1)
        ]
    return []


def get_camera_config(camera_id: str | None = None) -> dict[str, Any]:
    if not _state.cameras_config:
        return _state.camera_config
    if camera_id:
        normalized = normalize_camera_id(camera_id)
        for configured in _state.cameras_config:
            if configured.get('id') == normalized:
                return configured
        raise HTTPException(status_code=404, detail='Camera not found')
    return _state.cameras_config[0]
