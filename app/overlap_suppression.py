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
