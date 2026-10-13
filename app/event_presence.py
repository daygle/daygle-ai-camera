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
sighting) AND the recording it was seen in is no longer capturing. So within
one recording a returning object stays covered; after the clip has ended,
it raises a new event (and a new recording). Cameras that do not record use
the same absence window on its own.

Rising above one object of a key must hold for
``SECOND_OBJECT_CONFIRM_CYCLES`` consecutive sightings, so a detector drawing
two boxes on one cat for a single frame does not alert a "second cat". The
first object fires on first sight (the detector's own confirmation, if
enabled, already applied).
"""
from __future__ import annotations

import time
from typing import Any

import app.state as _state

SECOND_OBJECT_CONFIRM_CYCLES = 2
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


def _expire(scope: dict[str, dict[str, Any]], now: float, absence: float, recording_id: int | None) -> None:
    for key in [k for k, entry in scope.items()
                if now - float(entry.get('last_seen') or 0) > absence
                and (recording_id is None or entry.get('recording_id') != recording_id)]:
        del scope[key]


def observe_presence(
    camera_id: str,
    counts: dict[str, int],
    *,
    absence: float,
    recording_id: int | None = None,
    now: float | None = None,
) -> set[str]:
    """Record this cycle's per-key object counts; return the keys that are new.

    ``recording_id`` is the camera's capturing clip, if any: a key seen while
    it captures stays covered for as long as that clip does.
    """
    now = time.time() if now is None else now
    new: set[str] = set()
    with _state.live_event_presence_lock:
        scope = _state.live_event_presence.setdefault(camera_id, {})
        _expire(scope, now, absence, recording_id)
        for key, count in counts.items():
            if not key or count <= 0:
                continue
            entry = scope.setdefault(key, {'high': 0, 'pending_cycles': 0})
            entry['last_seen'] = now
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
