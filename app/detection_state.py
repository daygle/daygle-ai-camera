"""Live-detection state helpers extracted from ``app/main.py`` (Phase-26).

The four helpers shipped here cluster around the **live-detection monitor's
in-memory bookkeeping** that runs once per active camera:

- the per-camera detection-history deques (``live_detection_history`` +
  ``live_detection_history_lock``) under Phase-26 ownership,
- the per-camera adaptive-background motion model
  (``_frame_motion_prev`` + ``_frame_motion_last_frame`` + ``_frame_motion_lock`` + ``_frame_motion_error_cameras``)
  + the four tuning constants (``_MOTION_PIXEL_THRESHOLD`` /
  ``_MOTION_GATE_FRACTION`` / ``_MOTION_SCALE_FRACTION`` /
  ``_MOTION_BACKGROUND_ALPHA``) and the two frame-size constants
  (``_MOTION_FRAME_W`` / ``_MOTION_FRAME_H``),
- the misc detection-label helper (``detection_label_set``).

State PRIMITIVES themselves STAY on ``app.main`` (and continue to be
mutated there by the monitor loop + admin handlers); the new module
reaches them at call time via Pool C. This mirrors the precedent set
by :mod:`app.zone_schema` and :mod:`app.zone_detection` (both
``import app.main as main`` and dereference ``main.<attr>`` inside
their function bodies). It also keeps two cross-module consumers
working unchanged:

- :mod:`app.zone_detection` reads ``main._MOTION_FRAME_W`` /
  ``main._MOTION_FRAME_H`` for its zone-pixel-motion fraction -
  Phase-26 keeps the constants on main.py so this code path is
  unchanged.
- ``tests/test_api.py`` reaches ``main.live_detection_history`` /
  ``main.deque`` / ``main.build_track_from_live_history`` -
  Phase-26's Pool-A rebind wires these names onto main.py so the
  tests continue to work without import edits.

**Pool-A rebind (in ``app/main.py``):** ``detect_frame_motion``,
``record_live_detection_history``, ``build_track_from_live_history``,
``detection_label_set`` are re-bound as ``main.<name>``.

**Pool-C reach into ``main`` (resolved at call time):**

- ``main.live_detection_history_lock`` -
  ``record_live_detection_history``, ``build_track_from_live_history``
- ``main.live_detection_history`` - same two
- ``main._frame_motion_lock`` - ``detect_frame_motion``
- ``main._frame_motion_prev`` - ``detect_frame_motion``
- ``main._frame_motion_last_frame`` - ``detect_frame_motion``
- ``main._frame_motion_last_gray`` - ``detect_frame_motion``
- ``main._frame_motion_error_cameras`` - ``detect_frame_motion``
- ``main._MOTION_FRAME_W`` / ``main._MOTION_FRAME_H`` -
  ``detect_frame_motion``
- ``main._MOTION_PIXEL_THRESHOLD`` / ``main._MOTION_GATE_FRACTION`` /
  ``main._MOTION_SCALE_FRACTION`` / ``main._MOTION_BACKGROUND_ALPHA`` -
  ``detect_frame_motion`` defaults (resolved at call time, not as
  default-arg expressions; see the note below)
- ``main.effective_live_config`` - ``record_live_detection_history``
  (sanity-resolves ``detection_history_minutes`` against the
  configured live settings; tests/callers pass ``live_config``
  kwargs explicitly so this fallback is purely defensive).

**Default-arg safety on ``detect_frame_motion``.** The original main.py
signature ``pixel_threshold=_MOTION_PIXEL_THRESHOLD`` (etc.) evaluated
the constant at function-DEFINITION time. Because ``detect_frame_motion``
is now defined inside ``app/detection_state.py`` which is imported during
the Phase-26 rebind loop - BEFORE main.py's body has executed the
``_MOTION_PIXEL_THRESHOLD`` constant assignment - the bare default
would resolve to ``NameError`` at import time. Following the
:mod:`app.zone_detection` precedent (``zone_motion_detections``) the
defaults become ``None`` and the function body resolves them against
``main._MOTION_*`` on each call. Real callers (``process_live_stream_alerts``)
already pass explicit kwargs, so behavior is unchanged for the hot path.
"""

from __future__ import annotations

import io
import logging
import time
from collections import deque
from typing import Any

import app.state as _state
from app.config_facades import effective_live_config


try:
    import cv2
    _CV2_ERROR = (cv2.error,) if cv2 is not None else ()
except Exception:
    _CV2_ERROR = ()

_EXPECTED_MOTION_ERRORS = (ValueError, TypeError, MemoryError, OSError) + _CV2_ERROR

# Motion errors fail closed. A decode/processing failure is not evidence of
# movement: returning synthetic motion here lets a default motion rule pass and
# can create a recording of a static scene. The warning below identifies the
# bad-frame condition so the camera source can be repaired separately.


logger = logging.getLogger('daygle.ai')
# Per-camera throttle for the motion-gate diagnostic line below (keeps a
# debug run from flooding the log at ~4 Hz per camera).
_motion_log_last_at: dict[str, float] = {}
_MOTION_LOG_INTERVAL = 5.0  # seconds


def record_live_detection_history(camera_id: str, detections: list[dict[str, Any]], sample_ts: float | None=None, *, live_config: dict[str, Any] | None=None) -> None:
    """Append one monitor cycle's detections to the camera's rolling history.

    ``sample_ts`` must be when the analyzed frame was CAPTURED, not when
    inference finished: tracks sliced from this history are replayed against
    the recorded video, and stamping at completion shifts every box late by
    the inference duration - the playback overlay then trails moving objects.

    Empty cycles are recorded too: a recording track sliced from the history
    needs "nothing in frame" samples so playback overlays clear when an object
    leaves instead of holding the last box.

    ``motion_state`` (when present) is preserved alongside label/confidence/box
    so tracks sliced from this history replay the moving/still tag on playback
    overlays. Everything else is intentionally dropped -- the history is a
    compact box record, not a full detection dict."""
    sample = [
        {
            'label': detection.get('label'),
            'confidence': detection.get('confidence'),
            'box': detection.get('box'),
            **({'motion_state': detection['motion_state']} if detection.get('motion_state') in ('moving', 'still') else {}),
        }
        for detection in detections
        if isinstance(detection.get('box'), dict)
    ]
    if sample_ts is None:
        sample_ts = time.time()
    history_minutes = max(1, int((live_config or effective_live_config()).get('detection_history_minutes', 10)))
    history_maxlen = max(120, history_minutes * 120)
    with _state.live_detection_history_lock:
        history = _state.live_detection_history.get(camera_id)
        if history is None or history.maxlen != history_maxlen:
            history = deque(history or [], maxlen=history_maxlen)
            _state.live_detection_history[camera_id] = history
        history.append((sample_ts, sample))


def build_track_from_live_history(camera_id: str | None, start_ts: float, end_ts: float) -> list[dict[str, Any]] | None:
    """Slice the monitor's detection history into a clip-relative track.

    Returns ``[{"t": seconds_from_start, "detections": [...]}]`` or ``None``
    when the history has no samples inside the window (camera idle, monitor
    disabled, or the clip predates the in-memory history)."""
    if not camera_id or end_ts <= start_ts:
        return None
    with _state.live_detection_history_lock:
        samples = list(_state.live_detection_history.get(str(camera_id), ()))
    track = [{'t': round(sample_ts - start_ts, 3), 'detections': sample_detections} for sample_ts, sample_detections in samples if start_ts <= sample_ts <= end_ts]
    return track or None


def detection_label_set(detections: list[dict[str, Any]]) -> set[str]:
    return {str(detection.get('label') or '').strip().lower() for detection in detections if str(detection.get('label') or '').strip()}


def confirm_object_detections(
    camera_id: str,
    detections: list[dict[str, Any]],
    *,
    required_frames: Any,
    window_frames: Any,
) -> list[dict[str, Any]]:
    """Temporal N-of-M confirmation gate for object detections.

    Suppresses a detection until its label has appeared in at least
    ``required_frames`` of the last ``window_frames`` detection *cycles* for
    this camera. This filters transient single-frame false positives (a
    flicker misread as a ``cat``) while letting a genuinely present object -- of
    any label -- through once it has persisted, so it applies uniformly to
    ``person``, ``car``, ``cat``, and every other class.

    The gate is a pass-through no-op when ``required_frames <= 1`` (the default),
    preserving the historical single-frame behavior and touching no per-camera
    state. The window counts detection cycles that actually ran inference (a
    quiet frame with no motion is skipped upstream and never reaches here), not
    wall-clock frames.

    Labels are compared case-insensitively on the raw detector label, matching
    what the downstream zone/alert pipeline consumes.
    """
    try:
        required = int(required_frames)
    except (TypeError, ValueError):
        required = 1
    if required <= 1:
        # Feature disabled: do not accumulate state or allocate, so a camera
        # that never enables confirmation carries no per-camera history.
        return detections
    try:
        window = int(window_frames)
    except (TypeError, ValueError):
        window = required
    # A window smaller than the requirement can never confirm anything; clamp it
    # up so ``required`` of ``window`` is always satisfiable.
    window = max(required, window)

    labels_now = {
        str(detection.get('label') or '').strip().lower()
        for detection in detections
        if str(detection.get('label') or '').strip()
    }
    with _state.live_detection_confirm_lock:
        history = _state.live_detection_confirm_history.get(camera_id)
        if history is None or history.maxlen != window:
            # Preserve recent cycles when the operator resizes the window so a
            # setting change doesn't reset every camera's confirmation state.
            history = deque(history or [], maxlen=window)
            _state.live_detection_confirm_history[camera_id] = history
        history.append(labels_now)
        counts: dict[str, int] = {}
        for cycle_labels in history:
            for label in cycle_labels:
                counts[label] = counts.get(label, 0) + 1
    confirmed = {label for label, count in counts.items() if count >= required}
    if labels_now and not confirmed:
        logger.debug(
            'Confirmation gate for camera %s holding %d detection(s) '
            '(need %d of last %d cycles): %s',
            camera_id, len(detections), required, window, sorted(labels_now),
        )
    return [
        detection
        for detection in detections
        if str(detection.get('label') or '').strip().lower() in confirmed
    ]


def confirm_motion_detections(
    camera_id: str,
    detections: list[dict[str, Any]],
    *,
    required_frames: int = 2,
) -> list[dict[str, Any]]:
    """Require each motion zone to persist across consecutive frames.

    Motion is sampled independently from object inference and can be raised by
    a one-frame decoder or exposure artifact. Keeping the streak per zone means
    a genuine subject moving through a zone is confirmed on the second sample,
    while a transient zone diff never reaches the event/recording path.
    """
    try:
        required = max(1, int(required_frames))
    except (TypeError, ValueError):
        required = 2
    if required <= 1:
        return detections

    current_zone_ids = {
        str(detection.get('zone_id') or detection.get('zone_name') or '').strip()
        for detection in detections
        if str(detection.get('zone_id') or detection.get('zone_name') or '').strip()
    }
    with _state._motion_confirm_lock:
        previous = _state._motion_confirm_streaks.get(camera_id, {})
        current = {
            zone_id: min(required, int(previous.get(zone_id, 0)) + 1)
            for zone_id in current_zone_ids
        }
        _state._motion_confirm_streaks[camera_id] = current

    if detections and not current_zone_ids:
        return []
    confirmed_zone_ids = {
        zone_id for zone_id, streak in current.items() if streak >= required
    }
    if detections and not confirmed_zone_ids:
        logger.debug(
            'Motion confirmation for camera %s holding %d zone(s) until the next frame',
            camera_id,
            len(current_zone_ids),
        )
    return [
        detection
        for detection in detections
        if str(detection.get('zone_id') or detection.get('zone_name') or '').strip()
        in confirmed_zone_ids
    ]


def _mog2_available() -> bool:
    """Return True when the installed OpenCV build exposes MOG2.

    Cached on the module after the first probe. When False (a headless build
    compiled without ``bgsegm``/video), ``detect_frame_motion`` transparently
    falls back to the legacy adaptive-diff engine so motion never goes dark.
    """
    global _MOG2_AVAILABLE
    if _MOG2_AVAILABLE is not None:
        return _MOG2_AVAILABLE
    try:
        import cv2
        _MOG2_AVAILABLE = hasattr(cv2, 'createBackgroundSubtractorMOG2')
    except Exception:
        _MOG2_AVAILABLE = False
    return _MOG2_AVAILABLE


_MOG2_AVAILABLE: bool | None = None
# MOG2 varThreshold is expressed on the squared Mahalanobis distance; keep it in
# a sane band so an out-of-range pixel threshold cannot make the model match
# everything (too high) or nothing (too low).
_MOG2_VAR_THRESHOLD_MIN = 4.0
_MOG2_VAR_THRESHOLD_MAX = 255.0


def _to_motion_thumbnail_bgr(image: Any) -> Any:
    """Decode ``image`` (BGR numpy array or JPEG/PNG bytes) to a BGR thumbnail
    at the configured motion-frame size, using area interpolation so thin
    features survive the downscale. Colour is preserved (not converted to
    grayscale) because MOG2 shadow detection needs the chroma channels."""
    import cv2
    import numpy as np
    if hasattr(image, 'shape') and hasattr(image, 'dtype'):
        frame = image
        if frame.ndim == 2:  # grayscale array -> promote to BGR
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    else:
        from PIL import Image as _Image
        pil = _Image.open(io.BytesIO(image)).convert('RGB')
        frame = cv2.cvtColor(np.array(pil, dtype=np.uint8), cv2.COLOR_RGB2BGR)
    return cv2.resize(
        frame,
        (_state._MOTION_FRAME_W, _state._MOTION_FRAME_H),
        interpolation=cv2.INTER_AREA,
    )


def _denoise_mask(mask: Any) -> Any:
    """Morphologically open then close a boolean foreground mask.

    Opening (erode->dilate) deletes isolated speckle -- single-pixel sensor
    noise and JPEG blocking that the raw subtractor flags as motion -- while
    closing (dilate->erode) refills the small interior gaps opening leaves in a
    real subject, so a genuine blob stays connected. Returns a boolean array of
    the same shape."""
    import cv2
    import numpy as np
    kernel = np.ones((3, 3), np.uint8)
    m = mask.astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel)
    return m.astype(bool)


def _resolve_shadow_suppression(value: Any, thumbnail: Any) -> bool:
    """Resolve the tri-state shadow setting to a concrete on/off for one frame.

    - ``'auto'`` -> reject shadows only while the scene is bright (day). The
      decision uses the mean brightness of the BGR ``thumbnail`` against
      ``_MOTION_NIGHT_BRIGHTNESS``; a dark (night/IR) frame disables shadow
      rejection so MOG2's shadow class does not swallow a genuine subject.
    - ``'on'`` / ``'off'`` (and legacy ``True`` / ``False``) -> honoured directly.
    """
    if isinstance(value, str) and value.strip().lower() == 'auto':
        try:
            import numpy as np
            brightness = float(np.asarray(thumbnail).mean())
        except Exception:
            return True  # brightness unavailable -> default to rejecting shadows
        return brightness >= float(getattr(_state, '_MOTION_NIGHT_BRIGHTNESS', 50.0))
    from app.utils import normalize_bool_setting
    return normalize_bool_setting(value, True)


def _detect_frame_motion_mog2(
    camera_id: str,
    image: Any,
    *,
    pixel_threshold: float,
    gate_fraction: float,
    scale_fraction: float,
    background_alpha: float,
    denoise: bool,
    shadow_suppression: Any,
) -> tuple[bool, float, Any, float]:
    """MOG2 (Gaussian-mixture) background-subtraction motion gate.

    Returns ``(has_motion, confidence, diff_mask, raw_fraction)`` with exactly
    the same contract as the legacy engine, so every downstream consumer (zone
    pixel fraction, zone motion box, the confirmation gate) is unchanged.

    Key behaviours preserved from the legacy engine:

    - **Freeze-on-motion.** The mask is computed with ``learningRate=0`` (no
      model update); the model only learns the frame when the change is below
      the gate. A subject that stops moving is therefore NOT absorbed into the
      background within seconds -- the reported "motion recording stops after
      the first second" failure the legacy freeze also guarded against.
    - **Scene-reset guard.** A change of >=50% of the frame (exposure jump,
      reconnect, IR cut) rebuilds the model against the new scene and reports no
      motion, so a camera-wide transition cannot become a persistent 100%
      recording.
    - **Fail-closed.** Handled by the caller's ``except`` wrapper.

    Improvements over the legacy engine: multi-modal backgrounds (swaying
    foliage, flickering screens) are modelled instead of smeared into one mean,
    gradual illumination drift is tracked, and cast shadows are rejected when
    ``shadow_suppression`` is set."""
    import cv2
    import numpy as np

    resized = _to_motion_thumbnail_bgr(image)
    # Resolve the tri-state shadow setting to a concrete on/off for this frame.
    # 'auto' rejects shadows only while the scene is bright (day); once the frame
    # darkens (night/IR) it stops, because MOG2's shadow class then swallows real
    # subjects. 'on'/'off' (and legacy True/False) are honoured directly.
    shadow_on = _resolve_shadow_suppression(shadow_suppression, resized)
    var_threshold = float(
        max(_MOG2_VAR_THRESHOLD_MIN, min(_MOG2_VAR_THRESHOLD_MAX, pixel_threshold))
    )
    history = max(1, int(getattr(_state, '_MOTION_MOG2_HISTORY', 250)))
    # The parameters baked into a subtractor; a live change to any of them (frame
    # size, sensitivity, resolved shadow state) must rebuild it rather than
    # silently keep the stale model. Using the RESOLVED shadow bool means an
    # 'auto' day->night transition rebuilds the model with detectShadows flipped.
    signature = (
        int(_state._MOTION_FRAME_W),
        int(_state._MOTION_FRAME_H),
        round(var_threshold, 3),
        bool(shadow_on),
        history,
    )

    def _new_subtractor() -> Any:
        subtractor = cv2.createBackgroundSubtractorMOG2(
            history=history,
            varThreshold=var_threshold,
            detectShadows=bool(shadow_on),
        )
        # Seed the model with the current frame so the first real comparison has
        # a background to diff against instead of flagging the whole frame.
        subtractor.apply(resized, learningRate=1.0)
        return subtractor

    with _state._frame_motion_lock:
        subtractor = _state._frame_motion_mog2.get(camera_id)
        meta = _state._frame_motion_mog2_meta.get(camera_id)
        if subtractor is None or meta != signature:
            # First frame for this camera, or a tuning/size change: (re)build the
            # model and report no motion for this seed frame (mirrors the legacy
            # first-frame contract).
            _state._frame_motion_mog2[camera_id] = _new_subtractor()
            _state._frame_motion_mog2_meta[camera_id] = signature
            _state._frame_motion_error_cameras.discard(camera_id)
            return (False, 0.0, None, 0.0)

        # Compute the foreground AND adapt the model at background_alpha every
        # frame. MOG2 keeps a genuinely MOVING subject as foreground (it lands on
        # new pixels each frame, so no single pixel is ever learned) while
        # gradually absorbing one that has STOPPED: a car that parks fades back
        # into the background over ~1/background_alpha frames instead of pinning
        # the motion signal to its silhouette forever. The previous
        # learningRate=0 "freeze during motion" is exactly what left a parked car
        # stuck at ~30% motion indefinitely; recording continuity for a subject
        # that stops is handled by the post-event / keep-recording-after-motion
        # settings, not by freezing the motion signal.
        raw = subtractor.apply(resized, learningRate=background_alpha)
        # MOG2 marks foreground as 255 and (when detectShadows) shadow as 127.
        # Dropping 127 rejects cast shadows; including it treats them as motion.
        mask = raw == 255 if shadow_on else raw >= 127
        if denoise:
            mask = _denoise_mask(mask)
        changed_fraction = float(np.mean(mask))

        # A single >=50% frame is AMBIGUOUS: it could be a camera-wide light
        # change (exposure jump / IR cut / reconnect) OR a large object sweeping
        # close past the camera (a passing car). The former persists; the latter
        # is transient. Only treat it as a scene reset once the >=50% change has
        # held for several consecutive frames -- otherwise report it as the real
        # motion it is, so a big/close car is no longer silently dropped.
        scene_reset_frames = max(1, int(getattr(_state, '_MOTION_SCENE_RESET_FRAMES', 4)))
        if changed_fraction >= 0.5:
            streak = _state._frame_motion_scene_streak.get(camera_id, 0) + 1
            _state._frame_motion_scene_streak[camera_id] = streak
            if streak >= scene_reset_frames:
                # Sustained camera-wide change: rebuild against the new scene and
                # suppress, so a real light change cannot become a persistent
                # 100%-motion recording.
                _state._frame_motion_mog2[camera_id] = _new_subtractor()
                _state._frame_motion_mog2_meta[camera_id] = signature
                _state._frame_motion_scene_streak[camera_id] = 0
                _state._frame_motion_error_cameras.discard(camera_id)
                logger.debug(
                    'Motion scene reset for camera %s: changed=%.4f sustained %d frames; rebuilding MOG2 model',
                    camera_id, changed_fraction, streak,
                )
                return (False, 0.0, None, 0.0)
            # Transient large change -> a big moving object; fall through and
            # report it as the motion it is. It is not learned into the
            # background in a frame or two because it is moving across pixels.
        else:
            _state._frame_motion_scene_streak[camera_id] = 0
        _state._frame_motion_error_cameras.discard(camera_id)

    _now = time.monotonic()
    _last = _motion_log_last_at.get(camera_id, 0.0)
    if _now - _last >= _MOTION_LOG_INTERVAL:
        _motion_log_last_at[camera_id] = _now
        logger.debug(
            'Motion gate %s (mog2): changed=%.4f gate=%.4f var=%.1f shadow=%s(%s) denoise=%s WxH=%dx%d',
            camera_id, changed_fraction, gate_fraction, var_threshold,
            shadow_suppression, 'on' if shadow_on else 'off', denoise,
            _state._MOTION_FRAME_W, _state._MOTION_FRAME_H,
        )

    confidence = round(min(1.0, changed_fraction / max(scale_fraction, 1e-9)), 3)
    if changed_fraction < gate_fraction:
        return (False, 0.0, mask, round(changed_fraction, 6))
    return (True, confidence, mask, round(changed_fraction, 6))


def detect_frame_motion(
    camera_id: str,
    image: Any,
    *,
    pixel_threshold: float | None = None,
    gate_fraction: float | None = None,
    scale_fraction: float | None = None,
    background_alpha: float | None = None,
    algorithm: str | None = None,
    denoise: bool | None = None,
    shadow_suppression: Any = None,
) -> tuple[bool, float, Any, float]:
    """Per-camera motion gate. Returns ``(has_motion, confidence, diff_mask, raw_fraction)``.

    Dispatches to the MOG2 (Gaussian-mixture) engine by default and to the
    legacy adaptive-diff engine when ``algorithm='diff'`` or the OpenCV build
    lacks MOG2. All tuning parameters default to the module-level ``_MOTION_*``
    constants on :mod:`app.state`, resolved at call time (the same lazy-default
    pattern the diff engine uses), so callers that pass explicit kwargs -- the
    live monitor -- are unaffected.

    ``diff_mask`` is a boolean ``(H×W)`` array of changed thumbnail pixels (or
    ``None`` on the first frame / an error). Callers slice it for per-zone
    scores. On any decode/processing error the gate fails closed -- a broken
    frame is not evidence of motion and must not satisfy a motion rule."""
    if pixel_threshold is None:
        pixel_threshold = _state._MOTION_PIXEL_THRESHOLD
    if gate_fraction is None:
        gate_fraction = _state._MOTION_GATE_FRACTION
    if scale_fraction is None:
        scale_fraction = _state._MOTION_SCALE_FRACTION
    if background_alpha is None:
        background_alpha = _state._MOTION_BACKGROUND_ALPHA
    if algorithm is None:
        algorithm = getattr(_state, '_MOTION_ALGORITHM', 'mog2')
    if denoise is None:
        denoise = getattr(_state, '_MOTION_DENOISE', True)
    if shadow_suppression is None:
        shadow_suppression = getattr(_state, '_MOTION_SHADOW_SUPPRESSION', 'on')

    use_mog2 = str(algorithm or 'mog2').strip().lower() != 'diff' and _mog2_available()
    if use_mog2:
        try:
            return _detect_frame_motion_mog2(
                camera_id,
                image,
                pixel_threshold=pixel_threshold,
                gate_fraction=gate_fraction,
                scale_fraction=scale_fraction,
                background_alpha=background_alpha,
                denoise=bool(denoise),
                shadow_suppression=shadow_suppression,
            )
        except _EXPECTED_MOTION_ERRORS as exc:
            with _state._frame_motion_lock:
                if camera_id not in _state._frame_motion_error_cameras:
                    logger.warning(
                        'MOG2 motion gate unavailable for camera %s: %s; suppressing motion until a valid frame is available',
                        camera_id, exc,
                    )
                    _state._frame_motion_error_cameras.add(camera_id)
            return (False, 0.0, None, 0.0)
        except Exception:
            with _state._frame_motion_lock:
                if camera_id not in _state._frame_motion_error_cameras:
                    logger.exception(
                        'Unexpected MOG2 motion gate failure for camera %s; suppressing motion until a valid frame is available',
                        camera_id,
                    )
                    _state._frame_motion_error_cameras.add(camera_id)
            return (False, 0.0, None, 0.0)

    return _detect_frame_motion_diff(
        camera_id,
        image,
        pixel_threshold=pixel_threshold,
        gate_fraction=gate_fraction,
        scale_fraction=scale_fraction,
        background_alpha=background_alpha,
    )


def _detect_frame_motion_diff(camera_id: str, image: Any, *, pixel_threshold: float | None=None, gate_fraction: float | None=None, scale_fraction: float | None=None, background_alpha: float | None=None) -> tuple[bool, float, Any, float]:
    """Legacy single-frame adaptive-background motion gate. Returns (has_motion, confidence 0-1, diff_mask).

    ``image`` may be a BGR numpy array (from ``read_frame``) or JPEG bytes
    (legacy callers).  When a numpy array is provided the PIL decode is
    skipped, saving ~5-15 ms per cycle.

    Threshold parameters default to module-level constants on ``app.main``
    (resolved at call time) so operators can tune sensitivity without
    touching code. Callers that already pass explicit kwargs
    (e.g. ``process_live_stream_alerts``) are unaffected - this is the
    same lazy-default pattern :mod:`app.zone_detection` uses for
    ``zone_motion_detections``.

    Note: the four tuning parameters are typed ``float | None`` rather
    than ``float`` because the ``None`` default is resolved against
    ``main._MOTION_*`` at call time, not at function-definition time.
    Evaluating ``main._MOTION_PIXEL_THRESHOLD`` (or the bare-name
    ``_MOTION_PIXEL_THRESHOLD``) as the function-default would fire
    when ``app/detection_state.py`` is imported during main.py's
    Phase-26 rebind loop, BEFORE the state-primitive block at
    L376-396 has executed ``_MOTION_PIXEL_THRESHOLD = 30`` on
    ``app.main``. The ``None`` default is deliberate, matching
    :mod:`app.zone_detection`'s ``zone_motion_detections`` signature.

    Returns ``(has_motion, frame_confidence, diff_mask, raw_fraction)`` where
    ``diff_mask`` is a boolean (H×W) numpy array indicating which thumbnail
    pixels changed by more than ``pixel_threshold``. The mask combines change
    against the adaptive background with change since the previous analyzed
    frame, so a moving subject is not lost merely because it is small or the
    ingest repeated a frame. Callers can slice ``diff_mask`` to compute
    per-zone confidence scores instead of using the frame-wide value.
    ``diff_mask`` is ``None`` on the first frame or when an error occurs.
    ``frame_confidence`` is gated to ``0.0`` below ``gate_fraction`` (so alert
    logic ignores noise). ``raw_fraction`` is the ungated changed-pixel
    fraction (0.0-1.0) for UI diagnostics.
    """
    if pixel_threshold is None:
        pixel_threshold = _state._MOTION_PIXEL_THRESHOLD
    if gate_fraction is None:
        gate_fraction = _state._MOTION_GATE_FRACTION
    if scale_fraction is None:
        scale_fraction = _state._MOTION_SCALE_FRACTION
    if background_alpha is None:
        background_alpha = _state._MOTION_BACKGROUND_ALPHA
    try:
        import cv2
        import numpy as np
        if hasattr(image, 'shape') and hasattr(image, 'dtype'):
            full_gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            resized = cv2.resize(full_gray, (_state._MOTION_FRAME_W, _state._MOTION_FRAME_H), interpolation=cv2.INTER_NEAREST)
            current = resized.astype(np.float32)
        else:
            from PIL import Image as _Image
            full_image = _Image.open(io.BytesIO(image)).convert('L')
            full_gray = np.array(full_image, dtype=np.uint8)
            resized = full_image.resize((_state._MOTION_FRAME_W, _state._MOTION_FRAME_H), _Image.NEAREST)
            current = np.array(resized, dtype=np.float32)
        with _state._frame_motion_lock:
            background = _state._frame_motion_prev.get(camera_id)
            previous_frame = _state._frame_motion_last_frame.get(camera_id)
            previous_gray = _state._frame_motion_last_gray.get(camera_id)
            # Reset on a first frame OR a shape mismatch. ``_MOTION_FRAME_W/H``
            # are global and read outside this lock, so a concurrent live-settings
            # frame-size change (which clears all backgrounds) can race with a
            # store here and leave a background sized to the OLD dimensions. Without
            # the shape check the subsequent ``current - background`` would raise on
            # every frame and the ``except`` (which never resets the background)
            # would pin the camera to fail-open motion=True forever. Treating a
            # mismatch like a first frame self-heals it in one cycle.
            if (
                background is None
                or background.shape != current.shape
                or (previous_frame is not None and previous_frame.shape != current.shape)
                or (previous_gray is not None and previous_gray.shape != full_gray.shape)
            ):
                _state._frame_motion_prev[camera_id] = current
                _state._frame_motion_last_frame[camera_id] = current
                _state._frame_motion_last_gray[camera_id] = full_gray
                _state._frame_motion_error_cameras.discard(camera_id)
                return (False, 0.0, None, 0.0)
            background_diff = np.abs(current - background) > pixel_threshold
            # A subject can move only a small distance between sampled frames.
            # In that case its per-frame silhouette may be below the global gate
            # even though it is genuinely moving. Unioning the temporal diff
            # with the background diff catches both first appearance and motion
            # while leaving the object detector and its filtering untouched.
            temporal_diff = (
                np.abs(current - previous_frame) > pixel_threshold
                if previous_frame is not None
                else False
            )
            # Preserve fine movement before thumbnail reduction. A single leaf
            # or wire can disappear when nearest-neighbour resizing selects a
            # different source pixel; reducing a full-resolution binary mask
            # with area interpolation retains those changed regions in the
            # motion thumbnail without changing the ONNX input or detections.
            if previous_gray is not None:
                full_temporal_diff = (
                    np.abs(full_gray.astype(np.int16) - previous_gray.astype(np.int16))
                    > pixel_threshold
                ).astype(np.uint8)
                fine_temporal_diff = cv2.resize(
                    full_temporal_diff,
                    (_state._MOTION_FRAME_W, _state._MOTION_FRAME_H),
                    interpolation=cv2.INTER_AREA,
                ) > 0
            else:
                fine_temporal_diff = False
            diff_mask = background_diff | temporal_diff | fine_temporal_diff
            changed_fraction = float(np.mean(diff_mask))
            _state._frame_motion_last_frame[camera_id] = current
            _state._frame_motion_last_gray[camera_id] = full_gray
            # A large fraction changing at once is AMBIGUOUS: a camera-wide light
            # change (exposure jump / reconnect) OR a large object sweeping close
            # past the camera (a passing car). The former persists; the latter is
            # transient. Only reseed + suppress once the >=50% change has held for
            # several consecutive frames, so a big/close car is reported as the
            # real motion it is instead of being silently dropped. A genuine light
            # change still persists and reseeds after the short delay, so a static
            # scene cannot become a persistent 100% motion recording.
            scene_reset_frames = max(1, int(getattr(_state, '_MOTION_SCENE_RESET_FRAMES', 4)))
            if changed_fraction >= 0.5:
                streak = _state._frame_motion_scene_streak.get(camera_id, 0) + 1
                _state._frame_motion_scene_streak[camera_id] = streak
                if streak >= scene_reset_frames:
                    _state._frame_motion_prev[camera_id] = current
                    _state._frame_motion_scene_streak[camera_id] = 0
                    _state._frame_motion_error_cameras.discard(camera_id)
                    logger.debug(
                        'Motion scene reset for camera %s: changed=%.4f sustained %d frames; suppressing camera-wide transition',
                        camera_id, changed_fraction, streak,
                    )
                    return (False, 0.0, None, 0.0)
                # Transient large change -> a big moving object; report it as
                # motion. The background is NOT updated below (change is above the
                # gate), so the object is not learned in while it is still moving.
            else:
                _state._frame_motion_scene_streak[camera_id] = 0
            _now = time.monotonic()
            _last = _motion_log_last_at.get(camera_id, 0.0)
            # Threshold-tuning diagnostic (changed vs gate + pixel threshold):
            # originally a debug line promoted to INFO, it just floods the
            # application log in steady state, so it lives at DEBUG again and
            # only appears when the root logger is lowered below INFO.
            if _now - _last >= _MOTION_LOG_INTERVAL:
                _motion_log_last_at[camera_id] = _now
                logger.debug(
                    'Motion gate %s: changed=%.4f gate=%.4f px_thresh=%d WxH=%dx%d',
                    camera_id, changed_fraction, gate_fraction, pixel_threshold,
                    _state._MOTION_FRAME_W, _state._MOTION_FRAME_H,
                )
            # Only adapt the background when no motion is detected. Freezing the
            # background during motion keeps moving subjects visible indefinitely
            # instead of being absorbed into the background model within seconds.
            if changed_fraction < gate_fraction:
                updated_bg = (1.0 - background_alpha) * background + background_alpha * current
                _state._frame_motion_prev[camera_id] = updated_bg
            _state._frame_motion_error_cameras.discard(camera_id)
        # Scaled 0-1 motion value. When the change is below the alert gate the
        # returned confidence is forced to 0.0; otherwise it equals this value.
        confidence = round(min(1.0, changed_fraction / max(scale_fraction, 1e-9)), 3)
        if changed_fraction < gate_fraction:
            return (False, 0.0, diff_mask, round(changed_fraction, 6))
        return (True, confidence, diff_mask, round(changed_fraction, 6))
    except _EXPECTED_MOTION_ERRORS as exc:
        with _state._frame_motion_lock:
            if camera_id not in _state._frame_motion_error_cameras:
                logger.warning(
                    'Motion gate unavailable for camera %s: %s; suppressing motion until a valid frame is available',
                    camera_id,
                    exc,
                )
                _state._frame_motion_error_cameras.add(camera_id)
        # A failed decode or motion calculation cannot establish that pixels
        # changed. Fail closed so a broken/stale frame cannot satisfy a motion
        # rule and create a recording of a static scene.
        return (False, 0.0, None, 0.0)
    except Exception:
        with _state._frame_motion_lock:
            if camera_id not in _state._frame_motion_error_cameras:
                logger.exception(
                    'Unexpected motion gate failure for camera %s; suppressing motion until a valid frame is available',
                    camera_id,
                )
                _state._frame_motion_error_cameras.add(camera_id)
        return (False, 0.0, None, 0.0)
