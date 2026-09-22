"""Behavioural intelligence primitives (Tier 1).

Pure, dependency-free geometry for **directional line-crossing** ("tripwire")
detection, layered on top of the object tracker's per-track box centres
(``app/object_tracking.py``). Keeping this module free of heavy dependencies
means it is cheap on the per-frame hot path and trivially unit-testable.

A tripwire is a directed segment **A -> B** in normalized (0..1) frame
coordinates. As a tracked object moves from one detection cycle's centre
(``prev``) to the next (``curr``), it "crosses" the tripwire when the segment
``prev -> curr`` intersects the segment ``A -> B``. The crossing carries a
direction relative to the tripwire's orientation:

- ``"forward"``  -- the object moved from the LEFT side of ``A -> B`` to its
  RIGHT side (i.e. in the direction of the line's right-hand normal),
- ``"backward"`` -- the reverse (RIGHT side to LEFT side).

"Left" / "right" are defined by the 2-D cross product ``(B - A) x (P - A)``:
positive is left of the directed line, negative is right. The UI labels which
direction is meaningful for a given camera (e.g. "entering the driveway");
the geometry here stays neutral.
"""
from __future__ import annotations

from typing import Any

# A point is an (x, y) pair in normalized 0..1 frame coordinates.
Point = tuple[float, float]

FORWARD = 'forward'
BACKWARD = 'backward'
BOTH = 'both'


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    """2-D cross product of vectors (ax, ay) and (bx, by)."""
    return ax * by - ay * bx


def _orientation(a: Point, b: Point, c: Point) -> float:
    """Signed area * 2 of triangle a-b-c.

    > 0 : c is left of the directed line a -> b
    < 0 : c is right of it
    == 0: a, b, c are collinear
    """
    return _cross(b[0] - a[0], b[1] - a[1], c[0] - a[0], c[1] - a[1])


def _on_segment(a: Point, b: Point, c: Point) -> bool:
    """True when point ``c`` (assumed collinear with a-b) lies within the
    axis-aligned bounding box of segment a-b -- i.e. actually on the segment."""
    return (
        min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
    )


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Return True when segment ``p1-p2`` intersects segment ``q1-q2``.

    Handles the general straddling case plus collinear "touch" cases so a
    track whose step lands exactly on the tripwire still registers.
    """
    d1 = _orientation(q1, q2, p1)
    d2 = _orientation(q1, q2, p2)
    d3 = _orientation(p1, p2, q1)
    d4 = _orientation(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    if d1 == 0 and _on_segment(q1, q2, p1):
        return True
    if d2 == 0 and _on_segment(q1, q2, p2):
        return True
    if d3 == 0 and _on_segment(p1, p2, q1):
        return True
    if d4 == 0 and _on_segment(p1, p2, q2):
        return True
    return False


def line_crossing(prev: Point | None, curr: Point | None, a: Point, b: Point) -> str | None:
    """Classify a track's ``prev -> curr`` step against the tripwire ``a -> b``.

    Returns :data:`FORWARD`, :data:`BACKWARD`, or ``None`` (no crossing). A
    step that only grazes the line (starts or ends exactly on it, no clean
    side change) returns ``None`` so a subject loitering on the line does not
    emit a burst of crossings.
    """
    if prev is None or curr is None:
        return None
    if not segments_intersect(prev, curr, a, b):
        return None
    side_prev = _orientation(a, b, prev)
    side_curr = _orientation(a, b, curr)
    if side_prev > 0 and side_curr < 0:
        return FORWARD
    if side_prev < 0 and side_curr > 0:
        return BACKWARD
    return None


def crossing_matches(crossing: str | None, configured_direction: str) -> bool:
    """True when a detected ``crossing`` should fire under the tripwire's
    configured ``direction`` (``forward`` / ``backward`` / ``both``)."""
    if crossing is None:
        return False
    if configured_direction == BOTH:
        return True
    return crossing == configured_direction


def _point_of(value: Any) -> Point | None:
    """Coerce ``{'x': .., 'y': ..}`` or ``(x, y)`` into a ``Point``."""
    if isinstance(value, dict):
        try:
            return (float(value['x']), float(value['y']))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def tripwire_crossing(prev: Any, curr: Any, tripwire: Any) -> str | None:
    """Convenience wrapper: read ``a``/``b`` off a normalized ``tripwire``
    dict (as produced by ``zone_schema.normalize_zone_tripwire``) and return
    the crossing direction that actually matches its configured ``direction``,
    else ``None``. A disabled or malformed tripwire never fires.
    """
    if not isinstance(tripwire, dict) or tripwire.get('enabled') is False:
        return None
    a = _point_of(tripwire.get('a'))
    b = _point_of(tripwire.get('b'))
    p = _point_of(prev)
    c = _point_of(curr)
    if a is None or b is None or p is None or c is None:
        return None
    crossing = line_crossing(p, c, a, b)
    direction = str(tripwire.get('direction') or BOTH).strip().lower()
    return crossing if crossing_matches(crossing, direction) else None
