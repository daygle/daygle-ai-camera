"""Keep a motion box on its subject after the zone's trigger fired.

A motion zone saves a box to the playback track only on cycles where its
moving share clears the trigger and its "Must last: N checks" is met. A
subject the motion mask only half sees (a plain white truck panel changes
little between frames; a car going behind a bush) can clear the trigger on one
cycle and fall just under it on the next, so the clip's track held a single
box and playback showed it for a moment, then nothing, while the truck kept
driving.

For ``FOLLOW_SECONDS`` after a zone's motion last fired, that zone's motion box
is also saved on cycles where the zone still moves at a fraction of its trigger
(``FOLLOW_MIN_TRIGGER_RATIO``). These follow boxes go to the playback track
only: they never create an event, alert or recording, and never renew the
window themselves, so swaying foliage cannot keep a zone following forever.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

FOLLOW_SECONDS = 10.0
FOLLOW_MIN_TRIGGER_RATIO = 0.25

_lock = threading.Lock()
# camera_id -> zone_id -> capture ts the zone's motion last fired
_last_fired: dict[str, dict[str, float]] = {}


def note_fired(camera_id: str, zone_ids: Iterable[str], now: float) -> None:
    """Record that these zones' motion fired (cleared trigger + confirmation)."""
    keys = [str(zone_id).strip() for zone_id in zone_ids if str(zone_id or '').strip()]
    if not keys:
        return
    with _lock:
        fired = _last_fired.setdefault(str(camera_id), {})
        for key in keys:
            fired[key] = now


def following_zones(camera_id: str, now: float, window: float = FOLLOW_SECONDS) -> set[str]:
    """Zones whose motion fired within ``window`` seconds of ``now``."""
    with _lock:
        fired = _last_fired.get(str(camera_id))
        if not fired:
            return set()
        for key in [k for k, ts in fired.items() if now - ts > window]:
            fired.pop(key, None)
        if not fired:
            _last_fired.pop(str(camera_id), None)
            return set()
        return {key for key, ts in fired.items() if now - ts >= 0}


def forget_camera(camera_id: str) -> None:
    with _lock:
        _last_fired.pop(str(camera_id), None)
