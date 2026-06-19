"""Config-facade functions extracted from ``app/main.py`` (Phase-17).

Phase-17 carries the Phase-16 audit forward: after the auth-gate cluster
(4 helpers, ~19 cross-router reach sites) was extracted to
``app/middleware.py`` and ``app/auth_gates.py``, the next-highest-ROI
group of helpers in ``app/main.py`` is the config-facade cluster.

The 7 functions in this module each merge a runtime config snapshot
from ``database.get_setting(<name>)`` with the on-disk ``config``
dictionary and (for some) a hardcoded default set, returning a single
flattened dict the routers can consume directly without rewriting
their access pattern.

Same hybrid-pattern template as ``app/middleware.py`` (Phase-15) and
``app/auth_gates.py`` (Phase-16)::

    import app.main as main
    # helpers reach main.<attr> at CALL time, not at module top

The 18 reach sites across the 7 helpers resolve to ``main.<attr>``
attributes on ``app.main``: (``config``, ``auth_config``, ``database``,
``cameras_config``, ``camera_config``, ``normalize_camera_settings``,
``normalize_camera_id``). They stay on ``app.main`` (transport-level +
storage state + normalization helpers) so this module can read them
via Pool C.

Helpers moved (7):

- ``effective_ai_config`` - merges on-disk ``config['ai']`` with
  ``database.get_setting('ai')``; ``copy.deepcopy`` ensures callers
  cannot mutate the source ``config`` dict.
- ``effective_recording_config`` - same shape as AI.
- ``effective_live_config`` - hardcoded ``DEFAULT_LIVE_CONFIG``
  defaults first, then ``config['live']``, then ``database`` override.
  (No ``copy.deepcopy`` because the local ``settings`` is a fresh
  dict literal on every call.)
- ``effective_storage_config`` - merges ``config['storage']`` with the
  database override but PRESERVES ``settings['database']`` from the
  source dict even if the override contains a different value (the DB
  path is set at startup and must not be hot-reloaded).
- ``effective_auth_config`` - merges ``auth_config`` (a stripped
  pre-computed copy of ``config['auth']`` produced at module load for
  use during ``auth = AuthService(...)`` startup) with
  ``database.get_setting('auth')``.
- ``effective_cameras_config`` - reads only the database override; if
  present, normalizes each entry via ``main.normalize_camera_settings``.
  Empty list when no override is present (cameras are managed via the
  PUT/POST ``/api/cameras`` mutators, not the on-disk config).
- ``get_camera_config`` - resolves a camera's runtime config dict by
  id from ``main.cameras_config``. Falls back to ``main.camera_config``
  if the runtime list is empty. Raises ``HTTPException(404)`` when
  the runtime list is non-empty but the requested id is missing.

State KEPT on ``app.main`` (this module reads via ``main.<attr>``):

- ``main.config`` - the on-disk YAML-derived config dict. Static;
  never mutated at runtime.
- ``main.auth_config`` - a pre-stripped snapshot of ``config['auth']``
  produced at module load, used during AuthService init AND by
  ``effective_auth_config``.
- ``main.database`` - the EventDatabase singleton. All overrides
  route through its ``get_setting(name)`` calls.
- ``main.cameras_config`` - the runtime list of camera configs
  maintained by the cameras router / admin path. Mutated in place by
  the router after hot reloads.
- ``main.camera_config`` - singular fallback default-camera config
  used when ``cameras_config`` is empty (single-camera legacy setups).
- ``main.normalize_camera_settings`` - normalizer helper called by
  ``effective_cameras_config`` for each entry from the database
  override.
- ``main.normalize_camera_id`` - id-string normalizer called by
  ``get_camera_config`` to fuzzy-match incoming ``camera_id`` strings
  to camera records.

Pool A from-import rebinds (at the bottom of ``app/main.py``)::

    from app.config_facades import (
        effective_ai_config as effective_ai_config,
        effective_auth_config as effective_auth_config,
        effective_cameras_config as effective_cameras_config,
        effective_live_config as effective_live_config,
        effective_recording_config as effective_recording_config,
        effective_storage_config as effective_storage_config,
        get_camera_config as get_camera_config,
    )

These preserve the back-compat contract that lets every consumer
router continue calling ``main.<name>(...)`` without rewriting imports.
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi import HTTPException

import app.main as main


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
    'background_detection_enabled': True,
    'detection_history_minutes': 10,
    'motion_pixel_threshold': 30,
    'motion_gate_fraction': 0.003,
    'motion_scale_fraction': 0.1,
    'motion_background_alpha': 0.05,
    'periodic_scan_interval_seconds': 0,
}


def effective_ai_config() -> dict[str, Any]:
    settings = copy.deepcopy(main.config.get('ai', {}))
    override = main.database.get_setting('ai')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_recording_config() -> dict[str, Any]:
    settings = copy.deepcopy(main.config.get('recording', {}))
    override = main.database.get_setting('recording')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_live_config() -> dict[str, Any]:
    settings = copy.deepcopy(DEFAULT_LIVE_CONFIG)
    config_live = main.config.get('live', {})
    if isinstance(config_live, dict):
        settings.update(config_live)
    override = main.database.get_setting('live')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_storage_config() -> dict[str, Any]:
    settings = copy.deepcopy(main.config.get('storage', {}))
    override = main.database.get_setting('storage')
    if isinstance(override, dict):
        database_path = settings.get('database')
        settings.update(override)
        # The on-disk DB path is set at startup and must NOT be
        # hot-reloadable; preserve it from the source dict even if the
        # override attempted to set a different value.
        settings['database'] = database_path
    return settings


def effective_auth_config() -> dict[str, Any]:
    settings = copy.deepcopy(main.auth_config)
    override = main.database.get_setting('auth')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_cameras_config() -> list[dict[str, Any]]:
    override = main.database.get_setting('cameras')
    if isinstance(override, list) and override:
        return [
            main.normalize_camera_settings(camera_settings, index)
            for index, camera_settings in enumerate(override, start=1)
        ]
    return []


def get_camera_config(camera_id: str | None = None) -> dict[str, Any]:
    if not main.cameras_config:
        return main.camera_config
    if camera_id:
        normalized = main.normalize_camera_id(camera_id)
        for configured in main.cameras_config:
            if configured.get('id') == normalized:
                return configured
        raise HTTPException(status_code=404, detail='Camera not found')
    return main.cameras_config[0]
