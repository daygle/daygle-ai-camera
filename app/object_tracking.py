"""Lightweight IoU object tracker.

Assigns a **stable track id** to each detected object across detection cycles, so
the same person/car keeps one identity frame-to-frame instead of being a fresh,
anonymous box every cycle. This is the foundation for de-duplicated events,
dwell-time, and line-crossing, and lets the playback overlay keep a consistent
label on a moving subject.

The tracker is intentionally simple and dependency-free (no Kalman filter / no
Hungarian assignment): greedy IoU matching within the same object label. That is
plenty for the 2-4 Hz per-camera detection cadence here, and it never blocks or
allocates on a hot path beyond a handful of small dict/list operations.

Contract: :func:`update_object_tracks` takes the per-camera detection list and
returns the SAME detections, each annotated with:

- ``track_id``   -- stable integer id for this object on this camera,
- ``track_age``  -- how many cycles this track has been seen (1 on first sight),
- ``track_new``  -- ``True`` only on the cycle a track first appears.

It never drops or reorders detections, so every existing consumer keeps working;
callers that don't care about tracking can ignore the extra keys.
"""
from __future__ import annotations

import time
from typing import Any

import app.state as _state


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

    Greedy IoU matching within the same label: each detection takes the
    highest-IoU unmatched track of its label (>= ``iou_threshold``) or opens a
    new track. Tracks unseen for more than ``max_age`` consecutive cycles are
    dropped. Returns the same detection dicts, annotated with ``track_id`` /
    ``track_age`` / ``track_new`` (see module docstring)."""
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

        for detection in detections:
            box = detection.get("box")
            label = _label_key(detection)
            best_track = None
            best_iou = iou_threshold
            if isinstance(box, dict):
                for track in tracks:
                    if track["id"] in matched_track_ids or track["label"] != label:
                        continue
                    score = _iou(box, track["box"])
                    if score >= best_iou:
                        best_iou = score
                        best_track = track
            if best_track is not None:
                best_track["box"] = box if isinstance(box, dict) else best_track["box"]
                best_track["hits"] += 1
                best_track["misses"] = 0
                best_track["last_ts"] = now
                matched_track_ids.add(best_track["id"])
                detection["track_id"] = best_track["id"]
                detection["track_age"] = best_track["hits"]
                detection["track_new"] = False
            else:
                track_id = state["next_id"]
                state["next_id"] += 1
                tracks.append({
                    "id": track_id,
                    "label": label,
                    "box": box if isinstance(box, dict) else {},
                    "hits": 1,
                    "misses": 0,
                    "first_ts": now,
                    "last_ts": now,
                })
                matched_track_ids.add(track_id)
                detection["track_id"] = track_id
                detection["track_age"] = 1
                detection["track_new"] = True

        # Age out tracks that were not matched this cycle.
        survivors = []
        for track in tracks:
            if track["id"] not in matched_track_ids:
                track["misses"] += 1
            if track["misses"] <= max_age:
                survivors.append(track)
        state["tracks"] = survivors

    return detections
