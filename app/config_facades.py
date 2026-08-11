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
from app.settings import DEFAULT_CONFIG


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
def _normalize_shadow_suppression(value: Any) -> str:
    """Return the canonical persisted value for the tri-state shadow setting."""
    if isinstance(value, bool):
        return 'on' if value else 'off'
    normalized = str(value or '').strip().lower()
    return normalized if normalized in {'on', 'off', 'auto'} else 'on'


DEFAULT_RECORDING_CONFIG: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG['recording'])
DEFAULT_STORAGE_CONFIG: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG['storage'])
DEFAULT_AUTH_CONFIG: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG['auth'])
DEFAULT_EMAIL_ALERT_SETTINGS: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG['alerts']['email'])
DEFAULT_PUSH_NOTIFICATION_SETTINGS: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG['alerts']['push_notification'])


DEFAULT_LIVE_CONFIG: dict[str, Any] = {
    'snapshot_refresh_ms': 500,
    'detection_status_refresh_ms': 2000,
    'detection_interval_seconds': 0.5,
    'event_debounce_seconds': 10.0,
    # Temporal confirmation gate (all object labels). ``1`` = disabled
    # (single-frame behavior); higher requires the label to persist across
    # ``detection_confirm_frames`` of the last ``detection_confirm_window``
    # detection cycles before it can alert or record. Defaults to 2-of-3: it
    # pairs with the always-on object detector (below) to suppress the one-frame
    # false positives that running YOLO every cycle on a static scene can
    # produce. Set to 1 for instant single-frame alerts (lower latency, more
    # false positives).
    'detection_confirm_frames': 2,
    'detection_confirm_window': 3,
    'background_detection_enabled': True,
    # When True (default), object (YOLO) inference runs every detection cycle
    # regardless of the motion gate -- maximum recall (a still/slow/low-contrast
    # subject is never hidden from the detector). Set False to restore the
    # CPU-saving motion gate (object inference only when motion fires) on
    # hardware without the headroom to run YOLO continuously. Motion detection is
    # a separate, always-on signal either way.
    'always_run_object_detection': True,
    # When True, after the full-frame YOLO pass the detector is re-run zoomed
    # into the moving regions (from the motion diff mask) so small/distant
    # subjects are recovered, then merged + de-duplicated. Off by default: it
    # multiplies inference cost by the number of motion regions and is best
    # enabled per deployment after validating on real footage.
    'object_detection_region_boost': False,
    # Tiled / sliced inference: 'off' (default), or a grid like '2x2' / '3x3' /
    # '4x4'. When set, the detector is additionally run on every overlapping tile
    # of the grid each cycle, recovering SMALL subjects anywhere in frame --
    # including stationary ones the motion-region boost never sees -- at the cost
    # of one inference per tile. Best on cameras covering a large/deep area.
    'object_detection_tiling': 'off',
    'detection_history_minutes': 10,
    # Background-subtraction engine: 'mog2' (default, Gaussian-mixture with
    # shadow rejection) or 'diff' (legacy single-frame adaptive diff / automatic
    # fallback when the OpenCV build lacks MOG2).
    'motion_algorithm': 'mog2',
    # Morphological denoise of the foreground mask (removes single-pixel noise).
    'motion_denoise': True,
    # Reject MOG2-classified cast shadows so a moving shadow is not motion.
    # Tri-state: 'on' (always), 'off' (never), 'auto' (only while the scene is
    # bright; disabled at night/IR so real subjects are not read as shadow).
    'motion_shadow_suppression': 'on',
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
    settings = copy.deepcopy(DEFAULT_RECORDING_CONFIG)
    config_recording = _state.config.get('recording', {})
    if isinstance(config_recording, dict):
        settings.update(config_recording)
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
    # Keep the value returned to the settings page canonical. This preserves
    # the tri-state 'auto' choice and migrates older boolean rows without
    # allowing a malformed config value to appear as a blank/select fallback.
    settings['motion_shadow_suppression'] = _normalize_shadow_suppression(
        settings.get('motion_shadow_suppression')
    )
    return settings


def effective_storage_config() -> dict[str, Any]:
    settings = copy.deepcopy(DEFAULT_STORAGE_CONFIG)
    config_storage = _state.config.get('storage', {})
    if isinstance(config_storage, dict):
        settings.update(config_storage)
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
    settings = copy.deepcopy(DEFAULT_AUTH_CONFIG)
    config_auth = _state.config.get('auth', {})
    if isinstance(config_auth, dict):
        settings.update(config_auth)
    # ``auth_config`` is kept as a startup snapshot for compatibility with
    # callers that inspect it directly; include it here as a final startup
    # layer in case a test or embedding host populates it independently.
    if isinstance(_state.auth_config, dict):
        settings.update(_state.auth_config)
    override = _database_setting('auth')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_email_alert_settings() -> dict[str, Any]:
    settings = copy.deepcopy(DEFAULT_EMAIL_ALERT_SETTINGS)
    config_alerts = _state.config.get('alerts', {})
    if isinstance(config_alerts, dict) and isinstance(config_alerts.get('email'), dict):
        settings.update(config_alerts['email'])
    override = _database_setting('alert_email')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_push_notification_settings() -> dict[str, Any]:
    settings = copy.deepcopy(DEFAULT_PUSH_NOTIFICATION_SETTINGS)
    config_alerts = _state.config.get('alerts', {})
    if isinstance(config_alerts, dict) and isinstance(config_alerts.get('push_notification'), dict):
        settings.update(config_alerts['push_notification'])
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
