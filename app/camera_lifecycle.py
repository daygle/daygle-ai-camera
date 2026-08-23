"""Camera and storage lifecycle helpers extracted from ``app/main.py``.

Cluster membership:
- ``camera_event_recording_config(settings)`` - build per-camera recording
  config dict merging global recording policy with per-camera overrides
- ``apply_cameras_settings(settings_list)`` - hot-swap camera instances on
  config change; calls ``apply_sound_settings`` as a side-effect
- ``apply_storage_and_recording_settings()`` - hot-swap Storage +
  RecordingService on storage/recording config change
- ``reload_detector(ai_settings)`` - hot-swap the AI detector while
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


class _SentinelRecordingService:
    """Transient ``/dev/null`` recording service used by
    ``apply_storage_and_recording_settings`` during the swap window.

    Bug 6 follow-up: lives a few hundred milliseconds while the OLD
    ``RecordingService`` is being torn down. Every method/attribute access
    is intercepted by ``__getattr__`` and answered with a no-op credit:
    ``prime_rtsp_prebuffer`` / ``start_continuous_chunk_recording`` are
    no-ops, ``should_record`` returns ``(False, '', None)``, frame/audio
    queries return ``None``/empty, and attribute reads (e.g.
    ``prebuffer_dir``) return a benign default. The point is to make
    every dashboard / monitor call that lands on ``_state.recording_service``
    during the swap a graceful no-op rather than spawning fresh ffmpegs
    on the OLD service while it is mid-teardown.
    """

    _NO_OP_MARKER = '__sentinel_no_op__'

    def __getattr__(self, name: str) -> Any:
        # Recognize the special method names so every monitor-side call is
        # absorbed as a no-op rather than raising ``AttributeError``.
        if name.startswith('_') and name not in {'__class__', '__dict__'}:
            raise AttributeError(name)
        if name in {'diagnostic_callback'}:
            return None
        if name in {'prebuffer_dir', 'frames_dir', 'audio_dir'}:
            return ''
        return _SentinelRecordingService._no_op

    def should_record(self, *args: Any, **kwargs: Any) -> tuple[bool, str, str | None]:
        return (False, '', None)

    @staticmethod
    def _no_op(*args: Any, **kwargs: Any) -> Any:
        # Match the return-shape of the most-common monitor queries so the
        # caller can keep its current post-call control flow. Monitor
        # code never reads `prime_rtsp_prebuffer`'s return, so returning
        # ``None`` is sufficient.
        return None


class _SentinelDetector:
    """Transient ``/dev/null`` AI detector used by ``reload_detector``
    during the model hot-swap window.

    Bug 8 audit follow-up: the original ``reload_detector`` cleared the
    OLD detector's ``session`` attribute (via ``previous_detector.session
    = None; del old_session; gc.collect()``) BEFORE constructing the
    replacement. With the OLD still published at ``_state.detector`` for
    the duration of ``create_detector()``, concurrent pollers --
    ``live_alert_monitor_loop``'s per-poll ``detect_frame`` / ``ai_*
    status_payload``, ``/api/settings/ai/check-model``,
    ``/api/settings/ai/test-detector`` -- saw a detached ``session=None``
    detector and tripped ``AttributeError`` or ``DetectorUnavailableError``
    repeatedly. The rollback path (``_state.detector = previous_detector``
    on create_detector failure) ALSO re-published a session-less OLD,
    silently breaking inference until restart.

    The sentinel absorbs every poll: ``ai_status_payload()`` /
    ``detector_loaded_for(...)`` return ``detector_loaded=False`` because
    the sentinel's ``available`` is ``False``, which makes
    ``live_alert_monitor_loop`` early-exit via ``state='skipped'``
    (the documented "ONNX detector is not loaded" path) -- no warning
    storm. Direct callers that bypass the available-check get ``[]`` back
    from ``detect_frame`` / ``detect_image``, which downstream code
    treats as "no detections" (the correct observable behaviour during
    a model re-load).

    Mirror of ``_SentinelRecordingService`` in this same module.
    """

    backend = 'sentinel'

    def __init__(self, reason: str = 'AI detector is reloading; the new model is being loaded into memory.') -> None:
        # Class-level ``backend`` is mirrored on the instance too -- the OLD
        # ``OnnxYoloDetector`` exposes ``backend`` as a class attribute, so
        # duck-typed readers (e.g. ``ai_status_payload``) expect both lookup
        # paths to work for the published detector. Setting on instance keeps
        # any monkeypatch-level surprises localised.
        self.available = False
        self.unavailable_reason = reason
        self.session = None
        self.input_name = None
        self.output_names: list[str] = []
        self.active_providers: list[str] = []

    def detect_frame(self, image: Any, confidence: float | None = None) -> list[dict[str, Any]]:
        # Return an empty detection list rather than raising ``DetectorUnavailableError``
        # so callers that do NOT pre-check ``available`` (e.g. some tests,
        # future internal callers) get a clean "no detections" response
        # rather than an exception. ``live_alert_monitor_loop`` does
        # pre-check ``ai_status_payload()['detector_loaded']`` and exits
        # before ever calling ``detect_frame`` on the sentinel -- this
        # return is defensive for the post-check callers.
        return []

    def detect_image(self, image_bytes: bytes, confidence: float | None = None) -> list[dict[str, Any]]:
        return []





def camera_event_recording_config(settings: dict[str, Any]) -> dict[str, Any]:
    base = effective_recording_config()
    camera_recording = normalize_camera_recording_settings(settings.get('recording'))
    base.update({'continuous': camera_recording['continuous']})
    return base


def _cleanup_camera_runtime_state(removed_ids: set[str]) -> None:
    """Drop per-camera in-memory state for cameras removed from the config.

    The live monitor keeps a handful of per-camera dicts (rolling detection
    history, motion models, debounce/backoff state, dwell streaks, face
    identity caches, ...). Each is individually bounded by design, but an
    entry for a deleted camera would otherwise stay for the life of the
    process; a camera id that is deleted and re-added repeatedly (or churned
    via renames) would grow those dicts without bound. ``apply_cameras_settings``
    already stops the removed cameras' ffmpeg workers and pops the health
    state; this clears the rest of the runtime state under each dict's lock.
    """
    if not removed_ids:
        return
    with _state.live_detection_history_lock:
        for cam_id in removed_ids:
            _state.live_detection_history.pop(cam_id, None)
    with _state.live_detection_confirm_lock:
        for cam_id in removed_ids:
            _state.live_detection_confirm_history.pop(cam_id, None)
    with _state.live_detection_status_lock:
        for cam_id in removed_ids:
            _state.live_detection_status.pop(cam_id, None)
    with _state.live_event_last_emitted_lock:
        for cam_id in removed_ids:
            _state.live_event_last_emitted.pop(cam_id, None)
    with _state._still_dwell_lock:
        for cam_id in removed_ids:
            _state._still_dwell.pop(cam_id, None)
    with _state._object_tracks_lock:
        for cam_id in removed_ids:
            _state._object_tracks.pop(cam_id, None)
    with _state._motion_confirm_lock:
        for cam_id in removed_ids:
            _state._motion_confirm_streaks.pop(cam_id, None)
    with _state._frame_motion_lock:
        for cam_id in removed_ids:
            _state._frame_motion_prev.pop(cam_id, None)
            _state._frame_motion_last_frame.pop(cam_id, None)
            _state._frame_motion_last_gray.pop(cam_id, None)
            _state._frame_motion_mog2.pop(cam_id, None)
            _state._frame_motion_mog2_meta.pop(cam_id, None)
            _state._frame_motion_scene_streak.pop(cam_id, None)
            _state._frame_motion_error_cameras.discard(cam_id)
    with _state._live_backoff_lock:
        for cam_id in removed_ids:
            _state.live_detection_retry_after.pop(cam_id, None)
            _state.live_detection_failure_count.pop(cam_id, None)
    with _state.live_detection_worker_lock:
        for cam_id in removed_ids:
            _state.live_detection_last_checked.pop(cam_id, None)
            _state.active_live_detection_cameras.discard(cam_id)
    with _state._sound_statuses_lock:
        for cam_id in removed_ids:
            _state._sound_statuses.pop(cam_id, None)
    for cam_id in removed_ids:
        _state._periodic_scan_last_ts.pop(cam_id, None)
        try:
            from app.face_identity import reset_camera_identities
            reset_camera_identities(cam_id)
        except Exception:  # pragma: no cover - defensive; cache cleanup must not block the apply
            logger.debug('Face identity cache cleanup failed for removed camera %s', cam_id, exc_info=True)


def apply_cameras_settings(settings_list: list[dict[str, Any]]) -> None:
    # Bug 6 fix: take ``_state._apply_settings_lock`` for the entire body so a
    # concurrent ``apply_storage_and_recording_settings`` cannot be mid-swap
    # (still tearing down OLD workers, or about to publish NEW) while we go
    # through ``apply_sound_settings()`` -> ``prime_rtsp_prebuffer`` ->
    # ``_ensure_prebuffer_worker``. Without this lock, ``apply_sound_settings``
    # could land a ``prime_rtsp_prebuffer`` on the OLD service while
    # ``apply_storage_and_recording_settings`` is in the middle of OLD-stopped-
    # then-NEW-published, briefly spawning a fresh ffmpeg on the OLD service
    # that races against the OLD-stop teardown (Bugs 2/4) AND against the NEW
    # service's first worker for the same camera, both writing into the same
    # per-camera directory. Acquiring the lock here serializes BOTH paths so
    # apply-side state mutations are atomic with respect to each other.
    with _state._apply_settings_lock:
        new_instances = create_camera_instances(settings_list)
        new_ids = {str(cfg.get('id') or '') for cfg in settings_list if cfg.get('id')}
        with _state._camera_instances_lock:
            old_instances = _state.camera_instances
            removed_ids = (set(old_instances.keys()) if old_instances else set()) - new_ids
            _state.cameras_config = settings_list
            _state.camera_config = settings_list[0] if settings_list else {}
            _state.camera_instances = new_instances
            new_config = _state.camera_config
            _state.camera = new_instances.get(str(new_config.get('id') or '')) if new_config else None
        for old_cam in (old_instances or {}).values():
            try:
                old_cam.close()
            except Exception as unexpected_exc:
                logger.warning('Unexpected error updating camera: %s', unexpected_exc)
        if removed_ids:
            # Stop prebuffer/continuous ingest workers for cameras that were
            # removed from the config so their ffmpeg processes don't keep
            # running (and logging ingest_restart diagnostics) after deletion.
            rs = _state.recording_service
            for cam_id in removed_ids:
                try:
                    rs.stop_camera_workers(cam_id)
                except Exception as unexpected_exc:
                    logger.warning('Unexpected error stopping workers for deleted camera %s: %s', cam_id, unexpected_exc)
            # Remove deleted cameras from the health-state dict so the
            # /api/cameras/health response no longer counts them.
            with _state._camera_health_lock:
                for cam_id in removed_ids:
                    _state._camera_health_state.pop(cam_id, None)
            # Clear every other per-camera runtime dict (detection history,
            # motion models, debounce state, dwell streaks, face caches, ...)
            # so deleted cameras cannot accumulate entries over time.
            _cleanup_camera_runtime_state(removed_ids)
        apply_sound_settings()


def apply_storage_and_recording_settings() -> None:
    # Bug 6 fix: take ``_state._apply_settings_lock`` for the entire body AND
    # stop OLD workers BEFORE publishing the NEW RecordingService. The OLD
    # path published NEW first and stopped OLD second, which opened a window
    # where (a) ``live_monitor`` / ``sound_monitor`` saw the NEW service and
    # started fresh ffmpegs via ``prime_rtsp_prebuffer`` while the OLD
    # ffmpegs were still alive in their SIGTERM teardown -- two ffmpegs
    # writing into the SAME per-camera directory
    # (``.prebuffer/<key>/segment-*.mp4``, ``.audio/<key>/aud-*.wav``,
    # ``.frames/<key>/latest.jpg``, ``continuous-<key>/``).
    # ``apply_cameras_settings`` can also prime via ``apply_sound_settings``
    # -> ``prime_rtsp_prebuffer`` and was not serialized against this swap.
    #
    # Bug 6 follow-up: also publish a transient ``_SentinelRecordingService``
    # to ``_state.recording_service`` BEFORE the OLD workers are joined.
    # Live monitor / sound monitor / event-driven callbacks keep reading
    # ``_state.recording_service`` across their polling loops and do NOT
    # go through any ``apply_*`` path -- the ``_apply_settings_lock`` only
    # serializes the apply paths against each other. Without the sentinel,
    # a concurrent ``prime_rtsp_prebuffer`` call would land on the OLD
    # service, its ``_ensure_prebuffer_worker`` would see an empty
    # ``_prebuffer_workers`` dict for ``cam1`` (the OLD only had a
    # ``continuous-recorder-cam1`` in ``_continuous_workers``), and would
    # spawn a NEW ``prebuffer-cam1`` worker ON THE OLD SERVICE -- two
    # ffmpegs writing into the same ``.prebuffer/cam1/`` and
    # ``continuous-cam1/`` directories while the OLD service's
    # ``stop_all_continuous_recordings`` SIGTERM-teardown is still running.
    #
    # The sentinel intercepts every such poll: ``prime_rtsp_prebuffer`` on
    # the sentinel is a no-op, ``should_record`` returns ``(False, '', None)``,
    # frame/audio queries return ``None``/empty. Concurrent monitor primes
    # are silently absorbed; the OLD service's workers can drain cleanly
    # while we hold ``_apply_settings_lock``; THEN the NEW
    # ``RecordingService`` is published into a quiescent monitor-landscape.
    with _state._apply_settings_lock:
        old_service = _state.recording_service
        # Publish sentinel BEFORE stopping OLD workers, so any monitor poll
        # that arrives mid-teardown absorbs as a no-op rather than spawning
        # a fresh ffmpeg against the OLD service.
        _state.recording_service = _SentinelRecordingService()
        if old_service is not None:
            try:
                old_service.stop_prebuffer_workers()
                old_service.stop_all_continuous_recordings()
            except Exception as unexpected_exc:
                logger.warning(
                    'Unexpected error tearing down old recording service workers: %s',
                    unexpected_exc,
                )
        # Publish NEW AFTER OLD has fully exited its workers. By the time
        # ``_state.recording_service`` is reassigned below, no OLD ffmpeg is
        # alive that could race a NEW ffmpeg on the same per-camera directory.
        _state.storage = Storage({**_state.config, 'storage': effective_storage_config()})
        _state.recording_service = RecordingService({
            **_state.config,
            'storage': effective_storage_config(),
            'recording': effective_recording_config(),
        })
        _state.recording_service.diagnostic_callback = log_camera_diagnostic


def _rebuild_face_detector(ai_settings: dict[str, Any]) -> None:
    """Rebuild the optional secondary face detector after a settings change.

    Mirrors the primary reload's session teardown so a stale ONNX session is
    freed before a new one is allocated. Failures are non-fatal: the face pass
    simply stays unavailable and the reason is surfaced via status.
    """
    from app.detector import create_face_detector
    previous = _state.face_detector
    old_session = getattr(previous, 'session', None) if previous is not None else None
    if old_session is not None:
        previous.session = None
        del old_session
        gc.collect()
    try:
        candidate = create_face_detector(ai_settings)
    except Exception as exc:  # pragma: no cover - defensive: never break the primary reload
        logger.warning('Secondary face detector build failed: %s', exc)
        _state.face_detector = None
        _state.last_face_detector_error = str(exc)
        return
    _state.face_detector = candidate
    error = getattr(candidate, 'unavailable_reason', None) if candidate is not None else None
    _state.last_face_detector_error = error
    if candidate is not None:
        if getattr(candidate, 'available', False):
            logger.info('Secondary face detector loaded: %s', candidate.model_path)
        else:
            logger.warning('Secondary face detector unavailable: %s', error)


def reload_detector(ai_settings: dict[str, Any]) -> tuple[bool, str | None]:
    import app.alert_dispatch as _alert_dispatch
    _alert_dispatch._min_rule_confidence_cache = None
    # Bug 8 audit fix: alias the OLD reference BEFORE publishing the
    # sentinel so concurrent pollers -- ``live_alert_monitor_loop``'s
    # per-poll ``detect_frame`` / ``ai_status_payload``,
    # ``/api/settings/ai/check-model``, ``/api/settings/ai/test-detector`` --
    # see a deterministic "reloading" state rather than the OLD detector
    # mid-teardown with its ``session`` attribute nulled out from under
    # them. The OLD is no longer reachable via ``_state.detector``, but
    # the local ``previous_detector`` binding keeps a handle so we can
    # still free its ONNX session and associated VRAM before
    # ``create_detector`` allocates the new model.
    previous_detector = _state.detector
    _state.detector = _SentinelDetector()
    old_session = getattr(previous_detector, 'session', None)
    if old_session is not None:
        previous_detector.session = None
        del old_session
        gc.collect()
    candidate = create_detector(ai_settings)
    candidate_error = getattr(candidate, 'unavailable_reason', None)
    if ai_settings['backend'] == 'onnx' and (not getattr(candidate, 'available', False)):
        # Bug 8 rollback safety: publish the BROKEN candidate, NOT the
        # OLD ``_state.detector = previous_detector`` rollback that would
        # re-publish the OLD with its session attribute already nulled
        # out. The broken candidate carries its own ``unavailable_reason``
        # so the admin UI / ``ai_status_payload`` surfaces the actual
        # failure (e.g. "ONNX model not found"); publishing the dead
        # OLD would silently break inference until a successful reload.
        _state.detector = candidate
        _state.last_detector_error = candidate_error or 'Failed to load ONNX detector.'
        _rebuild_face_detector(ai_settings)
        log_detector_initialization('reload_failed')
        return (False, _state.last_detector_error)
    _state.detector = candidate
    _state.last_detector_error = candidate_error
    _rebuild_face_detector(ai_settings)
    log_detector_initialization('reload')
    return (True, _state.last_detector_error)


# Register callables on _state so extracted modules can call them without
# importing app.main (avoids circular deps and Pool C lazy imports).
_state.camera_event_recording_config = camera_event_recording_config
_state.apply_cameras_settings = apply_cameras_settings
_state.apply_storage_and_recording_settings = apply_storage_and_recording_settings
_state.reload_detector = reload_detector
# Face-only reload used by the model-download flow: wiring a downloaded face
# model into the secondary pass must not rebuild (and briefly drop) the
# primary object detector.
_state.rebuild_face_detector = _rebuild_face_detector
