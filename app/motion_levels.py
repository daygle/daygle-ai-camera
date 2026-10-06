"""Per-zone motion levels for the live page and the zone editor.

Every motion check scores each motion zone as the share of its own pixels that
changed. The trigger the operator sets ("trigger when 2% of this zone moves")
is in those same units, so publishing the per-zone share next to it lets the
UI show a meter that can be read directly against the setting:

    Driveway: 0.4% moving - triggers at 2%

Each zone also carries a state (``quiet`` / ``moving`` / ``triggered``) and the
highest share seen over the last ten minutes, which is the number an operator
needs when picking a trigger: just above the peak of a quiet scene.
"""

from __future__ import annotations

import threading
import time
from typing import Any

# Below this share a zone reads "Quiet": sensor noise and compression shimmer
# routinely leave a few stray pixels set on a still scene.
QUIET_FRACTION = 0.001
PEAK_WINDOW_SECONDS = 600
# Peaks are kept as one maximum per bucket, so the memory per zone is bounded
# by the window, not the check rate.
_PEAK_BUCKET_SECONDS = 30

_lock = threading.Lock()
# camera_id -> zone_id -> {bucket_start: max_fraction}
_peaks: dict[str, dict[str, dict[int, float]]] = {}


def _note_peak(camera_id: str, zone_id: str, fraction: float, now: float) -> float:
    """Record ``fraction`` and return the zone's peak over the window."""
    bucket = int(now // _PEAK_BUCKET_SECONDS) * _PEAK_BUCKET_SECONDS
    oldest = now - PEAK_WINDOW_SECONDS
    with _lock:
        buckets = _peaks.setdefault(camera_id, {}).setdefault(zone_id, {})
        if fraction > buckets.get(bucket, -1.0):
            buckets[bucket] = fraction
        for start in [start for start in buckets if start + _PEAK_BUCKET_SECONDS <= oldest]:
            del buckets[start]
        return max(buckets.values(), default=0.0)


def forget_camera(camera_id: str) -> None:
    with _lock:
        _peaks.pop(camera_id, None)


def motion_zone_levels(
    camera_id: str,
    levels: list[dict[str, Any]],
    *,
    triggered_zone_ids: set[str],
    streaks: dict[str, int] | None = None,
    confirm_cycles: dict[str, int] | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Turn ``zone_motion_detections`` levels into the published per-zone list.

    ``triggered_zone_ids`` are the zones that passed confirmation this check
    (and so can raise an event); ``streaks`` are the confirmation counters, so
    a zone above its trigger but still waiting for its next check reads as
    moving with ``checks_seen`` < ``checks_needed``.
    """
    now = time.time() if now is None else now
    known = set()
    result: list[dict[str, Any]] = []
    for level in levels:
        zone_id = str(level.get('zone_id') or '')
        if not zone_id:
            continue
        known.add(zone_id)
        fraction = level.get('fraction')
        trigger = float(level.get('trigger') or 0.0)
        needed = int((confirm_cycles or {}).get(zone_id, 2))
        if fraction is None:
            peak = _note_peak(camera_id, zone_id, 0.0, now)
            state = 'triggered' if zone_id in triggered_zone_ids else 'quiet'
        else:
            fraction = float(fraction)
            peak = _note_peak(camera_id, zone_id, fraction, now)
            if zone_id in triggered_zone_ids:
                state = 'triggered'
            elif fraction >= QUIET_FRACTION:
                state = 'moving'
            else:
                state = 'quiet'
        result.append({
            'zone_id': zone_id,
            'zone_name': level.get('zone_name') or zone_id,
            'fraction': None if fraction is None else round(fraction, 6),
            'trigger': round(trigger, 6),
            'above_trigger': fraction is not None and fraction >= trigger > 0,
            'state': state,
            'peak_fraction': round(peak, 6),
            'peak_window_seconds': PEAK_WINDOW_SECONDS,
            'checks_seen': min(needed, int((streaks or {}).get(zone_id, 0))),
            'checks_needed': needed,
        })
    # Drop peaks for zones that were deleted or disabled.
    with _lock:
        zones = _peaks.get(camera_id)
        if zones:
            for zone_id in [zone_id for zone_id in zones if zone_id not in known]:
                del zones[zone_id]
    return result
