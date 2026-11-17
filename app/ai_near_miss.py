"""AI near-miss check: the vision model's view on what Second Look rejects.

Second Look (app.second_look) re-runs the object detector on a zoomed crop
of each detection just under its threshold and drops it when the detector is
still unsure. A real person at night can stay at 40% however closely the
detector looks. With this check on, such a rejected near-miss is shown to the
vision model (app.ai_verification) in the background:

* the crop around the box is queued on the AI pool at background priority,
  so it never delays an alert, and the live detection loop never waits;
* a "yes, a real <label>" is remembered for the camera for
  CONFIRM_TTL_SECONDS, and on the following detection cycles a near-miss of
  that label at about the same place passes Second Look (see
  :func:`is_confirmed`). The normal pipeline then takes over: confirmation
  frames, the event, its recording and alerts.
* a "no" is remembered too (REJECT_TTL_SECONDS), so the same shadow is not
  asked about every cycle.

Nothing is created after the fact: an object that leaves before the answer
arrives is simply missed, as it would have been without the check. Off by
default (``object_detection_ai_near_miss``); needs Second Look on.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger('daygle.ai.detection')

CONFIRM_TTL_SECONDS = 20.0
REJECT_TTL_SECONDS = 60.0
# At most one model request per camera this often, so a busy scene cannot
# flood the AI queue with near-misses.
MIN_SUBMIT_INTERVAL_SECONDS = 3.0
# How much a later box must overlap a judged one to be the same object.
MATCH_IOU = 0.3
MAX_ENTRIES_PER_CAMERA = 20
CHECK_TIMEOUT_SECONDS = 30.0

_lock = threading.Lock()
# camera id -> [{'label', 'box', 'status': pending|confirmed|rejected, 'expires'}]
_entries: dict[str, list[dict[str, Any]]] = {}
_last_submit: dict[str, float] = {}


def enabled(live_settings: dict[str, Any]) -> bool:
    """The ``object_detection_ai_near_miss`` toggle; only meaningful with Second Look."""
    from app.second_look import second_look_enabled
    from app.utils import normalize_bool_setting

    return second_look_enabled(live_settings) and normalize_bool_setting(
        live_settings.get('object_detection_ai_near_miss'), False)


def _matching(camera_id: str, det: dict[str, Any], now: float) -> dict[str, Any] | None:
    from app.second_look import _box, _iou

    box = _box(det.get('box'))
    label = str(det.get('label') or '').strip().lower()
    if box is None or not label:
        return None
    entries = [entry for entry in _entries.get(camera_id, []) if entry['expires'] > now]
    _entries[camera_id] = entries
    for entry in entries:
        if entry['label'] == label and _iou(entry['box'], box) >= MATCH_IOU:
            return entry
    return None


def is_confirmed(camera_id: Any, det: dict[str, Any]) -> bool:
    """Whether the model recently confirmed a ``det``-like object on this camera."""
    with _lock:
        entry = _matching(str(camera_id), det, time.monotonic())
        return bool(entry and entry['status'] == 'confirmed')


def _crop_jpeg(frame: Any, box: dict[str, float]) -> bytes | None:
    import cv2

    from app.second_look import _crop_region

    height, width = frame.shape[:2]
    rx, ry, rw, rh = _crop_region(box)
    x1, y1 = int(rx * width), int(ry * height)
    x2, y2 = int((rx + rw) * width), int((ry + rh) * height)
    if x2 - x1 < 16 or y2 - y1 < 16:
        return None
    ok, encoded = cv2.imencode('.jpg', frame[y1:y2, x1:x2], [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return encoded.tobytes() if ok else None


def submit(camera_id: Any, camera_name: str, frame: Any, det: dict[str, Any]) -> bool:
    """Queue a Second Look reject for the model. Never blocks or raises;
    returns whether a request was queued."""
    from app.second_look import _box

    try:
        camera = str(camera_id)
        box = _box(det.get('box'))
        label = str(det.get('label') or '').strip().lower()
        if box is None or not label or not hasattr(frame, 'shape'):
            return False
        now = time.monotonic()
        with _lock:
            if _matching(camera, det, now) is not None:
                return False  # already asked (or answered) about this object
            if now - _last_submit.get(camera, -1e9) < MIN_SUBMIT_INTERVAL_SECONDS:
                return False
            entries = _entries.setdefault(camera, [])
            if len(entries) >= MAX_ENTRIES_PER_CAMERA:
                return False
            entry = {'label': label, 'box': box, 'status': 'pending', 'expires': now + CHECK_TIMEOUT_SECONDS * 2}
            entries.append(entry)
            _last_submit[camera] = now
        image = _crop_jpeg(frame, box)
        if image is None:
            _forget(camera, entry)
            return False
        from app.postprocess_pool import PRIORITY_BACKGROUND, verification_pool

        queued = verification_pool().submit(
            _check, camera, camera_name, image, label, entry,
            priority=PRIORITY_BACKGROUND, block=False, label=f'near-miss-{camera}',
        )
        if not queued:
            _forget(camera, entry)
        return bool(queued)
    except Exception:  # noqa: BLE001 - an optional check must never break the detection loop
        logger.debug('Could not queue AI near-miss check', exc_info=True)
        return False


def _forget(camera: str, entry: dict[str, Any]) -> None:
    with _lock:
        entries = _entries.get(camera, [])
        if entry in entries:
            entries.remove(entry)


def _check(camera: str, camera_name: str, image: bytes, label: str, entry: dict[str, Any],
           verifier: Any = None) -> None:
    """AI-pool job: ask the model about one near-miss crop and remember the answer."""
    from app.ai_verification import VerificationError, VisionVerifier, effective_ai_verification_settings

    settings = effective_ai_verification_settings()
    if not str(settings.get('server_url') or '').strip() or not str(settings.get('model') or '').strip():
        _forget(camera, entry)
        return
    try:
        verifier = verifier or VisionVerifier(settings, timeout=CHECK_TIMEOUT_SECONDS, background=True)
        verdict = verifier.ask(image, label, camera_name)
    except VerificationError as exc:
        logger.debug('AI near-miss check failed for %s on %s: %s', label, camera, exc)
        _forget(camera, entry)
        return
    present = verdict[0]
    with _lock:
        if present is True:
            entry.update(status='confirmed', expires=time.monotonic() + CONFIRM_TTL_SECONDS)
        else:
            entry.update(status='rejected', expires=time.monotonic() + REJECT_TTL_SECONDS)
    if present is True:
        from app.diagnostics import log_ai_diagnostic

        logger.info('AI confirmed a near-miss %s on camera %s', label, camera)
        log_ai_diagnostic(
            'ai_near_miss_confirmed',
            f'AI confirmed a near-miss {label} the detector was unsure about',
            camera_id=camera, camera_name=camera_name, details={'label': label, 'reason': verdict[1]},
        )


def reset() -> None:
    """Forget every remembered answer (tests, settings reload)."""
    with _lock:
        _entries.clear()
        _last_submit.clear()
