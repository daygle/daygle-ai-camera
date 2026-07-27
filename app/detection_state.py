"""Live-detection state helpers extracted from ``app/main.py`` (Phase-26).

The four helpers shipped here cluster around the **live-detection monitor's
in-memory bookkeeping** that runs once per active camera:

- the per-camera detection-history deques (``live_detection_history`` +
  ``live_detection_history_lock``) under Phase-26 ownership,
- the per-camera adaptive-background motion model
  (``_frame_motion_prev`` + ``_frame_motion_lock`` + ``_frame_motion_error_cameras``)
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


logger = logging.getLogger('daygle.ai')


def record_live_detection_history(camera_id: str, detections: list[dict[str, Any]], sample_ts: float | None=None, *, live_config: dict[str, Any] | None=None) -> None:
    """Append one monitor cycle's detections to the camera's rolling history.

    ``sample_ts`` must be when the analyzed frame was CAPTURED, not when
    inference finished: tracks sliced from this history are replayed against
    the recorded video, and stamping at completion shifts every box late by
    the inference duration - the playback overlay then trails moving objects.

    Empty cycles are recorded too: a recording track sliced from the history
    needs "nothing in frame" samples so playback overlays clear when an object
    leaves instead of holding the last box."""
    sample = [{'label': detection.get('label'), 'confidence': detection.get('confidence'), 'box': detection.get('box')} for detection in detections if isinstance(detection.get('box'), dict)]
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


def detect_frame_motion(camera_id: str, image: Any, *, pixel_threshold: float | None=None, gate_fraction: float | None=None, scale_fraction: float | None=None, background_alpha: float | None=None) -> tuple[bool, float, Any, float]:
    """Adaptive-background motion gate. Returns (has_motion, confidence 0-1, diff_mask, intensity 0-1).

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

    Returns ``(has_motion, frame_confidence, diff_mask, frame_intensity)`` where
    ``diff_mask`` is a boolean (H×W) numpy array indicating which thumbnail
    pixels changed by more than ``pixel_threshold``.  Callers can slice
    ``diff_mask`` to compute per-zone confidence scores instead of using the
    frame-wide value.  ``diff_mask`` is ``None`` on the first frame or when an
    error occurs.  ``frame_confidence`` is gated to ``0.0`` below
    ``gate_fraction`` (so alert logic ignores noise), while ``frame_intensity``
    is the same 0-1 scaled value WITHOUT the gate, so a motion-history
    visualisation can still reflect sub-gate ambient activity.
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
        import numpy as np
        if hasattr(image, 'shape') and hasattr(image, 'dtype'):
            import cv2
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            resized = cv2.resize(gray, (_state._MOTION_FRAME_W, _state._MOTION_FRAME_H), interpolation=cv2.INTER_NEAREST)
            current = resized.astype(np.float32)
        else:
            from PIL import Image as _Image
            img = _Image.open(io.BytesIO(image)).convert('L').resize((_state._MOTION_FRAME_W, _state._MOTION_FRAME_H), _Image.NEAREST)
            current = np.array(img, dtype=np.float32)
        with _state._frame_motion_lock:
            background = _state._frame_motion_prev.get(camera_id)
            # Reset on a first frame OR a shape mismatch. ``_MOTION_FRAME_W/H``
            # are global and read outside this lock, so a concurrent live-settings
            # frame-size change (which clears all backgrounds) can race with a
            # store here and leave a background sized to the OLD dimensions. Without
            # the shape check the subsequent ``current - background`` would raise on
            # every frame and the ``except`` (which never resets the background)
            # would pin the camera to fail-open motion=True forever. Treating a
            # mismatch like a first frame self-heals it in one cycle.
            if background is None or background.shape != current.shape:
                _state._frame_motion_prev[camera_id] = current
                _state._frame_motion_error_cameras.discard(camera_id)
                return (False, 0.0, None, 0.0)
            diff_mask = np.abs(current - background) > pixel_threshold
            changed_fraction = float(np.mean(diff_mask))
            # Only adapt the background when no motion is detected. Freezing the
            # background during motion keeps moving subjects visible indefinitely
            # instead of being absorbed into the background model within seconds.
            if changed_fraction < gate_fraction:
                updated_bg = (1.0 - background_alpha) * background + background_alpha * current
                _state._frame_motion_prev[camera_id] = updated_bg
            _state._frame_motion_error_cameras.discard(camera_id)
        # Raw frame-motion intensity, scaled the same way as the gated
        # confidence but WITHOUT applying the alert gate. The /live "Live
        # motion" sparkline records this so ambient, sub-gate activity still
        # animates the chart instead of flatlining at zero; alert decisions
        # keep using the gated confidence / per-zone diff_mask below.
        intensity = round(min(1.0, changed_fraction / max(scale_fraction, 1e-9)), 3)
        if changed_fraction < gate_fraction:
            return (False, 0.0, diff_mask, intensity)
        return (True, intensity, diff_mask, intensity)
    except Exception as exc:
        with _state._frame_motion_lock:
            if camera_id not in _state._frame_motion_error_cameras:
                logger.warning('Motion gate unavailable for camera %s: %s; failing open', camera_id, exc)
                _state._frame_motion_error_cameras.add(camera_id)
        return (True, 0.4, None, 0.4)
