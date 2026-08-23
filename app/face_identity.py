"""Live face-identity annotation for the detection loop (Stage 2c-2b).

Bridges the recognition service to the per-camera detection hot path: given the
tracked detections and the current frame, it recognises each ``face`` detection
and annotates the detection dict in place with the matched person (or marks it
unknown). Downstream code carries those annotations through to the stored event
metadata and the live overlay without any further changes.

Recognition inference is amortised across the object tracker's stable
``track_id``: a face that has already been identified on a track is not
re-embedded every ~4 Hz cycle. Faces that come back unknown are retried (a later
frame may be clearer), so cost stays bounded to "one embed per known face, plus
retries while a face is still unknown".

The whole module is a no-op unless recognition is enabled with a loaded model
(``service.available``) and the frame is a numpy array, so a camera running a
plain COCO detector -- which emits no ``face`` label -- pays nothing.
"""
from __future__ import annotations

import threading
from typing import Any

from app.face_detection_rules import enabled_unknown_rule
from app.face_recognition_service import get_face_recognition_service

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised only in minimal installs
    np = None  # type: ignore[assignment]

_FACE_LABEL = 'face'

# Per-camera identity cache: camera_id -> {track_id: MatchResult | None}.
# ``None`` means "recognised as unknown so far" and is retried each cycle; a
# MatchResult is cached and reused for the life of the track.
_cache: dict[str, dict[Any, Any]] = {}
# Per-camera set of unknown-face track ids already alerted, so a lingering
# stranger raises one alert rather than one per ~4 Hz cycle.
_alerted_unknown: dict[str, set[Any]] = {}
_lock = threading.Lock()


def _is_face(detection: dict[str, Any]) -> bool:
    return str(detection.get('label') or '').strip().lower() == _FACE_LABEL


def _crop_from_box(frame: Any, box: dict[str, Any]) -> Any:
    """Crop a normalised ``{x, y, width, height}`` box to pixels; None if empty."""
    height, width = frame.shape[:2]
    try:
        x0 = int(float(box.get('x', 0.0)) * width)
        y0 = int(float(box.get('y', 0.0)) * height)
        x1 = int((float(box.get('x', 0.0)) + float(box.get('width', 0.0))) * width)
        y1 = int((float(box.get('y', 0.0)) + float(box.get('height', 0.0))) * height)
    except (TypeError, ValueError):
        return None
    x0 = max(0, min(x0, width))
    y0 = max(0, min(y0, height))
    x1 = max(x0, min(x1, width))
    y1 = max(y0, min(y1, height))
    if x1 <= x0 or y1 <= y0:
        return None
    return frame[y0:y1, x0:x1]


def _apply(detection: dict[str, Any], result: Any) -> None:
    detection['recognized'] = True
    if result is None:
        detection['identity'] = 'unknown'
        detection['person_id'] = None
        detection['person_name'] = None
    else:
        detection['identity'] = result.name
        detection['person_id'] = result.person_id
        detection['person_name'] = result.name
        detection['identity_score'] = round(float(result.score), 4)


def annotate_face_identities(camera_id: str, detections: list[dict[str, Any]], frame: Any) -> list[dict[str, Any]]:
    """Annotate ``face`` detections in ``detections`` with recognised identities.

    Mutates and returns the same list. A no-op (returns unchanged) when
    recognition is unavailable, numpy/frame is missing, or there are no faces.
    """
    service = get_face_recognition_service()
    if np is None or frame is None or not getattr(frame, 'shape', None) or not service.available:
        return detections
    faces = [d for d in detections if _is_face(d)]
    if not faces:
        with _lock:
            _cache.pop(camera_id, None)
        return detections

    with _lock:
        cam_cache = _cache.setdefault(camera_id, {})
        seen: set[Any] = set()
        for detection in faces:
            track_id = detection.get('track_id')
            seen.add(track_id)
            cached = cam_cache.get(track_id, 'miss')
            # Recognise on first sight and keep retrying while still unknown; a
            # cached positive match is reused for the life of the track.
            if track_id is not None and cached != 'miss' and cached is not None:
                result = cached
            else:
                crop = _crop_from_box(frame, detection.get('box') or {})
                result = service.recognize(crop) if crop is not None else None
                if track_id is not None:
                    cam_cache[track_id] = result
            _apply(detection, result)
        # Prune tracks no longer present so the cache cannot grow unbounded.
        _cache[camera_id] = {tid: res for tid, res in cam_cache.items() if tid in seen}
    return detections


def face_identity_metadata(detections: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise recognised identities for storage on the event metadata.

    Returns ``{'face_identities': [...]}`` (recognised people + an unknown
    count) or ``{}`` when no face was run through recognition, so the base event
    metadata is untouched on non-face cameras.
    """
    recognized: list[dict[str, Any]] = []
    unknown = 0
    saw_face = False
    for detection in detections:
        if not detection.get('recognized'):
            continue
        saw_face = True
        if detection.get('person_id') is not None:
            recognized.append({
                'person_id': detection.get('person_id'),
                'name': detection.get('person_name'),
                'track_id': detection.get('track_id'),
                'score': detection.get('identity_score'),
            })
        else:
            unknown += 1
    if not saw_face:
        return {}
    return {'face_identities': {'people': recognized, 'unknown': unknown}}


def unknown_face_alerts(camera_id: str, detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the unknown-face detections that should raise a new alert.

    A no-op (``[]``) unless recognition is available AND the ``_unknown``
    system face-detection rule is enabled (the Face Rules tab is where
    unknown-person alerting is configured -- it replaced the legacy
    ``alert_unknown`` recognition setting). Only faces already annotated as
    unknown (``recognized`` with no ``person_id``) count, and each is alerted
    **once per track** -- a stranger who lingers does not flood the alert
    feed. Tracks are forgotten when they leave the frame, so the same person
    returning later alerts again.

    Must be called after :func:`annotate_face_identities` (it reads the identity
    annotations) and only decides *which* faces are alert-worthy; the caller
    turns them into alert-history entries.
    """
    service = get_face_recognition_service()
    if not service.available:
        return []
    if enabled_unknown_rule() is None:
        return []
    face_track_ids: set[Any] = set()
    new_alerts: list[dict[str, Any]] = []
    with _lock:
        alerted = _alerted_unknown.setdefault(camera_id, set())
        for detection in detections:
            if not _is_face(detection) or not detection.get('recognized'):
                continue
            track_id = detection.get('track_id')
            if track_id is None:
                continue
            face_track_ids.add(track_id)
            if detection.get('person_id') is None and track_id not in alerted:
                alerted.add(track_id)
                new_alerts.append(detection)
        # Forget tracks no longer present so a returning stranger re-alerts and
        # the set cannot grow unbounded.
        _alerted_unknown[camera_id] = {tid for tid in alerted if tid in face_track_ids}
    return new_alerts


def reset_camera_identities(camera_id: str) -> None:
    """Drop the identity + unknown-alert caches for a camera (stop/reconfigure)."""
    with _lock:
        _cache.pop(camera_id, None)
        _alerted_unknown.pop(camera_id, None)
