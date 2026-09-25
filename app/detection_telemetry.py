"""Per-camera, per-cycle detection-pipeline telemetry (Item 10, P2 follow-up).

The live detection pipeline has many stages that can each silently drop a
candidate:

``detector`` -> still/moving filter -> camera/zone filter -> N-of-M
confirmation -> zone alert matching -> event.

Until now, "why did this camera miss the person?" could only be answered by
correlating log lines. This module records, for every detection cycle, WHICH
path the cycle took and HOW MANY candidates survived each stage, so a
regression (a floor that got too high, a confirmation window that never fills,
a zone that stopped matching) is measurable from a number instead of a hunch.

Design constraints:

* **Never on the critical path.** Recording is a single dict append under one
  lock. Nothing here raises: a telemetry failure must not break detection.
* **Bounded.** A rolling window of the most recent ``WINDOW_SIZE`` cycles per
  camera, so a long-running process cannot grow without limit.
* **Aggregates, not just samples.** The most useful question is "of the last N
  cycles, how many candidates were dropped and why?", so the store keeps
  running sums alongside the raw per-cycle records.

The record shape is intentionally flat and JSON-serializable: it is exposed
through ``/api/live/detection-telemetry`` for the same operational debugging
the motion gauge already serves.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger('daygle.ai')

# Rolling window of recent cycles retained per camera.
WINDOW_SIZE = 50

# Stable vocabulary for the inference mode of a cycle. Kept as explicit
# constants so a typo in a call site fails loudly at review time rather than
# producing an unqueryable telemetry value.
MODE_ALWAYS_ON = 'always_on'
MODE_MOTION_GATED = 'motion_gated'
MODE_SKIPPED_NO_MOTION = 'skipped_no_motion'
MODE_SKIPPED_NO_DETECTOR = 'skipped_no_detector'
MODE_ERROR = 'error'

# Rejection reasons a candidate can be dropped. Each maps to exactly one
# pipeline stage so an operator can attribute a miss without guessing.
REJECT_MOTION_MODE = 'motion_mode'
REJECT_CAMERA_SCOPE = 'camera_scope'
REJECT_ZONE = 'zone'
REJECT_CONFIRMATION = 'confirmation'
REJECT_CAMERA_MOTION = 'camera_motion'

_telemetry_lock = threading.Lock()
# camera_id -> {'cycles': [...], 'totals': {...}}
_telemetry: dict[str, dict[str, Any]] = {}


def _empty_totals() -> dict[str, Any]:
    return {
        'cycles': 0,
        'inference_cycles': 0,
        'camera_motion_cycles': 0,
        'candidates_detected': 0,
        'candidates_after_motion_mode': 0,
        'candidates_after_camera_filter': 0,
        'candidates_after_confirmation': 0,
        'candidates_alertable': 0,
        'events': 0,
        'rejected': {
            REJECT_MOTION_MODE: 0,
            REJECT_CAMERA_SCOPE: 0,
            REJECT_ZONE: 0,
            REJECT_CONFIRMATION: 0,
            REJECT_CAMERA_MOTION: 0,
        },
    }


def record_detection_cycle(
    camera_id: str,
    *,
    inference_mode: str,
    camera_motion: bool = False,
    camera_motion_reason: str | None = None,
    frame_motion: bool = False,
    candidates: dict[str, int] | None = None,
    rejected: dict[str, int] | None = None,
    event_created: bool = False,
) -> dict[str, Any]:
    """Record one detection cycle for ``camera_id`` and return the stored entry.

    ``candidates`` maps a pipeline stage name to the number of candidates that
    SURVIVED that stage. ``rejected`` maps a rejection reason to a count. Both
    are merged into the running totals so the read API can answer "why" without
    the caller replaying history.

    Returns the per-camera telemetry entry (also stored). Never raises: a
    detection cycle must not fail because bookkeeping did.
    """
    try:
        candidate_counts = {
            str(key): max(0, int(value))
            for key, value in (candidates or {}).items()
        }
        rejection_counts = {
            str(key): max(0, int(value))
            for key, value in (rejected or {}).items()
            if int(value) > 0
        }
        cycle = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'inference_mode': str(inference_mode),
            'frame_motion': bool(frame_motion),
            'camera_motion': bool(camera_motion),
            'camera_motion_reason': str(camera_motion_reason) if camera_motion_reason else None,
            'candidates': candidate_counts,
            'rejected': rejection_counts,
            'event_created': bool(event_created),
        }
        key = str(camera_id)
        with _telemetry_lock:
            entry = _telemetry.get(key)
            if entry is None:
                entry = {'cycles': [], 'totals': _empty_totals()}
                _telemetry[key] = entry
            cycles = entry['cycles']
            cycles.append(cycle)
            # Bound the raw history; older cycles are already folded into the
            # totals below, so dropping them loses no aggregate information.
            if len(cycles) > WINDOW_SIZE:
                del cycles[: len(cycles) - WINDOW_SIZE]
            totals = entry['totals']
            totals['cycles'] += 1
            if inference_mode in (MODE_ALWAYS_ON, MODE_MOTION_GATED):
                totals['inference_cycles'] += 1
            if camera_motion:
                totals['camera_motion_cycles'] += 1
            if event_created:
                totals['events'] += 1
            _accumulate(totals, 'candidates_detected', candidate_counts.get('detected'))
            _accumulate(
                totals, 'candidates_after_motion_mode',
                candidate_counts.get('after_motion_mode'),
            )
            _accumulate(
                totals, 'candidates_after_camera_filter',
                candidate_counts.get('after_camera_filter'),
            )
            _accumulate(
                totals, 'candidates_after_confirmation',
                candidate_counts.get('after_confirmation'),
            )
            _accumulate(totals, 'candidates_alertable', candidate_counts.get('alertable'))
            for reason, count in rejection_counts.items():
                totals['rejected'][reason] = totals['rejected'].get(reason, 0) + count
        return entry
    except Exception as exc:  # noqa: BLE001 - telemetry must never break detection
        logger.debug('Detection telemetry record failed for %s: %s', camera_id, exc)
        return {}


def _accumulate(totals: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    totals[key] = int(totals.get(key, 0)) + int(value)


def detection_telemetry_payload(camera_id: str | None = None) -> dict[str, Any]:
    """Return the telemetry payload for one camera, or for every camera.

    ``last_cycle`` is the most recent recorded cycle and ``totals`` the running
    sums for the whole process lifetime, so a caller can answer both "what just
    happened" and "how does this camera normally behave".
    """
    with _telemetry_lock:
        if camera_id is not None:
            entry = _telemetry.get(str(camera_id))
            if entry is None:
                return {
                    'camera_id': str(camera_id),
                    'cycles_recorded': 0,
                    'last_cycle': None,
                    'totals': _empty_totals(),
                }
            cycles = list(entry['cycles'])
            totals = {
                **entry['totals'],
                'rejected': dict(entry['totals']['rejected']),
            }
        else:
            merged = _empty_totals()
            all_last: dict[str, Any] = {}
            for cam_id, entry in _telemetry.items():
                cam_cycles = entry['cycles']
                if cam_cycles:
                    all_last[cam_id] = cam_cycles[-1]
                cam_totals = entry['totals']
                for key, value in cam_totals.items():
                    if key == 'rejected':
                        for reason, count in value.items():
                            merged['rejected'][reason] = merged['rejected'].get(reason, 0) + count
                    else:
                        merged[key] = int(merged.get(key, 0)) + int(value)
            return {
                'camera_id': None,
                'cameras': sorted(_telemetry.keys()),
                'last_cycle': None,
                'last_cycle_by_camera': all_last,
                'totals': merged,
            }
    return {
        'camera_id': str(camera_id),
        'cycles_recorded': len(cycles),
        'last_cycle': cycles[-1] if cycles else None,
        'totals': totals,
    }


def clear_detection_telemetry(camera_id: str | None = None) -> None:
    """Drop telemetry for one camera, or for all cameras (tests / camera delete)."""
    with _telemetry_lock:
        if camera_id is None:
            _telemetry.clear()
        else:
            _telemetry.pop(str(camera_id), None)


def prune_detection_telemetry(active_camera_ids: set[str] | None = None) -> None:
    """Drop telemetry for cameras that no longer exist.

    Called from the same pruning pass that clears per-camera motion state, so a
    removed camera cannot leave a permanent telemetry entry behind.
    """
    if active_camera_ids is None:
        return
    with _telemetry_lock:
        for stale in [key for key in _telemetry if key not in active_camera_ids]:
            _telemetry.pop(stale, None)
