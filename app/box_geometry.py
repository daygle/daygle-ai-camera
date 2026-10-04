"""Normalized box geometry shared by the tracker and the confirmation gate.

The one concept here is **extent instability**: the detector drawing the same
object at wildly different sizes within a short span. Observed on real footage
(event 47720, Driveway): one cycle boxed a parked car's whole body
(0.506 x 0.429), the next only its roof (0.319 x 0.111) -- 2.4x apart in aspect
ratio, 6.1x in area, centres 0.178 of the frame apart -- and the car never
moved.

Anything that compares two boxes and concludes "this object moved" or "this
object persisted" is fooled by that, because both the box centre and the box
size change dramatically while nothing in the scene does. The signature is a
large SHAPE change combined with one box sitting inside the other. Neither of
the two cases that legitimately move boxes has both: a genuinely translating
object separates its boxes (low containment), and a subject approaching the
camera scales its box uniformly (stable aspect).

This lives in its own module rather than in ``app.object_tracking`` so the
detection-confirmation gate can use it without depending on the tracker (see
the note on ``_box_iou`` in ``app.detection_state``) and without a second copy
of the logic drifting out of step.
"""
from __future__ import annotations

from typing import Any

# How far apart two boxes' aspect ratios may be before the pair counts as a
# SHAPE disagreement. The real case is 2.4x; a subject approaching the camera
# changes aspect by ~1.0x.
ASPECT_DRIFT = 1.7

# How much of the smaller box the larger must cover for the pair to count as
# the same place. A roof-only box inside a whole-vehicle box is 1.0; two
# objects that translate apart are near 0.0.
EXTENT_CONTAINMENT = 0.8


def box_tuple(box: Any) -> tuple[float, float, float, float] | None:
    """Normalized ``(x, y, w, h)`` for a ``{x,y,width,height}`` box, or None.

    Returns None for anything unusable rather than raising, so a caller holding
    a partially-populated detection dict degrades to "no evidence" instead of
    failing the cycle.
    """
    if not isinstance(box, dict):
        return None
    try:
        x = float(box.get('x') or 0.0)
        y = float(box.get('y') or 0.0)
        w = float(box.get('width') or 0.0)
        h = float(box.get('height') or 0.0)
    except (TypeError, ValueError):
        return None
    return (x, y, w, h)


def extent_unstable(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
) -> bool:
    """True when two boxes disagree about SHAPE but agree about WHERE.

    See the module docstring: that combination means the detector drew one
    object at two granularities, not that anything moved. Used to suppress a
    spurious motion reading and to let the pair satisfy a spatial-persistence
    test that raw IoU would fail.
    """
    try:
        ax, ay, aw, ah = (float(value) for value in box_a)
        bx, by, bw, bh = (float(value) for value in box_b)
    except (TypeError, ValueError):
        return False
    aw, ah, bw, bh = max(aw, 1e-9), max(ah, 1e-9), max(bw, 1e-9), max(bh, 1e-9)
    aspect_a, aspect_b = aw / ah, bw / bh
    if max(aspect_a, aspect_b) / min(aspect_a, aspect_b) <= ASPECT_DRIFT:
        return False
    overlap_x = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_y = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    smaller = min(aw * ah, bw * bh)
    return smaller > 0.0 and (overlap_x * overlap_y) / smaller >= EXTENT_CONTAINMENT
