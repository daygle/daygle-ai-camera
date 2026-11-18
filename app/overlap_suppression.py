"""Drop a person box the detector drew on an animal it also boxed.

A small, low-contrast animal (a grey cat on an IR-lit driveway) can come back
from the detector twice in one frame: as ``cat`` at 85% and as ``person`` at
68%, two boxes on the same spot. The person box then raises a person alert
for a cat. When a person box overlaps an animal box of higher confidence by
at least ``PERSON_ANIMAL_IOU`` (intersection over union), it is the same
object and the person box is dropped.

IoU rather than containment keeps a person holding or walking an animal: the
animal's box is a small part of the person's, so their IoU is low.
"""
from __future__ import annotations

from typing import Any

from app.label_groups import DEFAULT_LABEL_GROUPS
from app.object_tracking import _iou
from app.zone_schema import canonical_label

PERSON_ANIMAL_IOU = 0.5
# The built-in animal labels, not the editable ``animal`` group: renaming or
# reshaping that group on the Objects page must not change what counts as an
# animal here.
ANIMAL_LABELS = DEFAULT_LABEL_GROUPS['animal']


def _confidence(detection: dict[str, Any]) -> float:
    try:
        return float(detection.get('confidence') or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _box(detection: dict[str, Any]) -> dict[str, Any]:
    # Live detections carry ``box``; stored event detections are flat.
    box = detection.get('box')
    return box if isinstance(box, dict) else detection


def covering_animal(person: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The strongest animal box that is the same object as ``person``, if any."""
    if canonical_label(person.get('label')) != 'person':
        return None
    confidence = _confidence(person)
    best = None
    for other in evidence:
        if canonical_label(other.get('label')) not in ANIMAL_LABELS or _confidence(other) <= confidence:
            continue
        try:
            overlap = _iou(_box(person), _box(other))
        except (TypeError, ValueError):
            continue
        if overlap >= PERSON_ANIMAL_IOU and (best is None or _confidence(other) > _confidence(best)):
            best = other
    return best


def drop_people_boxed_as_animals(
    detections: list[dict[str, Any]], evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """``detections`` without the person boxes an animal in ``evidence`` explains.

    ``evidence`` is every box the detector found this cycle, so an animal the
    camera does not watch still explains a person box drawn on it.
    """
    return [detection for detection in detections if covering_animal(detection, evidence) is None]


def overlap_reason(person: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    animal = covering_animal(person, evidence)
    if animal is None:
        return 'Same object as an animal box'
    return f"Same object as {canonical_label(animal.get('label'))} ({_confidence(animal):.0%})"


# The reverse: an animal box drawn on a person's head. Seen from above (a
# camera over a doorway), a head of hair reads as a cat or dog - the reported
# frame boxed a woman's head "Cat 76%" inside her own person box. Such a box
# sits almost wholly inside a confident person box, is a small share of it,
# and its top is at the top of the person: the head. A cat carried in the
# arms sits mid-body and keeps its box, as does any animal beside a person.
HEAD_CONTAINMENT = 0.8      # animal box area inside the person box
HEAD_AREA_RATIO = 0.35      # animal area / person area, at most
HEAD_TOP_SHARE = 0.15       # animal top within this share of the person's height from its top
HEAD_CENTRE_SHARE = 0.35    # animal centre within this share of the person's height from its top
HEAD_PERSON_CONFIDENCE = 0.5


def _xywh(box: dict[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        x, y = float(box.get('x') or 0.0), float(box.get('y') or 0.0)
        w, h = float(box.get('width') or 0.0), float(box.get('height') or 0.0)
    except (TypeError, ValueError, AttributeError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def head_of_person(animal: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The confident person whose head ``animal`` is, if any."""
    if canonical_label(animal.get('label')) not in ANIMAL_LABELS:
        return None
    a = _xywh(_box(animal))
    if a is None:
        return None
    ax, ay, aw, ah = a
    for person in evidence:
        if canonical_label(person.get('label')) != 'person' or _confidence(person) < HEAD_PERSON_CONFIDENCE:
            continue
        p = _xywh(_box(person))
        if p is None:
            continue
        px, py, pw, ph = p
        inter = (max(0.0, min(ax + aw, px + pw) - max(ax, px))
                 * max(0.0, min(ay + ah, py + ph) - max(ay, py)))
        if (
            inter >= HEAD_CONTAINMENT * aw * ah
            and aw * ah <= HEAD_AREA_RATIO * pw * ph
            and ay - py <= HEAD_TOP_SHARE * ph
            and ay + ah / 2.0 - py <= HEAD_CENTRE_SHARE * ph
        ):
            return person
    return None


def drop_animals_on_heads(detections: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``detections`` without the animal boxes drawn on a person's head."""
    return [detection for detection in detections if head_of_person(detection, evidence) is None]


def head_reason(animal: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    person = head_of_person(animal, evidence)
    return f"A person's head ({_confidence(person):.0%} person)" if person else "A person's head"
