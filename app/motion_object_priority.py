"""Object priority over motion ACROSS cycles, not only within one.

Within a single detection cycle, ``filter_motion_detections_by_objects`` already
drops a motion box that a concrete object box explains. That does not cover the
common night case: a passing car's headlights sweep the zone a second or more
before the car body is lit and recognisable, so the motion-zone rule confirms
and opens a ``motion`` event (with its alert) first, and the car then opens a
second event. Tail-light glow, or a beam spreading wider than the car's box,
does the same on the way out.

``MotionObjectArbiter`` resolves that per camera and per motion zone:

* Motion in a zone where an alertable object was seen within
  ``grace_seconds`` is attributed to that object and dropped, whichever came
  first (trailing glow after the car, or motion alongside it).
* Motion-only in a zone is HELD for ``grace_seconds``. If an object appears in
  that zone meanwhile, the held motion is absorbed into the object's event and
  never fires on its own. If not, it is released: stamped with the time the
  motion began and carrying that moment's frame, so the motion event and its
  recording's pre-roll start where the motion did.

``grace_seconds <= 0`` disables the arbiter: motion fires exactly as before.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_GRACE_SECONDS = 3.0
MAX_GRACE_SECONDS = 10.0


def _zone_key(detection: dict[str, Any]) -> str:
    return str(detection.get('zone_id') or detection.get('zone_name') or '').strip()


@dataclass
class _Held:
    first_ts: float
    detection: dict[str, Any]
    image: Any
    image_is_numpy: bool


@dataclass
class ArbiterResult:
    """What the cycle should treat as its motion detections.

    ``event_ts``/``image``/``image_is_numpy`` are set only when released held
    motion should be stamped with (and pictured at) the moment it began; they
    are None when the cycle's own time and frame apply.
    """

    motion_detections: list[dict[str, Any]]
    held_zones: list[str] = field(default_factory=list)
    attributed_zones: list[str] = field(default_factory=list)
    event_ts: float | None = None
    image: Any = None
    image_is_numpy: bool = False


class MotionObjectArbiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # camera_id -> zone key -> capture ts of the last alertable object in it
        self._object_seen: dict[str, dict[str, float]] = {}
        # camera_id -> zone key -> held motion
        self._held: dict[str, dict[str, _Held]] = {}

    def has_held(self, camera_id: str) -> bool:
        """True while motion is held for a camera (its cycle must still run
        so the hold can be released or absorbed)."""
        with self._lock:
            return bool(self._held.get(camera_id))

    def clear_camera(self, camera_id: str) -> None:
        with self._lock:
            self._object_seen.pop(camera_id, None)
            self._held.pop(camera_id, None)

    def resolve(
        self,
        camera_id: str,
        *,
        now: float,
        grace_seconds: float,
        motion_detections: list[dict[str, Any]],
        object_zone_keys: set[str],
        image: Any,
        image_is_numpy: bool,
        has_objects: bool,
    ) -> ArbiterResult:
        """Decide this cycle's motion detections.

        ``object_zone_keys`` are the motion-zone keys containing at least one
        alertable object this cycle. ``has_objects`` says whether the cycle has
        any alertable object at all (its event then takes the cycle's own time
        and frame, and released motion rides along with it).
        """
        if grace_seconds <= 0:
            with self._lock:
                self._held.pop(camera_id, None)
            return ArbiterResult(motion_detections=list(motion_detections))
        with self._lock:
            seen = self._object_seen.setdefault(camera_id, {})
            held = self._held.setdefault(camera_id, {})
            for key in object_zone_keys:
                seen[key] = now

            def _object_near(key: str) -> bool:
                last = seen.get(key)
                return last is not None and abs(now - last) <= grace_seconds

            result = ArbiterResult(motion_detections=[])
            # Held motion that an object showed up for WITHIN its hold is
            # absorbed. A sighting after the hold expired (a gap between
            # cycles) does not reach back: that motion is released below.
            for key in [
                k for k, item in held.items()
                if item.first_ts <= seen.get(k, float('-inf')) <= item.first_ts + grace_seconds
            ]:
                held.pop(key, None)
            current: dict[str, dict[str, Any]] = {}
            for detection in motion_detections:
                key = _zone_key(detection)
                if not key:
                    # Unattributable motion cannot be arbitrated; keep legacy.
                    result.motion_detections.append(detection)
                    continue
                if _object_near(key):
                    result.attributed_zones.append(key)
                    continue
                current[key] = detection
                if key not in held:
                    held[key] = _Held(now, detection, image, image_is_numpy)
            released: list[_Held] = []
            for key, item in list(held.items()):
                if now - item.first_ts >= grace_seconds:
                    # Prefer this cycle's reading of the zone when it is still
                    # moving; otherwise the one captured when it began.
                    released.append(_Held(item.first_ts, current.get(key, item.detection), item.image, item.image_is_numpy))
                    held.pop(key, None)
                else:
                    result.held_zones.append(key)
            # Forget stale object sightings so the dict cannot grow unbounded.
            for key in [k for k, ts in seen.items() if now - ts > grace_seconds * 4]:
                seen.pop(key, None)
        if released:
            result.motion_detections.extend(item.detection for item in released)
            if not has_objects:
                earliest = min(released, key=lambda item: item.first_ts)
                result.event_ts = earliest.first_ts
                result.image = earliest.image
                result.image_is_numpy = earliest.image_is_numpy
        return result


def object_zone_keys(
    zones: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    matches: Callable[[dict[str, Any], dict[str, Any]], bool],
) -> set[str]:
    """Keys of motion zones that contain at least one of ``objects``."""
    keys: set[str] = set()
    for zone in zones:
        key = str(zone.get('id') or zone.get('name') or '').strip()
        if key and any(matches(obj, zone) for obj in objects):
            keys.add(key)
    return keys
