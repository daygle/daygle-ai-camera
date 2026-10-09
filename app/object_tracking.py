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

# Anchored stationary tracks. The windowed displacement compares the two halves
# of the last ``TRACK_DISPLACEMENT_HISTORY`` boxes, so a transient change in a
# parked car's box - a passing car hiding part of it for a cycle or two, or
# headlight glare reshaping it - stays in the window and reads "moving" for
# several cycles after the box is back exactly where it was (event 48173: two
# occluded cycles, then six "moving" samples on an unmoved box). Once a track has
# read still for ``ANCHOR_STILL_CYCLES`` consecutive sightings it is anchored to
# its settled box (counting only sightings with a full history window, so a
# young track's slow approach is never anchored early): a sighting within
# ``ANCHOR_TOLERANCE`` of the anchor is still
# whatever the window holds, and the anchor is only released - handing back to
# the windowed measure - when the box departs from it for
# ``ANCHOR_RELEASE_CYCLES`` consecutive sightings. A sighting while another
# detection covers the anchor (something passing in front) does not count
# toward release: the box is unreliable then. A car that really pulls out
# departs steadily and reads moving after the release cycles.
ANCHOR_STILL_CYCLES = 4
ANCHOR_TOLERANCE = 2 * TRACK_STILL_DISPLACEMENT
ANCHOR_RELEASE_CYCLES = 3
ANCHOR_OCCLUSION_OVERLAP = 0.2
# A redrawn parked car is not a departure. At night the detector can redraw a
# parked car when a passing car's headlights light it - recording 17783: a
# minivan anchored for the evening was boxed as just its lit roof, a box nested
# inside its settled one but not different enough in shape to count as an
# extent flip. Every roof sighting counted toward release, so after three the
# anchor let go and the unmoved car read "moving" until the window refilled. A
# departing box nested in the anchor (``EXTENT_CONTAINMENT`` of the smaller box
# inside the larger) that holds within ``TRACK_STILL_DISPLACEMENT`` of the first
# departing box is that redraw, and does not count toward release. A car that
# really pulls out keeps moving from one sighting to the next, so it leaves the
# first departing box behind and is released as before, one sighting later.
ANCHOR_REDRAW_TOLERANCE = TRACK_STILL_DISPLACEMENT
# Re-acquiring a parked car. A distant car at night hovers around the
# detector's confidence threshold, so it can go unseen for more than ``max_age``
# cycles and its track is dropped. It then came back as a brand-new track, and a
# new track has no history, so the motion mask alone decided moving/still: a
# moth or rain streak crossing it that cycle read the parked car as moving
# (recording 21122: a parked car tagged moving on track 1584 as a moth flew
# past). An anchored track that ages out is kept for ``REVIVE_SECONDS``; a new
# detection of the same label within ``ANCHOR_TOLERANCE`` of its anchor takes
# the old track back - id, history and anchor - so it stays still. A car really
# moving through that spot does not line up with the old box that closely, and
# one that does is released by the usual anchor rules once it keeps going.
REVIVE_SECONDS = 600.0
REVIVE_MAX_TRACKS = 32


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
    # Drop sightings drawn at a different EXTENT from the window's prevailing
    # one (the box of median aspect). The pairwise check below only sees the
    # two halves' summaries: one roof-only box among whole-vehicle boxes blends
    # into an in-between summary that no longer reads as an extent change, yet
    # shifts the summary centre by several hundredths of the frame, so a single
    # flip kept a parked car "moving" for most of the window.
    reference = sorted(window, key=lambda item: item[2] / max(item[3], 1e-9))[len(window) // 2]
    window = [item for item in window if not _extent_unstable(item, reference)]
    if len(window) < max(2, TRACK_DISPLACEMENT_MIN_AGE):
        return 0.0  # flapping between extents is not evidence of motion
    mid = len(window) // 2
    older = window[:mid] or window[:1]
    newer = window[mid:] or window[-1:]

    def _median_box(group: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
        # Per-axis median, not mean: one outlying sighting (a glare-inflated or
        # half-occluded box) cannot drag a half's summary across the threshold,
        # while sustained translation or growth still separates the halves.
        def _median(values: list[float]) -> float:
            ordered = sorted(values)
            middle = len(ordered) // 2
            return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
        return tuple(_median([item[axis] for item in group]) for axis in range(4))

    older_box = _median_box(older)
    newer_box = _median_box(newer)

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
    current, previous = _box_tuple(box), _box_tuple(track_box)
    if current is not None and previous is not None and _extent_unstable(current, previous):
        # The same object drawn at another extent, one box inside the other:
        # containment already proves the place, so no distance gate applies -
        # in particular not the stationary one, which a parked car's roof/body
        # flip (centres ~0.18 of the frame apart) would otherwise fail.
        return distance
    gate = MOTION_MATCH_GATE
    if isinstance(velocity, (list, tuple)) and len(velocity) >= 2:
        speed = (float(velocity[0]) ** 2 + float(velocity[1]) ** 2) ** 0.5
        if speed < MOTION_MATCH_STATIONARY_SPEED:
            gate = MOTION_MATCH_STATIONARY_GATE
    return distance if distance <= gate * size else None


def _box_deviation(
    current: tuple[float, float, float, float],
    reference: tuple[float, float, float, float],
) -> float:
    """Translation-or-scale change between two boxes, in still-threshold units.

    The same measure the windowed displacement applies to its two halves: the
    larger-axis centre move, or the relative change of the larger side scaled
    by ``TRACK_SIZE_REFERENCE``. Zero for the same object at another extent.
    """
    if _extent_unstable(current, reference):
        return 0.0
    translation = max(
        abs((current[0] + current[2] / 2.0) - (reference[0] + reference[2] / 2.0)),
        abs((current[1] + current[3] / 2.0) - (reference[1] + reference[3] / 2.0)),
    )
    current_size, reference_size = max(current[2], current[3]), max(reference[2], reference[3])
    mean_size = (current_size + reference_size) / 2.0
    if mean_size <= 0.0:
        return translation
    return max(translation, abs(current_size - reference_size) / mean_size * TRACK_SIZE_REFERENCE)


def _covered(anchor: tuple[float, float, float, float], others: list[tuple[float, float, float, float]]) -> bool:
    """Whether another detection's box covers a meaningful part of ``anchor``."""
    ax, ay, aw, ah = anchor
    area = aw * ah
    if area <= 0.0:
        return False
    for ox, oy, ow, oh in others:
        overlap = max(0.0, min(ax + aw, ox + ow) - max(ax, ox)) * max(0.0, min(ay + ah, oy + oh) - max(ay, oy))
        if overlap / area >= ANCHOR_OCCLUSION_OVERLAP:
            return True
    return False


def _nested(
    box: tuple[float, float, float, float],
    anchor: tuple[float, float, float, float],
) -> bool:
    """Whether the smaller of two boxes lies (mostly) inside the larger."""
    bx, by, bw, bh = box
    ax, ay, aw, ah = anchor
    smaller = min(bw * bh, aw * ah)
    if smaller <= 0.0:
        return False
    overlap = max(0.0, min(bx + bw, ax + aw) - max(bx, ax)) * max(0.0, min(by + bh, ay + ah) - max(by, ay))
    return overlap / smaller >= box_geometry.EXTENT_CONTAINMENT


def _anchored_displacement(
    track: dict[str, Any],
    box: tuple[float, float, float, float] | None,
    others: list[tuple[float, float, float, float]],
) -> float | None:
    """Displacement for the classifier, robust to transient changes once settled.

    Updates the track's anchor state (see ``ANCHOR_STILL_CYCLES``) and returns
    0.0 while an anchored track's box is within tolerance - or departs only
    transiently - otherwise the windowed displacement.
    """
    windowed = _recent_displacement(track)
    anchor = track.get("anchor")
    if anchor is not None and box is not None:
        if _box_deviation(box, anchor) <= ANCHOR_TOLERANCE:
            track["anchor_breaks"] = 0
            track.pop("departure", None)
            return 0.0
        # The first box away from the anchor; later departures are measured
        # against it to tell a steady redraw from a car that keeps moving.
        departure = track.get("departure")
        if departure is None:
            track["departure"] = box
        redrawn = _nested(box, anchor) and (
            departure is None or _box_deviation(box, departure) <= ANCHOR_REDRAW_TOLERANCE
        )
        if not redrawn and not _covered(anchor, others):
            track["anchor_breaks"] = int(track.get("anchor_breaks") or 0) + 1
        if int(track.get("anchor_breaks") or 0) < ANCHOR_RELEASE_CYCLES:
            return 0.0
        # Sustained departure: the subject really moved. Release the anchor and
        # report what the history says.
        track.pop("anchor", None)
        track.pop("departure", None)
        track["anchor_breaks"] = 0
        track["still_streak"] = 0
        return windowed
    full_window = len(track.get("boxes") or []) >= TRACK_DISPLACEMENT_HISTORY
    if windowed is not None and windowed <= TRACK_STILL_DISPLACEMENT and full_window:
        # Only a full window counts: a young track's first still readings can
        # be a slow, distant approach that has not yet cleared the threshold.
        track["still_streak"] = int(track.get("still_streak") or 0) + 1
        if track["still_streak"] >= ANCHOR_STILL_CYCLES:
            boxes = [b for b in (track.get("boxes") or [])[-TRACK_DISPLACEMENT_HISTORY:] if len(b) >= 4]
            if boxes:
                def _median(values: list[float]) -> float:
                    ordered = sorted(values)
                    middle = len(ordered) // 2
                    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
                track["anchor"] = tuple(_median([float(b[axis]) for b in boxes]) for axis in range(4))
                track["anchor_breaks"] = 0
    else:
        track["still_streak"] = 0
    return windowed


def _retire(state: dict[str, Any], track: dict[str, Any], now: float) -> None:
    """Keep an anchored (parked) track that aged out, so it can be revived."""
    if track.get("anchor") is None:
        return
    retired = [
        entry for entry in state.get("retired") or []
        if now - float(entry.get("retired_ts") or 0.0) <= REVIVE_SECONDS
    ]
    retired.append({**track, "retired_ts": now})
    state["retired"] = retired[-REVIVE_MAX_TRACKS:]


def _revive(state: dict[str, Any], label: str, box: tuple[float, float, float, float] | None, now: float) -> dict[str, Any] | None:
    """Take back a retired parked track whose anchor this box sits on."""
    if box is None:
        return None
    best: tuple[float, int] | None = None
    retired = state.get("retired") or []
    for index, entry in enumerate(retired):
        if entry.get("label") != label or now - float(entry.get("retired_ts") or 0.0) > REVIVE_SECONDS:
            continue
        anchor = entry.get("anchor")
        if anchor is None:
            continue
        deviation = _box_deviation(box, tuple(anchor))
        if deviation <= ANCHOR_TOLERANCE and (best is None or deviation < best[0]):
            best = (deviation, index)
    if best is None:
        return None
    track = retired.pop(best[1])
    track.pop("retired_ts", None)
    track["misses"] = 0
    track["anchor_breaks"] = 0
    track.pop("departure", None)
    track.pop("velocity", None)
    return track


def _label_key(detection: dict[str, Any]) -> str:
    return str(detection.get("label") or "").strip().lower()


def update_object_tracks(
    camera_id: str,
    detections: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.3,
    max_age: int = 5,
    hold_labels: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Assign/refresh stable track ids for ``detections`` on ``camera_id``.

    Globally ranked IoU matching within the same label: the strongest
    non-conflicting detection/track pairs (>= ``iou_threshold``) are assigned
    first, or an unmatched detection opens a new track. Tracks unseen for more
    than ``max_age`` consecutive cycles are
    dropped. Returns the same detection dicts, annotated with ``track_id`` /
    ``track_age`` / ``track_new`` / ``track_displacement`` (see module
    docstring).

    ``hold_labels`` are labels whose detector did not run this cycle (the face
    model runs on its own, slower clock): their tracks are not aged, since an
    absent face on such a cycle says nothing about whether it left. Without
    this, a face interval longer than ``max_age`` detection cycles would give
    the same face a new track id on every face pass, defeating every
    per-track guard (one stranger alert per track, one Review capture per
    track, the identity cache)."""
    if not detections:
        # Still age out existing tracks on an empty cycle so a track that left
        # the frame is retired instead of lingering forever.
        with _state._object_tracks_lock:
            state = _state._object_tracks.get(camera_id)
            if state:
                survivors = []
                now = time.time()
                for track in state["tracks"]:
                    if track["label"] not in hold_labels:
                        track["misses"] += 1
                    if track["misses"] <= max_age:
                        survivors.append(track)
                    else:
                        _retire(state, track, now)
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

        # A detection nothing matched may be a parked car that was lost for a
        # while: give it its old track back (see REVIVE_SECONDS).
        for detection_index, detection in enumerate(detections):
            box = detection.get("box")
            if detection_index in assigned_detection_indices or not isinstance(box, dict):
                continue
            revived = _revive(state, _label_key(detection), _box_tuple(box), now)
            if revived is not None:
                tracks.append(revived)
                assignments[detection_index] = revived
                assigned_detection_indices.add(detection_index)

        # Apply the precomputed assignments in input order so the returned list
        # remains in detector order; only ownership of a track is order-free.
        matched_track_ids.clear()
        cycle_boxes = [_box_tuple(d.get("box")) if isinstance(d.get("box"), dict) else None for d in detections]
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
                previous_box = _box_tuple(best_track.get("box") or {})
                best_track["box"] = box if isinstance(box, dict) else best_track["box"]
                centers = best_track.setdefault("centers", [])
                boxes_hist = best_track.setdefault("boxes", [])
                new_center = _center_of(box) if isinstance(box, dict) else None
                new_box = _box_tuple(box) if isinstance(box, dict) else None
                # An extent flip (same object, other granularity) moves the box
                # centre without the object moving; learning it as velocity
                # would throw the next cycle's prediction off by the same jump.
                extent_flip = (
                    previous_box is not None and new_box is not None
                    and _extent_unstable(previous_box, new_box)
                )
                if new_center is not None:
                    if centers and not extent_flip:
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
                detection["track_displacement"] = _anchored_displacement(
                    best_track,
                    cycle_boxes[detection_index],
                    [b for i, b in enumerate(cycle_boxes) if b is not None and i != detection_index],
                )
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
            if track["id"] not in matched_track_ids and track["label"] not in hold_labels:
                track["misses"] += 1
            if track["misses"] <= max_age:
                survivors.append(track)
            else:
                _retire(state, track, now)
        state["tracks"] = survivors

    return detections


def live_track_ids(camera_id: str, label: str) -> set[Any]:
    """Ids of the tracks with ``label`` the tracker still holds for a camera.

    A track survives a few missed cycles, so this is the set a per-track cache
    should be pruned to -- not just the tracks seen this very cycle.
    """
    key = str(label or "").strip().lower()
    with _state._object_tracks_lock:
        state = _state._object_tracks.get(camera_id) or {}
        return {track["id"] for track in state.get("tracks", []) if track.get("label") == key}


def recent_track_boxes(camera_id: str, label: str, *, max_misses: int = 2) -> list[dict[str, Any]]:
    """Boxes of the ``label`` tracks seen within the last ``max_misses`` cycles.

    For gates that need "was there one here a moment ago" -- a detector that
    flickers for a cycle should not make everything that depends on it vanish.
    """
    key = str(label or "").strip().lower()
    with _state._object_tracks_lock:
        state = _state._object_tracks.get(camera_id) or {}
        return [
            dict(track["box"])
            for track in state.get("tracks", [])
            if track.get("label") == key and track.get("misses", 0) <= max_misses and isinstance(track.get("box"), dict)
        ]
