"""Camera-config helpers extracted from ``app/main.py`` (Phase-18).

The 4 helpers shipped here cluster around camera-id normalization,
camera-settings orchestration with defaulting/migration, mid-stream
camera-id renaming (with on-disk ingest-dir migration), and credential
redaction in API responses.

Like ``app/auth_gates.py`` (Phase-16) and ``app/config_facades.py``
(Phase-17), these are extracted with the **hybrid-pattern template**:
helpers reach ``main.<attr>`` for their cross-module dependencies at
*call time* (not import time), so they continue to work seamlessly
when ``app/main.py`` is partially loaded during the Pool A rebind loop.

Cluster membership:

- ``normalize_camera_id`` -- regex-based id normaliser used by
  ``camera_router`` (camera list/update) and ``recordings_router``
  (selected camera id).
- ``normalize_camera_settings`` -- full settings normaliser that layers
  defaults, recursively normalises nested ``detection`` / ``recording``
  / ``ptz`` sub-dicts, and migrates legacy motion fields into the
  current zones/object_rules schema.
- ``_migrate_camera_id`` -- runtime helper used by ``update_cameras``
  when an operator renames a camera's id; carries the in-memory live
  detection/motion state plus the on-disk ingest dirs from old id to
  new id under a single lock-protected sweep.
- ``_redact_camera`` -- strips the ``password`` field from a camera
  record before sending it over the wire, replacing it with a
  ``has_password`` boolean for UI hints.

The Pool A rebinds in ``app/main.py`` (top-of-file, after the
``from app.storage import Storage`` import) wire
``main.<name> = camera_config.<name>`` so existing routers calling
``main.normalize_camera_id(...)`` continue to resolve to these
implementations with no source edits.
"""

from __future__ import annotations

import logging
from typing import Any

import app.state as _state
from app.camera_id import normalize_camera_id as normalize_camera_id  # noqa: PLC0414  re-export
from app.recording_settings import (
    _migrate_legacy_camera_motion,
    _normalize_camera_sound_settings,
    normalize_camera_ptz_settings,
    normalize_camera_recording_settings,
)
from app.recordings import RecordingService
from app.utils import camera_default_name, default_camera_detection_settings
from app.zone_schema import normalize_label_list, normalize_monitoring_zones

logger = logging.getLogger('daygle.ai')


def normalize_camera_settings(
    settings: dict[str, Any],
    index: int = 1,
) -> dict[str, Any]:
    camera_settings = dict(settings or {})
    camera_settings['id'] = normalize_camera_id(
        camera_settings.get('id'),
        f'camera-{index}',
    )
    camera_settings['name'] = camera_default_name(
        camera_settings,
        f'Camera {index}',
    )
    camera_settings['backend'] = str(
        camera_settings.get('backend') or 'onvif'
    ).lower()
    camera_settings['width'] = int(camera_settings.get('width') or 1280)
    camera_settings['height'] = int(camera_settings.get('height') or 720)
    camera_settings['fps'] = int(camera_settings.get('fps') or 15)
    raw_stale = camera_settings.get('stale_frame_grabs')
    camera_settings['stale_frame_grabs'] = (
        int(raw_stale) if raw_stale is not None else None
    )
    detection = default_camera_detection_settings()
    if isinstance(camera_settings.get('detection'), dict):
        detection.update(camera_settings['detection'])
    detection['object_detection_enabled'] = bool(
        detection.get('object_detection_enabled', True)
    )
    detection['object_labels'] = normalize_label_list(
        detection.get('object_labels', []),
    )
    detection['zones'] = normalize_monitoring_zones(
        detection.get('zones', []),
    )
    detection['sound'] = _normalize_camera_sound_settings(
        detection.get('sound'),
    )
    _migrate_legacy_camera_motion(detection)
    camera_settings['detection'] = detection
    camera_settings['recording'] = normalize_camera_recording_settings(
        camera_settings.get('recording'),
    )
    camera_settings['ptz'] = normalize_camera_ptz_settings(
        camera_settings.get('ptz'),
    )
    return camera_settings


def _migrate_camera_id(old_id: str, new_id: str) -> None:
    """Rename ``old_id`` -> ``new_id`` across in-memory state and on-disk
    ingest dirs in one lock-protected sweep.

    Called by ``app/api/cameras_router.py::update_cameras`` when an
    operator renames a camera's id; tolerates missing / colliding
    targets by either popping in-memory state or skipping the rename
    if the destination dir already exists (the latter guards against
    silently clobbering an unrelated camera's frames).
    """
    old_key = RecordingService._camera_key(old_id)
    new_key = RecordingService._camera_key(new_id)
    with _state.live_detection_history_lock:
        if old_id in _state.live_detection_history:
            _state.live_detection_history[new_id] = (
                _state.live_detection_history.pop(old_id)
            )
    with _state._frame_motion_lock:
        if old_id in _state._frame_motion_prev:
            _state._frame_motion_prev[new_id] = (
                _state._frame_motion_prev.pop(old_id)
            )
    if _state.recording_service is not None:
        for base in (
            _state.recording_service.prebuffer_dir,
            _state.recording_service.frames_dir,
            _state.recording_service.audio_dir,
        ):
            old_dir = base / old_key
            new_dir = base / new_key
            if old_dir.exists() and (not new_dir.exists()):
                try:
                    old_dir.rename(new_dir)
                except OSError as exc:
                    logger.warning(
                        'Could not rename ingest dir %s \u2192 %s: %s',
                        old_dir,
                        new_dir,
                        exc,
                    )


def _redact_camera(cam: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``cam`` with the ``password`` field stripped and
    replaced by a ``has_password`` boolean, so API responses never echo
    raw ONVIF credentials back to the operator UI.
    """
    out = {k: v for k, v in cam.items() if k != 'password'}
    out['has_password'] = bool(cam.get('password'))
    return out
