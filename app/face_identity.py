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

import logging
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

logger = logging.getLogger('daygle.ai')

_FACE_LABEL = 'face'

# Per-camera identity cache: camera_id -> {track_id: MatchResult | None}.
# ``None`` means "recognised as unknown so far" and is retried each cycle; a
# MatchResult is cached and reused for the life of the track.
_cache: dict[str, dict[Any, Any]] = {}
# Per-camera set of unknown-face track ids already alerted, so a lingering
# stranger raises one alert rather than one per ~4 Hz cycle.
_alerted_unknown: dict[str, set[Any]] = {}
# Per-camera set of unknown-face track ids already captured for review.
_captured_unknown: dict[str, set[Any]] = {}
# Per-person set of track ids already enriched, to avoid re-enriching the
# same face on every cycle.
_enriched_tracks: dict[str, set[Any]] = {}
_lock = threading.Lock()

# Auto-enrichment: store new embeddings for recognized faces to improve
# match accuracy over time. Only high-confidence matches are enriched.
_ENRICH_MIN_SCORE = 0.7  # minimum identity score to trigger enrichment
_ENRICH_MAX_PER_PERSON = 20  # max auto-enriched embeddings per person (per model)


def _maybe_enrich_person(
    camera_id: str,
    track_id: Any,
    detection: dict[str, Any],
    crop_bgr: Any,
    result: Any,
    service: Any,
) -> None:
    """Store a new embedding for a recognized person to improve accuracy.

    Only triggers when:
    - The match confidence is above _ENRICH_MIN_SCORE
    - This track hasn't already been enriched this session
    - The person doesn't already have too many embeddings for this model
    """
    if result is None or track_id is None or crop_bgr is None:
        return
    person_id = result.person_id
    if not person_id:
        return
    score = float(result.score or 0)
    if score < _ENRICH_MIN_SCORE:
        return
    # One enrichment per track to avoid flooding.
    person_key = str(person_id)
    with _lock:
        enriched = _enriched_tracks.setdefault(person_key, set())
        if track_id in enriched:
            return
        enriched.add(track_id)
    # Offload to background thread to avoid blocking the hot path.
    threading.Thread(
        target=_store_enriched_embedding,
        args=(person_id, camera_id, track_id, detection, crop_bgr, service),
        daemon=True,
    ).start()


def _store_enriched_embedding(
    person_id: int,
    camera_id: str,
    track_id: Any,
    detection: dict[str, Any],
    crop_bgr: Any,
    service: Any,
) -> None:
    """Background: embed the face and store it for the recognized person."""
    try:
        import app.state as _state
        from app.face_recognition import embedding_to_bytes
        if _state.database is None or crop_bgr is None:
            return
        model = str(service.model_id)
        faces = _state.database.list_person_faces(int(person_id))
        if len(faces) >= _ENRICH_MAX_PER_PERSON:
            return
        embedding = service.embed_face(crop_bgr)
        if embedding is None:
            return
        emb_bytes = embedding_to_bytes(embedding)
        dim = int(embedding.shape[0])
        _state.database.add_person_face(
            int(person_id),
            embedding=emb_bytes,
            dim=dim,
            model=model,
            source_snapshot=f'auto-enrich:cam={camera_id},track={track_id}',
        )
        # Refresh the matcher so the new embedding is active immediately.
        from app.face_recognition_service import refresh_face_recognition_matcher
        refresh_face_recognition_matcher()
        logger.debug('Enriched person %s with new face embedding from track %s', person_id, track_id)
    except Exception as exc:
        logger.debug('Failed to enrich face for person %s: %s', person_id, exc)


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


# Maximum unknown-face captures per camera to prevent unbounded growth.
_MAX_CAPTURES_PER_CAMERA = 200


def _maybe_capture_unknown(
    camera_id: str,
    track_id: Any,
    detection: dict[str, Any],
    crop_bgr: Any,
    service: Any,
) -> None:
    """Capture an unknown face for the Review workflow (first time per track)."""
    # Only capture the first unknown appearance per track.
    with _lock:
        captured = _captured_unknown.setdefault(camera_id, set())
        if track_id in captured:
            return
        captured.add(track_id)
    # Enforce per-camera cap to prevent unbounded growth.
    try:
        import app.state as _state
        if _state.database is not None:
            count = _state.database.count_unknown_faces(status='pending')
            if count >= _MAX_CAPTURES_PER_CAMERA:
                return
    except Exception:
        pass  # non-fatal: skip capture if DB unavailable
    # Embed + thumbnail off the hot path (already computed by recognize, but
    # the embedding isn't stored by the matcher). We re-embed here only once
    # per track since the first unknown appearance is the only capture.
    threading.Thread(
        target=_store_unknown_face,
        args=(camera_id, track_id, detection, crop_bgr, service),
        daemon=True,
    ).start()


def _store_unknown_face(
    camera_id: str,
    track_id: Any,
    detection: dict[str, Any],
    crop_bgr: Any,
    service: Any,
) -> None:
    """Background: embed the face, generate a thumbnail, and store to DB."""
    try:
        import app.state as _state
        from app.face_recognition import embedding_to_bytes, encode_face_thumbnail
        if _state.database is None or crop_bgr is None:
            return
        embedding = service.embed_face(crop_bgr)
        if embedding is None:
            return
        emb_bytes = embedding_to_bytes(embedding)
        dim = int(embedding.shape[0])
        thumbnail = encode_face_thumbnail(crop_bgr)
        box = detection.get('box') or {}
        _state.database.store_unknown_face(
            camera_id=camera_id,
            zone_id=detection.get('zone_id'),
            track_id=str(track_id) if track_id is not None else None,
            embedding=emb_bytes,
            dim=dim,
            model=str(service.model_id),
            thumbnail=thumbnail,
            confidence=float(detection.get('confidence') or 0),
            box_x=float(box.get('x', 0)),
            box_y=float(box.get('y', 0)),
            box_width=float(box.get('width', 0)),
            box_height=float(box.get('height', 0)),
        )
        logger.debug('Captured unknown face track %s on camera %s', track_id, camera_id)
    except Exception as exc:
        logger.debug('Failed to capture unknown face: %s', exc)


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
        # Capture the first unknown face per track for the Review workflow.
        if result is None and crop is not None and track_id is not None:
            _maybe_capture_unknown(camera_id, track_id, detection, crop, service)
        # Auto-enrich: store a new embedding for high-confidence matches.
        if result is not None and crop is not None and track_id is not None:
            _maybe_enrich_person(camera_id, track_id, detection, crop, result, service)

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
        # Prune captured-unknown set too so a returning person can be captured again.
        cap_set = _captured_unknown.get(camera_id)
        if cap_set is not None:
            _captured_unknown[camera_id] = cap_set - seen
        # Prune enriched-tracks per-person sets for tracks no longer seen.
        stale_pids = {str(d.get('person_id')) for d in faces if d.get('person_id') is not None}
        # Only prune tracks we know about; leave others untouched.
        for pid in list(_enriched_tracks.keys()):
            if pid in stale_pids:
                _enriched_tracks[pid] = _enriched_tracks[pid] - {d.get('track_id') for d in faces if str(d.get('person_id')) == pid}
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
        _captured_unknown.pop(camera_id, None)
