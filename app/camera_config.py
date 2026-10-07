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
- ``redact_camera_secrets`` -- stricter redaction for responses that
  cross a role boundary to viewer accounts: ``_redact_camera`` plus
  masking of credentials embedded in ``stream_url``.

The Pool A rebinds in ``app/main.py`` (top-of-file, after the
``from app.storage import Storage`` import) wire
``main.<name> = camera_config.<name>`` so existing routers calling
``main.normalize_camera_id(...)`` continue to resolve to these
implementations with no source edits.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import app.state as _state
from app.camera_id import camera_storage_key, normalize_camera_id  # re-export
from app.recording_settings import (
    _migrate_legacy_camera_motion,
    _normalize_camera_sound_settings,
    apply_active_camera_detection_profile,
    normalize_camera_ptz_settings,
    normalize_camera_recording_settings,
)
from app.utils import (
    camera_default_name,
    default_camera_detection_settings,
    normalize_ptz_motion_detection,
)
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
    raw_fps = camera_settings.get('fps')
    if raw_fps is None or (isinstance(raw_fps, str) and not raw_fps.strip()):
        camera_settings['fps'] = None
    else:
        camera_settings['fps'] = int(raw_fps)
    camera_settings['timezone'] = str(camera_settings.get('timezone') or 'UTC').strip() or 'UTC'
    for _location_key, _low, _high in (
        ('latitude', -90.0, 90.0),
        ('longitude', -180.0, 180.0),
    ):
        try:
            _location_value = float(camera_settings.get(_location_key))
            camera_settings[_location_key] = round(max(_low, min(_high, _location_value)), 6)
        except (TypeError, ValueError):
            camera_settings[_location_key] = None
    # Match the ``fps`` handling above: an empty/whitespace string (the form's
    # blank "Auto" value, or a restored/legacy config) means "no override" ->
    # None, rather than reaching ``int('')`` and raising ValueError out of every
    # ``effective_cameras_config`` read.
    raw_stale = camera_settings.get('stale_frame_grabs')
    if raw_stale is None or (isinstance(raw_stale, str) and not raw_stale.strip()):
        camera_settings['stale_frame_grabs'] = None
    else:
        camera_settings['stale_frame_grabs'] = int(raw_stale)
    detection = default_camera_detection_settings()
    if isinstance(camera_settings.get('detection'), dict):
        detection.update(camera_settings['detection'])
    detection['object_detection_enabled'] = bool(
        detection.get('object_detection_enabled', True)
    )
    detection['ptz_motion_detection'] = normalize_ptz_motion_detection(
        detection.get('ptz_motion_detection'),
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
    # Per-camera YOLO model assignment (app/camera_models.py). Tolerant on
    # read: an invalid stored override is dropped (self-heals to the global
    # default detector) rather than breaking every effective_cameras_config
    # read. Valid overrides are re-canonicalised to the project-relative form.
    from app.camera_models import normalize_camera_labels_path, normalize_camera_model_path
    _model_path = normalize_camera_model_path(detection.get('model_path'))
    if _model_path is None:
        detection.pop('model_path', None)
        detection.pop('labels_path', None)
    else:
        detection['model_path'] = _model_path
        _labels_path = normalize_camera_labels_path(detection.get('labels_path'))
        if _labels_path is None:
            detection.pop('labels_path', None)
        else:
            detection['labels_path'] = _labels_path
    _migrate_legacy_camera_motion(detection)
    camera_settings['detection'] = detection
    camera_settings['recording'] = normalize_camera_recording_settings(
        camera_settings.get('recording'),
    )
    camera_settings['ptz'] = normalize_camera_ptz_settings(
        camera_settings.get('ptz'),
    )
    # Migrate legacy nested motion override dict to flat motion_* keys so
    # per-camera overrides use the same naming convention as global live settings.
    _legacy_cam_motion = camera_settings.pop('motion', None)
    if isinstance(_legacy_cam_motion, dict):
        for _flat_key, _short_key in (
            ('motion_pixel_threshold', 'pixel_threshold'),
            ('motion_gate_fraction', 'gate_fraction'),
            ('motion_scale_fraction', 'scale_fraction'),
            ('motion_background_alpha', 'background_alpha'),
        ):
            if camera_settings.get(_flat_key) is None and _legacy_cam_motion.get(_short_key) is not None:
                camera_settings[_flat_key] = _legacy_cam_motion[_short_key]
    apply_active_camera_detection_profile(camera_settings)
    return camera_settings


def camera_id_renames(
    old_configs: list[dict[str, Any]],
    new_settings: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Return unambiguous ``(old_id, new_id)`` camera-id renames between two
    camera lists.

    ``update_cameras`` receives the WHOLE new camera list alongside the current
    one. Pairing the two **positionally** (``zip``) is wrong the moment the lists
    differ in length: adding, removing, or reordering a camera shifts every
    later entry, so a delete would pair an unrelated old/new camera and
    "migrate" one camera's live-detection state and on-disk ingest dirs onto
    another -- silently corrupting them.

    A rename is therefore acted on only when it is unambiguous: exactly one id
    disappears from the old list and exactly one new id appears. Adds (nothing
    disappears), deletes (nothing appears), enable/disable toggles (no id
    change), and reorders (same id set) all yield no pairs and thus no
    migration. Multiple renames in a single save are deliberately not guessed
    at either -- their ids stay unmigrated (state is re-keyed fresh on the next
    cycle) rather than being paired incorrectly.
    """
    old_ids = [str(cfg.get('id') or '') for cfg in old_configs]
    new_ids = [str(cfg.get('id') or '') for cfg in new_settings]
    old_set = {i for i in old_ids if i}
    new_set = {i for i in new_ids if i}
    # dict.fromkeys preserves first-seen order while de-duplicating, so a
    # repeated id cannot make a single rename look like several.
    removed = [i for i in dict.fromkeys(old_ids) if i and i not in new_set]
    added = [i for i in dict.fromkeys(new_ids) if i and i not in old_set]
    if len(removed) == 1 and len(added) == 1:
        return [(removed[0], added[0])]
    return []


def _migrate_camera_id(old_id: str, new_id: str) -> None:
    """Rename ``old_id`` -> ``new_id`` across in-memory state and on-disk
    ingest dirs in one lock-protected sweep.

    Called by ``app/api/cameras_router.py::update_cameras`` when an
    operator renames a camera's id; tolerates missing / colliding
    targets by either popping in-memory state or skipping the rename
    if the destination dir already exists (the latter guards against
    silently clobbering an unrelated camera's frames).
    """
    old_key = camera_storage_key(old_id)
    new_key = camera_storage_key(new_id)
    with _state.live_detection_history_lock:
        if old_id in _state.live_detection_history:
            _state.live_detection_history[new_id] = (
                _state.live_detection_history.pop(old_id)
            )
    from app.detection_state import frame_motion_locks
    # Lock every stripe in stable order so active detection cannot race a move.
    with frame_motion_locks():
        for mapping_name in (
            '_frame_motion_prev',
            '_frame_motion_last_frame',
            '_frame_motion_last_gray',
            '_frame_motion_mog2',
            '_frame_motion_mog2_meta',
            '_frame_motion_scene_streak',
        ):
            mapping = getattr(_state, mapping_name)
            if old_id in mapping:
                mapping[new_id] = mapping.pop(old_id)
        if old_id in _state._frame_motion_error_cameras:
            _state._frame_motion_error_cameras.discard(old_id)
            _state._frame_motion_error_cameras.add(new_id)
    with _state._apply_settings_lock:
        service = _state.recording_service
        if service is not None:
            for base in (
                service.prebuffer_dir,
                service.frames_dir,
                service.audio_dir,
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


def redact_camera_secrets(cam: dict[str, Any]) -> dict[str, Any]:
    """Strict redaction for camera records served across role boundaries.

    ``_redact_camera`` covers the operator-facing cameras endpoints where an
    admin's own UI needs the stored fields back. This stricter variant is for
    responses that reach **viewer-role** accounts (``/api/config``, the
    viewer branch of ``GET /api/cameras``): on top of the password strip it
    masks any credentials EMBEDDED in ``stream_url`` (e.g.
    ``rtsp://user:secret@cam/stream``), which the stream-source validator
    accepts and which would otherwise echo the camera password verbatim to
    every signed-in user. The masked URL keeps scheme/host/port/path so
    non-admin consumers still see which camera the record describes; the
    stored configuration is never modified.
    """
    out = _redact_camera(cam)
    stream_url = str(out.get('stream_url') or '')
    if stream_url:
        try:
            parsed = urlsplit(stream_url)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.username is not None:
            out['has_stream_url_credentials'] = True
            netloc = parsed.hostname or ''
            # A bare IPv6 literal must stay bracketed in the rebuilt authority
            # (mirrors ``build_stream_url``) or colons read as port separators.
            if ':' in netloc and not netloc.startswith('['):
                netloc = f'[{netloc}]'
            if parsed.port is not None:
                netloc = f'{netloc}:{parsed.port}'
            out['stream_url'] = urlunsplit((
                parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment,
            ))
        else:
            out['has_stream_url_credentials'] = False
    else:
        out['has_stream_url_credentials'] = False
    return out
