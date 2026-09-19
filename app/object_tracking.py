"""Lightweight IoU object tracker.

Assigns a **stable track id** to each detected object across detection cycles, so
the same person/car keeps one identity frame-to-frame instead of being a fresh,
anonymous box every cycle. This is the foundation for de-duplicated events,
dwell-time, and line-crossing, and lets the playback overlay keep a consistent
label on a moving subject.

The tracker is intentionally simple and dependency-free (no Kalman filter / no
Hungarian assignment): globally ranked IoU matching within the same object
label. That is plenty for the 2-4 Hz per-camera detection cadence here, and it
never blocks or allocates on a hot path beyond a handful of small dict/list
operations.

Contract: :func:`update_object_tracks` takes the per-camera detection list and
returns the SAME detections, each annotated with:

- ``track_id``   -- stable integer id for this object on this camera,
- ``track_age``  -- how many cycles this track has been seen (1 on first sight),
- ``track_new``  -- ``True`` only on the cycle a track first appears,
- ``track_displacement`` -- normalized (0-1 frame) Chebyshev distance between
  the current box center and the most recent up-to
  ``TRACK_DISPLACEMENT_HISTORY`` box centers of the same track. ``None`` until
  the track has ``TRACK_DISPLACEMENT_MIN_AGE`` cycles of box history. The
  still/moving classifier (``app/object_settings.py``) reads this to override
  the motion-mask verdict: a track whose box has not moved is *still* even when
  background motion inside its large box crosses the mask threshold, and a
  track that has swept across the frame is *moving* even when the mask is
  unavailable. Tracks that predate a process restart re-accumulate history and
  report ``None`` (mask-only classification) until then.

It never drops or reorders detections, so every existing consumer keeps working;
callers that don't care about tracking can ignore the extra keys.
"""
from __future__ import annotations

import time
from typing import Any

import app.state as _state


# Displacement history knobs. ``TRACK_DISPLACEMENT_HISTORY`` bounds how many
# recent box centers are kept per track (memory + staleness); measuring over a
# window rather than consecutive cycles makes the verdict robust to per-cycle
# jitter while still responding within a few cycles. ``MIN_AGE`` is the number
# of matched cycles required before a displacement is considered trustworthy.
# ``TRACK_STILL_DISPLACEMENT`` is the normalized distance below which a track
# counts as stationary (the classification threshold lives in
# ``app/object_settings.py``; this copy is for the tracker-side docstring).
TRACK_DISPLACEMENT_HISTORY = 8
TRACK_DISPLACEMENT_MIN_AGE = 3
TRACK_STILL_DISPLACEMENT = 0.01


def _center_of(box: dict[str, Any]) -> tuple[float, float] | None:
    """Return the normalized center ``(cx, cy)`` of a detection box, or None."""
    try:
        x = float(box.get("x") or 0.0)
        y = float(box.get("y") or 0.0)
        w = float(box.get("width") or 0.0)
        h = float(box.get("height") or 0.0)
    except (TypeError, ValueError):
        return None
    return (x + w / 2.0, y + h / 2.0)


def _recent_displacement(track: dict[str, Any]) -> float | None:
    """Net normalized motion of a track over its recent center history.

    Returns the largest axis distance between the current box center and any
    of the last ``TRACK_DISPLACEMENT_HISTORY`` centers, or ``None`` when the
    track has not accumulated enough history yet (brand-new track, or a legacy
    track rebuilt after a restart).
    """
    centers = track.get("centers")
    if not isinstance(centers, list) or len(centers) < max(2, TRACK_DISPLACEMENT_MIN_AGE):
        return None
    recent = centers[-TRACK_DISPLACEMENT_HISTORY:]
    last_x, last_y = recent[-1]
    displacement = 0.0
    for center in recent:
        try:
            cx, cy = float(center[0]), float(center[1])
        except (TypeError, ValueError, IndexError):
            continue
        displacement = max(displacement, abs(cx - last_x), abs(cy - last_y))
    return displacement


def _iou(box_a: dict[str, Any], box_b: dict[str, Any]) -> float:
    """Intersection-over-union of two normalized ``{x,y,width,height}`` boxes."""
    ax1 = float(box_a.get("x") or 0.0)
    ay1 = float(box_a.get("y") or 0.0)
    ax2 = ax1 + max(0.0, float(box_a.get("width") or 0.0))
    ay2 = ay1 + max(0.0, float(box_a.get("height") or 0.0))
    bx1 = float(box_b.get("x") or 0.0)
    by1 = float(box_b.get("y") or 0.0)
    bx2 = bx1 + max(0.0, float(box_b.get("width") or 0.0))
    by2 = by1 + max(0.0, float(box_b.get("height") or 0.0))
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = ix2 - ix1
    ih = iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    intersection = iw * ih
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _label_key(detection: dict[str, Any]) -> str:
    return str(detection.get("label") or "").strip().lower()


def update_object_tracks(
    camera_id: str,
    detections: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.3,
    max_age: int = 5,
) -> list[dict[str, Any]]:
    """Assign/refresh stable track ids for ``detections`` on ``camera_id``.

    Globally ranked IoU matching within the same label: the strongest
    non-conflicting detection/track pairs (>= ``iou_threshold``) are assigned
    first, or an unmatched detection opens a new track. Tracks unseen for more
    than ``max_age`` consecutive cycles are
    dropped. Returns the same detection dicts, annotated with ``track_id`` /
    ``track_age`` / ``track_new`` / ``track_displacement`` (see module
    docstring)."""
    if not detections:
        # Still age out existing tracks on an empty cycle so a track that left
        # the frame is retired instead of lingering forever.
        with _state._object_tracks_lock:
            state = _state._object_tracks.get(camera_id)
            if state:
                survivors = []
                for track in state["tracks"]:
                    track["misses"] += 1
                    if track["misses"] <= max_age:
                        survivors.append(track)
                state["tracks"] = survivors
        return detections

    now = time.time()
    with _state._object_tracks_lock:
        state = _state._object_tracks.get(camera_id)
        if state is None:
            state = {"tracks": [], "next_id": 1}
            _state._object_tracks[camera_id] = state
        tracks: list[dict[str, Any]] = state["tracks"]
        matched_track_ids: set[int] = set()

        # Build all viable same-label matches before mutating any track. The
        # previous detection-by-detection greedy loop made assignment depend on
        # detector output order: with two cars, the first car in a reordered
        # result could claim the other car's track and inherit its moving/still
        # history. Resolving the strongest IoUs globally for this cycle keeps
        # track identity stable when two same-label boxes approach or cross.
        candidates: list[tuple[float, int, int]] = []
        for detection_index, detection in enumerate(detections):
            box = detection.get("box")
            if not isinstance(box, dict):
                continue
            label = _label_key(detection)
            for track_index, track in enumerate(tracks):
                if track["label"] != label:
                    continue
                score = _iou(box, track["box"])
                if score >= iou_threshold:
                    candidates.append((score, detection_index, track_index))
        candidates.sort(reverse=True)
        assignments: dict[int, dict[str, Any]] = {}
        assigned_detection_indices: set[int] = set()
        for _score, detection_index, track_index in candidates:
            track = tracks[track_index]
            if detection_index in assigned_detection_indices or track["id"] in matched_track_ids:
                continue
            assignments[detection_index] = track
            assigned_detection_indices.add(detection_index)
            matched_track_ids.add(track["id"])

        # Apply the precomputed assignments in input order so the returned list
        # remains in detector order; only ownership of a track is order-free.
        matched_track_ids.clear()
        for detection_index, detection in enumerate(detections):
            box = detection.get("box")
            label = _label_key(detection)
            best_track = assignments.get(detection_index)
            if best_track is not None:
                matched_track_ids.add(best_track["id"])
                # Extend the bounded center history with THIS cycle's center;
                # the previous center is already stored from the cycle that
                # observed it (append-on-observe, never append-on-match, or
                # the history double-counts and shifts the age gate).
                best_track["box"] = box if isinstance(box, dict) else best_track["box"]
                centers = best_track.setdefault("centers", [])
                new_center = _center_of(box) if isinstance(box, dict) else None
                if new_center is not None:
                    centers.append(new_center)
                del centers[:-TRACK_DISPLACEMENT_HISTORY]
                best_track["hits"] += 1
                best_track["misses"] = 0
                best_track["last_ts"] = now
                matched_track_ids.add(best_track["id"])
                detection["track_id"] = best_track["id"]
                detection["track_age"] = best_track["hits"]
                detection["track_new"] = False
                detection["track_displacement"] = _recent_displacement(best_track)
            else:
                track_id = state["next_id"]
                state["next_id"] += 1
                tracks.append({
                    "id": track_id,
                    "label": label,
                    "box": box if isinstance(box, dict) else {},
                    "centers": [_center_of(box)] if isinstance(box, dict) else [],
                    "hits": 1,
                    "misses": 0,
                    "first_ts": now,
                    "last_ts": now,
                })
                matched_track_ids.add(track_id)
                detection["track_id"] = track_id
                detection["track_age"] = 1
                detection["track_new"] = True
                # One center is not motion evidence; the classifier falls back
                # to the motion mask until the track has enough history.
                detection["track_displacement"] = None

        # Age out tracks that were not matched this cycle.
        survivors = []
        for track in tracks:
            if track["id"] not in matched_track_ids:
                track["misses"] += 1
            if track["misses"] <= max_age:
                survivors.append(track)
        state["tracks"] = survivors

    return detections
