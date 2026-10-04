"""Lightweight IoU object tracker.

Assigns a **stable track id** to each detected object across detection cycles, so
the same person/car keeps one identity frame-to-frame instead of being a fresh,
anonymous box every cycle. This is the foundation for de-duplicated events,
dwell-time, and line-crossing, and lets the playback overlay keep a consistent
label on a moving subject.

The tracker is intentionally simple and dependency-free (no Kalman filter / no
Hungarian assignment): globally ranked IoU matching within the same object
label, then a motion-gated nearest-center fallback (``MOTION_MATCH_GATE``) for
subjects that moved further than their own box since the last cycle. That is
plenty for the per-camera detection cadence here, and it never blocks or
allocates on a hot path beyond a handful of small dict/list operations.

Contract: :func:`update_object_tracks` takes the per-camera detection list and
returns the SAME detections, each annotated with:

- ``track_id``   -- stable integer id for this object on this camera,
- ``track_age``  -- how many cycles this track has been seen (1 on first sight),
- ``track_new``  -- ``True`` only on the cycle a track first appears,
- ``track_displacement`` -- normalized (0-1 frame) Chebyshev motion of the box
  over the most recent up-to ``TRACK_DISPLACEMENT_HISTORY`` observations: the
  larger of the box-center translation and the box's own growth/shrink. The
  scale term matters because a subject walking toward (or a car driving away
  from) the camera barely moves its box *center* while the box scales, so a
  center-only measure called a plainly moving subject *still*. ``None`` until
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

import app.box_geometry as box_geometry
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
# Detector box-size jitter, as a fraction of the box's own larger side. Edge
# jitter is proportional to box size, so a fixed threshold on an absolute size
# change is size-dependent: a large parked box (a car close to the lens) crosses
# it while a small one does not. Both a real depth-axis subject and the detector
# noise scale with the box, so the scale term is measured and thresholded in
# RELATIVE units -- see ``TRACK_SIZE_REFERENCE``.
TRACK_SIZE_JITTER_FRACTION = 0.05
# Converts a RELATIVE size change into the normalized-frame units the still
# threshold uses: ``TRACK_SIZE_JITTER_FRACTION`` of relative growth is exactly
# ``TRACK_STILL_DISPLACEMENT`` of signal. This keeps the verdict independent of
# how large the box happens to be -- a subject that moves 5% closer grows its
# box 5% whether it is 3 m or 20 m away -- and keeps the noise band (a few
# percent of the box) below the threshold without a separate gate.
TRACK_SIZE_REFERENCE = TRACK_STILL_DISPLACEMENT / TRACK_SIZE_JITTER_FRACTION
# Extent instability: the detector drawing the SAME object at wildly different
# sizes. Observed on real footage (event 47720): one cycle boxed a parked car's
# whole body (0.506 x 0.429), the next only its roof (0.319 x 0.111) -- 2.4x
# apart in aspect ratio, 6.1x in area, centre shifted 0.178 of the frame, and
# the object had not moved at all. Both the centre and the scale measure read
# that as vigorous motion, so a parked car was classified ``moving`` and kept
# by a Moving Only rule.
#
# The signature is a large SHAPE change combined with one box sitting inside
# the other. Neither of the two cases that legitimately move boxes has both:
# a genuinely translating object separates its boxes (low containment), and a
# subject approaching the camera scales its box uniformly (stable aspect).
# The extent-instability test is shared with the detection-confirmation gate,
# so it lives in app.box_geometry. These aliases keep the tracker's own names
# (and the tests that read them) unchanged.
TRACK_ASPECT_DRIFT = box_geometry.ASPECT_DRIFT
TRACK_EXTENT_CONTAINMENT = box_geometry.EXTENT_CONTAINMENT
_box_tuple = box_geometry.box_tuple
_extent_unstable = box_geometry.extent_unstable

# Motion-gated fallback for detections no track overlaps. At a 0.5-1.5s cycle a
# walking person (a narrow box) moves further than its own width between
# cycles, so IoU is zero and every cycle used to open a new track: one person
# crossing the frame carried a new id per sample, breaking playback
# interpolation, dwell and line-crossing alike. A leftover detection may claim
# a same-label track seen within ``MOTION_MATCH_MAX_MISSES`` cycles when its
# center lies within ``MOTION_MATCH_GATE`` x the track box's larger side of
# where the track's last step predicts it, and its area is within
# ``MOTION_MATCH_AREA_RATIO`` of the track's. IoU matches are resolved first and
# are unaffected, so stationary objects keep exactly their previous behaviour.
MOTION_MATCH_GATE = 1.5
MOTION_MATCH_MAX_MISSES = 2
MOTION_MATCH_AREA_RATIO = 2.5
# A track whose measured velocity is below this (normalized units per cycle)
# is stationary, and its gate shrinks to ``MOTION_MATCH_STATIONARY_GATE`` x its
# box side: a parked car cannot jump to the car parked beside it. Without this,
# two adjacent parked cars detected intermittently at night swapped tracks, and
# the jump read as motion under Moving Only (event 47935: a stationary box
# flagged moving). Tracks without a velocity yet (one sighting) keep the full
# gate so a walker's second sighting still matches.
MOTION_MATCH_STATIONARY_SPEED = 0.01
MOTION_MATCH_STATIONARY_GATE = 0.5


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
    """Net normalized motion of a track's box over its recent history.

    Returns the larger of two comparisons between the OLDER half and the NEWER
    half of the last ``TRACK_DISPLACEMENT_HISTORY`` observations:

    - the larger-axis distance between the halves' mean centers, and
    - the RELATIVE change in the halves' mean larger-side length.

    Returns ``0.0`` when the two halves disagree about box SHAPE while one box
    sits inside the other: the same object drawn at two extents has not moved
    (see ``TRACK_ASPECT_DRIFT``). ``None`` when the track has not accumulated
    enough history yet (brand-new track, or a legacy track rebuilt after a
    restart).

    The scale term is not optional: a person walking toward the camera (or a
    car driving away from it) translates very little in image space while its
    box grows steadily, so a center-only measure scored a plainly moving
    subject below the still threshold and the classifier *overrode* the motion
    mask to call it still.

    The scale term is thresholded in relative units (``TRACK_SIZE_REFERENCE``),
    so the verdict is independent of how large the box happens to be.

    Comparing the two halves' *means* rejects per-cycle detector jitter: a
    stationary box wobbles symmetrically around its true center and size, so
    the wobble cancels in each mean and only sustained change moves the halves
    apart. The previous max-deviation-from-the-latest-center measure summed two
    opposite jitter spikes and reported a parked-but-wobbly car (a large box
    whose edges shift a little each frame) as *moving*, so a Moving Only rule
    kept alerting on it.
    """
    boxes = track.get("boxes")
    if not isinstance(boxes, list):
        return None
    window: list[tuple[float, float, float, float]] = []
    for entry in boxes[-TRACK_DISPLACEMENT_HISTORY:]:
        if isinstance(entry, (list, tuple)) and len(entry) >= 4:
            try:
                window.append(tuple(float(value) for value in entry[:4]))
            except (TypeError, ValueError):
                continue
    if len(window) < max(2, TRACK_DISPLACEMENT_MIN_AGE):
        return None
    mid = len(window) // 2
    older = window[:mid] or window[:1]
    newer = window[mid:] or window[-1:]

    def _mean_box(group: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
        count = len(group)
        return tuple(sum(item[axis] for item in group) / count for axis in range(4))

    older_box = _mean_box(older)
    newer_box = _mean_box(newer)

    # The same object drawn at two different extents is not motion. Without
    # this, a roof-only box nested inside a whole-vehicle box moves both the
    # centre and the size enough to clear the threshold, and a parked car is
    # classified moving. See TRACK_ASPECT_DRIFT.
    if _extent_unstable(older_box, newer_box):
        return 0.0

    older_x = older_box[0] + older_box[2] / 2.0
    older_y = older_box[1] + older_box[3] / 2.0
    newer_x = newer_box[0] + newer_box[2] / 2.0
    newer_y = newer_box[1] + newer_box[3] / 2.0
    translation = max(abs(newer_x - older_x), abs(newer_y - older_y))

    older_size = max(older_box[2], older_box[3])
    newer_size = max(newer_box[2], newer_box[3])
    mean_size = (newer_size + older_size) / 2.0
    # The scale term is compared in RELATIVE units: growth divided by the box's
    # own size, rescaled so that TRACK_SIZE_JITTER_FRACTION of relative growth
    # is exactly TRACK_STILL_DISPLACEMENT of signal. An absolute measure would
    # make a small object's identical relative growth read as no motion at all,
    # and would let a large parked box cross the threshold on detector drift.
    if mean_size <= 0.0:
        return translation
    scale_signal = (abs(newer_size - older_size) / mean_size) * TRACK_SIZE_REFERENCE
    return max(translation, scale_signal)


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


def _box_area(box: dict[str, Any]) -> float:
    try:
        return max(0.0, float(box.get("width") or 0.0)) * max(0.0, float(box.get("height") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _motion_match_distance(box: dict[str, Any], track: dict[str, Any]) -> float | None:
    """Gated distance from ``box`` to where ``track`` should be now, or None.

    The prediction extends the track's per-cycle velocity (its last observed
    step divided by the cycles that step spanned, see ``update_object_tracks``)
    by the cycles elapsed since it was seen (``misses + 1``), so a subject
    moving at a steady pace is matched near its expected position rather than
    near its last one, even across intermittent misses. Returns None when the
    pair fails the gate or the size check.
    """
    center = _center_of(box)
    track_box = track.get("box") or {}
    centers = [c for c in (track.get("centers") or []) if isinstance(c, (list, tuple)) and len(c) >= 2]
    if center is None or not centers or int(track.get("misses") or 0) > MOTION_MATCH_MAX_MISSES:
        return None
    area, track_area = _box_area(box), _box_area(track_box)
    if area <= 0 or track_area <= 0:
        return None
    if max(area, track_area) / min(area, track_area) > MOTION_MATCH_AREA_RATIO:
        # Extent instability -- the same object drawn at two very different
        # sizes, one box inside the other -- must not mint a new track id. A
        # fresh id means a young track, and a young track never reaches the
        # displacement override, so the pixel mask alone decides and a parked
        # car caught in a passing car's headlights reads as moving (observed on
        # event 47720). Two genuinely different objects separate their boxes
        # and so still fail this test.
        current = _box_tuple(box)
        previous = _box_tuple(track_box)
        if current is None or previous is None or not _extent_unstable(current, previous):
            return None
    last_x, last_y = float(centers[-1][0]), float(centers[-1][1])
    steps = int(track.get("misses") or 0) + 1
    velocity = track.get("velocity")
    if isinstance(velocity, (list, tuple)) and len(velocity) >= 2:
        last_x += float(velocity[0]) * steps
        last_y += float(velocity[1]) * steps
    distance = ((center[0] - last_x) ** 2 + (center[1] - last_y) ** 2) ** 0.5
    try:
        size = max(float(track_box.get("width") or 0.0), float(track_box.get("height") or 0.0))
    except (TypeError, ValueError):
        return None
    gate = MOTION_MATCH_GATE
    if isinstance(velocity, (list, tuple)) and len(velocity) >= 2:
        speed = (float(velocity[0]) ** 2 + float(velocity[1]) ** 2) ** 0.5
        if speed < MOTION_MATCH_STATIONARY_SPEED:
            gate = MOTION_MATCH_STATIONARY_GATE
    return distance if distance <= gate * size else None


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

        # Motion-gated fallback for what IoU left unmatched (see
        # MOTION_MATCH_GATE), nearest pairs first, same exclusivity rules.
        motion_candidates: list[tuple[float, int, int]] = []
        for detection_index, detection in enumerate(detections):
            box = detection.get("box")
            if detection_index in assigned_detection_indices or not isinstance(box, dict):
                continue
            label = _label_key(detection)
            for track_index, track in enumerate(tracks):
                if track["label"] != label or track["id"] in matched_track_ids:
                    continue
                distance = _motion_match_distance(box, track)
                if distance is not None:
                    motion_candidates.append((distance, detection_index, track_index))
        motion_candidates.sort()
        for _distance, detection_index, track_index in motion_candidates:
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
                # the history double-counts and shifts the age gate). The size
                # history is kept index-aligned with the center history and
                # trimmed identically, so ``_recent_displacement`` can pair
                # each center with the box's scale at that same sighting.
                best_track["box"] = box if isinstance(box, dict) else best_track["box"]
                centers = best_track.setdefault("centers", [])
                boxes_hist = best_track.setdefault("boxes", [])
                new_center = _center_of(box) if isinstance(box, dict) else None
                if new_center is not None:
                    if centers:
                        # Per-cycle velocity: this step spans the cycles missed
                        # since the last sighting plus this one.
                        elapsed = int(best_track.get("misses") or 0) + 1
                        best_track["velocity"] = (
                            (new_center[0] - float(centers[-1][0])) / elapsed,
                            (new_center[1] - float(centers[-1][1])) / elapsed,
                        )
                    centers.append(new_center)
                    box_tuple = _box_tuple(box) if isinstance(box, dict) else None
                    if box_tuple is not None:
                        boxes_hist.append(box_tuple)
                del centers[:-TRACK_DISPLACEMENT_HISTORY]
                del boxes_hist[:-TRACK_DISPLACEMENT_HISTORY]
                best_track["hits"] += 1
                best_track["misses"] = 0
                best_track["last_ts"] = now
                matched_track_ids.add(best_track["id"])
                detection["track_id"] = best_track["id"]
                detection["track_age"] = best_track["hits"]
                detection["track_new"] = False
                detection["track_displacement"] = _recent_displacement(best_track)
                # This cycle's centre and the previous one, so a behavioural
                # consumer (line-crossing) can test the ``prev -> curr`` step
                # without reaching into tracker state. ``None`` prev on the
                # first two sights (need two points to define a crossing).
                detection["track_center"] = centers[-1] if centers else None
                detection["track_prev_center"] = centers[-2] if len(centers) >= 2 else None
                detection["track_box"] = boxes_hist[-1] if boxes_hist else None
                detection["track_prev_box"] = boxes_hist[-2] if len(boxes_hist) >= 2 else None
            else:
                track_id = state["next_id"]
                state["next_id"] += 1
                tracks.append({
                    "id": track_id,
                    "label": label,
                    "box": box if isinstance(box, dict) else {},
                    "centers": [_center_of(box)] if isinstance(box, dict) else [],
                    "boxes": [_box_tuple(box)] if isinstance(box, dict) else [],
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
                detection["track_center"] = _center_of(box) if isinstance(box, dict) else None
                detection["track_prev_center"] = None
                detection["track_box"] = _box_tuple(box) if isinstance(box, dict) else None
                detection["track_prev_box"] = None

        # Age out tracks that were not matched this cycle.
        survivors = []
        for track in tracks:
            if track["id"] not in matched_track_ids:
                track["misses"] += 1
            if track["misses"] <= max_age:
                survivors.append(track)
        state["tracks"] = survivors

    return detections
