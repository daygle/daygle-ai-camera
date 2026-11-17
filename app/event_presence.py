"""Presence-based event gate for the live pipeline.

Decides when a detection is a NEW event without cooldown timers:

* An object raises one event when it appears. While it stays in view, it raises
  no further events or alerts.
* A second object of the same type raises its own event when it is in view
  together with the first one (the count of that type seen at once rises).
* An object of a different type raises its own event straight away.

Each ``key`` (a label such as ``cat``, or a face key such as ``person:<id>``)
keeps the most objects of that key seen together (``high``). A cycle that sees
more than ``high`` fires and raises ``high``. Counting replaces tracker ids
here on purpose: a cat that slips behind a bush comes back under a new track
id, but there is still only one cat in view, so it does not alert again.

A key is forgotten once it has been unseen for the absence window (the
recording's extension step, i.e. how long a clip keeps going after its last
sighting). That holds even while the clip that first saw the object is still
capturing: an animal that leaves and comes back to the same footage is a new
sighting, so it raises a new event (linked to the ongoing clip) and its alert
rule -- including AI verification -- runs again. A subject that stays in view
never expires, because every cycle it is seen refreshes ``last_seen``.

Rising above one object of a key must hold for
``SECOND_OBJECT_CONFIRM_CYCLES`` consecutive sightings, so a detector drawing
two boxes on one cat for a single frame does not alert a "second cat". The
first object fires on first sight (the detector's own confirmation, if
enabled, already applied).

An object whose alert AI verification rejected gets one re-check while it stays
on record (``reopen_rejected``): the first frame of a person at the edge of the view can
be too poor for the model, and without a re-check the rejected verdict would
cover everything of that type for the rest of the clip.
"""
from __future__ import annotations

import time
from typing import Any

import app.state as _state

SECOND_OBJECT_CONFIRM_CYCLES = 2
# How recently an object on record must have been seen for an overlapping
# sighting under another label of its group to count as that same object (see
# ``presence_keys``). A few detection cycles: the label flips cycle to cycle.
GROUP_MATE_WINDOW_SECONDS = 3.0
# Floor for the absence window, so a 0s extension step does not forget an
# object between two detection passes.
MIN_ABSENCE_SECONDS = 5.0
DEFAULT_ABSENCE_SECONDS = 45.0


def absence_seconds(recording_config: dict[str, Any] | None) -> float:
    """How long a key may go unseen before it can raise a new event."""
    config = recording_config or {}
    raw = config.get('extension_step_seconds', config.get('post_event_seconds', DEFAULT_ABSENCE_SECONDS))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_ABSENCE_SECONDS
    return max(MIN_ABSENCE_SECONDS, value)


def _expire(scope: dict[str, dict[str, Any]], now: float, absence: float) -> None:
    """Forget every key unseen for longer than ``absence``.

    The clip a key was seen in does not keep it alive: a return to the same
    footage is a new sighting (see the module docstring), so its alert and AI
    verification run again. ``observe_presence`` still records the recording id
    on each entry because ``reopen_rejected`` compares it.
    """
    for key in [k for k, entry in scope.items()
                if now - float(entry.get('last_seen') or 0) > absence]:
        del scope[key]


def presence_keys(
    camera_id: str,
    detections: list[dict[str, Any]],
    *,
    now: float | None = None,
) -> list[str]:
    """The presence key each detection counts under.

    Normally its own label. At night an IR-lit cat can read ``cat``, then
    ``dog``, then ``bird`` on successive cycles; counted by label, each flip
    was a new type of object, so one cat behind an ``animal`` rule raised three
    events (and three notifications) in as many cycles. A label not on record
    whose box overlaps where a label-group mate on record was just seen is that
    mate, the same evidence the confirmation gate uses for such sightings. An
    animal away from it, or a label in no group with the one on record, still
    counts as its own.
    """
    from app.box_geometry import box_tuple, extent_unstable
    from app.detection_state import GROUP_MATE_MIN_IOU, _box_iou, _group_mates

    now = time.time() if now is None else now
    with _state.live_event_presence_lock:
        scope = dict(_state.live_event_presence.get(camera_id) or {})
    keys: list[str] = []
    for detection in detections:
        label = str(detection.get('label') or '').strip().lower()
        box = detection.get('box')
        key = label
        if label and label not in scope and isinstance(box, dict):
            current = box_tuple(box)
            for mate in sorted(_group_mates(label)):
                entry = scope.get(mate)
                if not entry or now - float(entry.get('boxes_seen') or float('-inf')) > GROUP_MATE_WINDOW_SECONDS:
                    continue
                if any(
                    _box_iou(box, seen) >= GROUP_MATE_MIN_IOU
                    or (current is not None and (previous := box_tuple(seen)) is not None
                        and extent_unstable(current, previous))
                    for seen in entry.get('boxes') or ()
                ):
                    key = mate
                    break
        keys.append(key)
    return keys


def observe_presence(
    camera_id: str,
    counts: dict[str, int],
    *,
    absence: float,
    recording_id: int | None = None,
    now: float | None = None,
    boxes: dict[str, list[dict[str, Any]]] | None = None,
) -> set[str]:
    """Record this cycle's per-key object counts; return the keys that are new.

    ``recording_id`` is the camera's capturing clip, if any. It is stored on
    each entry (``reopen_rejected`` compares it) but does not extend coverage: a
    key unseen past ``absence`` is forgotten even mid-clip, so its return opens
    a new event in the same footage.

    ``boxes`` (per key, this cycle's boxes) are kept with each entry so
    ``presence_keys`` can tell a relabelled sighting of the same object.
    """
    now = time.time() if now is None else now
    new: set[str] = set()
    with _state.live_event_presence_lock:
        scope = _state.live_event_presence.setdefault(camera_id, {})
        _expire(scope, now, absence)
        for key, count in counts.items():
            if not key or count <= 0:
                continue
            entry = scope.setdefault(key, {'high': 0, 'pending_cycles': 0})
            entry['last_seen'] = now
            if boxes and boxes.get(key):
                entry['boxes'] = list(boxes[key])
                entry['boxes_seen'] = now
            if recording_id is not None:
                entry['recording_id'] = recording_id
            if count <= entry['high']:
                entry['pending_cycles'] = 0
                continue
            entry['pending_cycles'] += 1
            required = 1 if entry['high'] == 0 else SECOND_OBJECT_CONFIRM_CYCLES
            if entry['pending_cycles'] >= required:
                entry['high'] = count
                entry['pending_cycles'] = 0
                new.add(key)
    return new


def reopen_rejected(camera_id: str, labels: set[str], *, recording_id: int | None = None) -> set[str]:
    """Let keys whose alert AI verification rejected raise one more event.

    The next sighting of such a key is new again, so it opens its own event and
    is verified on a fresh frame. Each key is reopened once while it stays on
    record: a second rejection stands, so a bush the detector keeps calling a
    person costs one extra check, not one per cycle. ``recording_id`` is the
    rejected event's clip; a key now tied to another clip is a later sighting
    with its own event and is left alone. Returns the keys reopened.
    """
    reopened: set[str] = set()
    with _state.live_event_presence_lock:
        scope = _state.live_event_presence.get(camera_id) or {}
        for key in labels:
            entry = scope.get(key)
            if entry is None or entry.get('reopened'):
                continue
            if recording_id is not None and entry.get('recording_id') not in (None, recording_id):
                continue
            entry['high'] = 0
            entry['pending_cycles'] = 0
            entry['reopened'] = True
            reopened.add(key)
    return reopened


def claim_presence(camera_id: str, key: str, *, recording_id: int | None = None,
                   now: float | None = None) -> bool:
    """Put ``key`` on record for an object found outside the detector.

    AI verification can relabel a rejected box (the "person" was a cat). The
    cat then alerts once, like any first sighting: True when ``key`` was not
    on record yet (claimed now), False when it already was. Its later
    sightings are covered and it is forgotten as usual (see observe_presence).
    """
    now = time.time() if now is None else now
    with _state.live_event_presence_lock:
        scope = _state.live_event_presence.setdefault(camera_id, {})
        entry = scope.setdefault(key, {'high': 0, 'pending_cycles': 0})
        if entry['high'] > 0:
            return False
        entry.update(high=1, pending_cycles=0, last_seen=now)
        if recording_id is not None:
            entry['recording_id'] = recording_id
        return True


def drop_covered_motion(camera_id: str, new: set[str]) -> set[str]:
    """Motion around something already on record (an object, a face) is that
    subject moving, not a new event: drop ``motion`` from ``new`` then."""
    if 'motion' not in new:
        return new
    with _state.live_event_presence_lock:
        others = set(_state.live_event_presence.get(camera_id) or ()) - {'motion'}
    return new - {'motion'} if others else new


def bind_presence_recording(camera_id: str, recording_id: int, *, seen_since: float) -> None:
    """Tie what this cycle saw to the recording its event just opened or joined.

    The first sighting is observed before its event creates the clip, so it
    would otherwise carry no recording and could be forgotten mid-clip.
    """
    with _state.live_event_presence_lock:
        for entry in (_state.live_event_presence.get(camera_id) or {}).values():
            if float(entry.get('last_seen') or 0) >= seen_since:
                entry['recording_id'] = recording_id
