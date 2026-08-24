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

from app.face_detection_rules import (
    _coerce_bool,
    effective_face_detection_rules,
    is_unknown_rule,
    rule_scope_matches,
)
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

    # Phase 1 (under lock): consult the per-track cache and collect the faces
    # that actually need an embedding pass. Cached positive matches are reused
    # for the life of the track; ``None`` ("recognised as unknown so far")
    # retries, since a later frame is often clearer.
    pending: list[tuple[dict[str, Any], Any, Any]] = []  # (detection, track_id, crop)
    with _lock:
        cam_cache = _cache.setdefault(camera_id, {})
        for detection in faces:
            track_id = detection.get('track_id')
            cached = cam_cache.get(track_id, 'miss')
            if track_id is not None and cached != 'miss' and cached is not None:
                _apply(detection, cached)
                continue
            pending.append((detection, track_id, _crop_from_box(frame, detection.get('box') or {})))

    # Phase 2 (NO lock): recognition is a neural-network embedding pass (tens
    # of ms per face). Running it inside the module lock serialised every
    # camera's annotation cycle behind one camera's inference -- with N
    # cameras the per-cycle lock hold time grew roughly N x embed-time.
    results_by_track: dict[Any, Any] = {}
    for detection, track_id, crop in pending:
        result = service.recognize(crop) if crop is not None else None
        _apply(detection, result)
        if track_id is not None:
            results_by_track[track_id] = result

    # Phase 3 (under lock): write the fresh results back and prune tracks no
    # longer present so the cache cannot grow unbounded.
    if results_by_track:
        with _lock:
            _cache.setdefault(camera_id, {}).update(results_by_track)
    seen: set[Any] = {d.get('track_id') for d in faces}
    with _lock:
        cam_cache = _cache.get(camera_id)
        if cam_cache is not None:
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
    rules = effective_face_detection_rules().get('rules') or []
    if not any(is_unknown_rule(rule) for rule in rules):
        return []
    face_track_ids: set[Any] = set()
    new_alerts: list[dict[str, Any]] = []
    with _lock:
        # Per-camera map of still-present track ids -> the unknown-rule ids
        # that already fired for that track (one alert per rule per track).
        alerted = _alerted_unknown.setdefault(camera_id, {})
        for detection in detections:
            if not _is_face(detection) or not detection.get('recognized'):
                continue
            track_id = detection.get('track_id')
            if track_id is None:
                continue
            face_track_ids.add(track_id)
            if detection.get('person_id') is not None:
                continue
            # Zone-scoped stranger alerts: the live pipeline stamps faces with
            # their containing zone; ``_unknown`` matches everywhere while an
            # ``_unknown:<zone>`` variant only fires inside its own zone.
            det_zone = str(detection.get('zone_id') or '')
            matched_rules = [
                rule for rule in rules
                if is_unknown_rule(rule)
                and _coerce_bool(rule.get('enabled'), False)
                and rule_scope_matches(rule, camera_id, det_zone)
            ]
            if not matched_rules:
                continue
            try:
                det_conf = float(detection.get('confidence') or 0)
            except (TypeError, ValueError):
                det_conf = 0.0
            fired_ids: list[str] = []
            for rule in matched_rules:
                # Per-rule confidence gate: only faces at/above the rule's
                # ``min_confidence`` fire it. Blank = any detected face.
                rule_min_conf = rule.get('min_confidence')
                if rule_min_conf is not None and det_conf < float(rule_min_conf):
                    continue
                # One alert per (rule, track): a stranger who lingers does not
                # re-fire the same zone's rule, but each configured zone gets
                # its own alert when several zones carry unknown-rules.
                rule_id = str(rule.get('id') or '')
                fired_for_track = alerted.setdefault(track_id, set())
                if rule_id in fired_for_track:
                    continue
                fired_for_track.add(rule_id)
                fired_ids.append(rule_id)
            if fired_ids:
                new_alerts.append({**detection, 'face_rule_ids': fired_ids, 'zone_id': det_zone})
        # Forget tracks no longer present so a returning stranger re-alerts and
        # the map cannot grow unbounded.
        _alerted_unknown[camera_id] = {
            tid: rule_ids for tid, rule_ids in alerted.items() if tid in face_track_ids
        }
    return new_alerts


def reset_camera_identities(camera_id: str) -> None:
    """Drop the identity + unknown-alert caches for a camera (stop/reconfigure)."""
    with _lock:
        _cache.pop(camera_id, None)
        _alerted_unknown.pop(camera_id, None)
