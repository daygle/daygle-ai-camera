"""Camera and storage lifecycle helpers extracted from ``app/main.py``.

Cluster membership:
- ``camera_event_recording_config(settings)`` — build per-camera recording
  config dict merging global recording policy with per-camera overrides
- ``apply_cameras_settings(settings_list)`` — hot-swap camera instances on
  config change; calls ``apply_sound_settings`` as a side-effect
- ``apply_storage_and_recording_settings()`` — hot-swap Storage +
  RecordingService on storage/recording config change
- ``reload_detector(ai_settings)`` — hot-swap the AI detector while
  gracefully evicting the previous ONNX session from memory

All four functions are registered on ``app.state`` at module load so
extracted modules can call them via ``_state.<name>(...)`` without
importing ``app.main``.  ``app/main.py`` keeps Pool A re-exports so
routers and ``app/deps.py`` continue to reach them as ``main.<name>``.
"""
from __future__ import annotations

import gc
import logging
from typing import Any

import app.state as _state
from app.ai_settings import log_detector_initialization
from app.camera_instance import create_camera_instances
from app.config_facades import effective_recording_config, effective_storage_config
from app.detector import create_detector
from app.diagnostics import log_camera_diagnostic
from app.recording_settings import normalize_camera_recording_settings
from app.recordings import RecordingService
from app.sound_monitor import apply_sound_settings
from app.storage import Storage

logger = logging.getLogger('daygle.ai')


def camera_event_recording_config(settings: dict[str, Any]) -> dict[str, Any]:
    base = effective_recording_config()
    camera_recording = normalize_camera_recording_settings(settings.get('recording'))
    base.update({'continuous': camera_recording['continuous']})
    return base


def apply_cameras_settings(settings_list: list[dict[str, Any]]) -> None:
    new_instances = create_camera_instances(settings_list)
    with _state._camera_instances_lock:
        old_instances = _state.camera_instances
        _state.cameras_config = settings_list
        _state.camera_config = settings_list[0] if settings_list else {}
        _state.camera_instances = new_instances
        new_config = _state.camera_config
        _state.camera = new_instances[new_config['id']] if new_config else None
    for old_cam in (old_instances or {}).values():
        try:
            old_cam.close()
        except Exception as unexpected_exc:
            logger.warning('Unexpected error updating camera: %s', unexpected_exc)
    apply_sound_settings()


def apply_storage_and_recording_settings() -> None:
    _state.storage = Storage({**_state.config, 'storage': effective_storage_config()})
    old_service = _state.recording_service
    _state.recording_service = RecordingService({
        **_state.config,
        'storage': effective_storage_config(),
        'recording': effective_recording_config(),
    })
    _state.recording_service.diagnostic_callback = log_camera_diagnostic
    if old_service is not None:
        try:
            old_service.stop_prebuffer_workers()
            old_service.stop_all_continuous_recordings()
        except Exception as unexpected_exc:
            logger.warning('Unexpected error deleting camera: %s', unexpected_exc)


def reload_detector(ai_settings: dict[str, Any]) -> tuple[bool, str | None]:
    import app.alert_dispatch as _alert_dispatch
    _alert_dispatch._min_rule_confidence_cache = None
    previous_detector = _state.detector
    old_session = getattr(previous_detector, 'session', None)
    if old_session is not None:
        previous_detector.session = None
        del old_session
        gc.collect()
    candidate = create_detector(ai_settings)
    candidate_error = getattr(candidate, 'unavailable_reason', None)
    if ai_settings['backend'] == 'onnx' and (not getattr(candidate, 'available', False)):
        _state.detector = previous_detector
        _state.last_detector_error = candidate_error or 'Failed to load ONNX detector.'
        log_detector_initialization('reload_failed')
        return (False, _state.last_detector_error)
    _state.detector = candidate
    _state.last_detector_error = candidate_error
    log_detector_initialization('reload')
    return (True, _state.last_detector_error)


# Register callables on _state so extracted modules can call them without
# importing app.main (avoids circular deps and Pool C lazy imports).
_state.camera_event_recording_config = camera_event_recording_config
_state.apply_cameras_settings = apply_cameras_settings
_state.apply_storage_and_recording_settings = apply_storage_and_recording_settings
_state.reload_detector = reload_detector
