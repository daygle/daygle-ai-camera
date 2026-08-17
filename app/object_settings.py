"""Per-label still / moving object-detection filtering.

The ONNX detector (Layer 2) reports any object it can recognise, whether the
subject is walking down the drive or parked in it. This module lets an
operator say, per object label, whether detections should count for **moving**
subjects only, **still** subjects only, or both.

How "moving" is decided
-----------------------
Every detection cycle already computes the Layer-1 motion diff mask
(background subtraction, ~320x240 thumbnail). A detection whose bounding box
overlaps a meaningful fraction of changed pixels is classified ``moving``;
one that does not is ``still``. A subject that has stopped is absorbed into
the background model over ``1 / motion_background_alpha`` frames, so a parked
car naturally reads as *still*; a subject that keeps moving lands on fresh
pixels every frame and stays *moving*.

When no diff mask is available (periodic scan on a quiet frame, first frame
after a camera (re)connect, or a motion-gate error) the detection is
classified ``still``: no pixel change was measured, so treating it as a still
subject is the correct semantic. The one-frame transient on a brand-new
camera self-corrects on the next cycle.

Scope
-----
The filter applies to **object** detections only (Layer 2 / YOLO output).
Per-zone **motion** rules (Layer 3) are a separate pixel-diff axis and are
never filtered here, so a "car moving only" setting does not silence a
"motion in the driveway" zone rule.

Settings are stored in the database (setting key ``objects``) as::

    {
        "default_mode": "moving",               # moving | any | still
        "labels": {"person": "still"},         # optional per-label overrides
        "group_modes": {"animal": "moving"},   # optional per-group overrides
        "still_alerts": {"package": 10},       # label -> minutes (0/absent = off)
    }

A label without an override falls back to ``default_mode``; the default for
``default_mode`` is ``moving`` so a fresh install only counts moving subjects
(choose ``any`` to restore the historical moving-or-still behaviour).

Still-dwell alerts
------------------
``still_alerts`` adds a second, independent axis: alert when a label has been
detected **continuously still** for at least N minutes -- a package left in
view, a pet that has settled down, a car parked in the frame. The signal is the
same Layer-1 background-absorption classification the filter uses: once a
subject stops moving, its pixels stop changing and it reads as ``still`` every
cycle. The dwell tracker watches that classification across cycles; when the
streak reaches the threshold it emits a dwell alert (event + recording + in-app
alert), once per streak. If the subject moves again or leaves the frame, the
streak resets and a fresh streak can alert again later.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import app.state as _state
from app.label_groups import cached_label_groups
from app.zone_schema import canonical_label

logger = logging.getLogger('daygle.ai')

# Detection-behavior modes.
MODE_ANY = 'any'      # moving and still
MODE_MOVING = 'moving'  # only when the object's pixels are changing (default)
MODE_STILL = 'still'    # only when the object's pixels are not changing

VALID_MODES: frozenset[str] = frozenset({MODE_ANY, MODE_MOVING, MODE_STILL})

DEFAULT_OBJECT_SETTINGS: dict[str, Any] = {
    'default_mode': MODE_MOVING,
    'labels': {},
    'group_modes': {},
    'still_alerts': {},
}

# Still-dwell alert bounds (minutes). A value below the floor is treated as
# off; the cap keeps a silly 10,000-minute streak from being persisted.
_STILL_ALERT_MIN_MINUTES = 1
_STILL_ALERT_MAX_MINUTES = 1440

# A detection counts as "moving" when at least this fraction of its box's
# pixels are flagged as changed in the motion thumbnail. The box is tight
# around the subject, so a genuinely moving subject fills a large share of it;
# a still subject (absorbed into the background) has ~0. The absolute-pixel
# floor below keeps a lone noise pixel from flipping a huge box.
_MOVING_BOX_FRACTION = 0.01
_MOVING_MIN_CHANGED_PIXELS = 2


def normalize_mode(value: Any, default: str = MODE_ANY) -> str:
    """Coerce a raw mode to one of ``any`` / ``moving`` / ``still``."""
    text = str(value or '').strip().lower()
    return text if text in VALID_MODES else default


def _explicit_mode(value: Any) -> str | None:
    """A raw mode kept only when it is one of the valid modes, else ``None``.

    Unlike ``normalize_mode`` this does NOT coerce junk to a default: an
    empty/unparseable/unknown mode yields ``None`` so the caller can drop the
    entry entirely rather than persist a spurious default-valued override.
    """
    text = str(value or '').strip().lower()
    return text if text in VALID_MODES else None


def normalize_object_settings(value: Any) -> dict[str, Any]:
    """Validate + canonicalise a raw ``objects`` setting dict.

    Tolerates ``None`` (returns the defaults), a partial dict, legacy string
    modes, and junk labels; every returned mode is one of ``any`` /
    ``moving`` / ``still``.
    """
    if not isinstance(value, dict):
        return {
            'default_mode': MODE_MOVING,
            'labels': {},
            'group_modes': {},
            'still_alerts': {},
        }
    default_mode = normalize_mode(value.get('default_mode'), MODE_MOVING)
    labels: dict[str, str] = {}
    raw_labels = value.get('labels')
    if isinstance(raw_labels, dict):
        for raw_label, raw_mode in raw_labels.items():
            label = canonical_label(raw_label)
            mode = _explicit_mode(raw_mode)
            # Keep every explicit, valid override -- INCLUDING one that equals
            # the default. With group modes sitting between the per-label and
            # default layers, an explicit mode that matches the default is not
            # redundant: it overrides a covering group's non-default mode back
            # to that value (e.g. default=moving, animal=still, then an
            # explicit cat=moving to keep cats moving). Only invalid/empty
            # modes are dropped, so they never become a spurious override.
            if label and mode is not None:
                labels[label] = mode
    group_modes: dict[str, str] = {}
    raw_group_modes = value.get('group_modes')
    if isinstance(raw_group_modes, dict):
        for raw_group, raw_mode in raw_group_modes.items():
            group = canonical_label(raw_group)
            mode = _explicit_mode(raw_mode)
            # Keep explicit group modes even when equal to the default: a more
            # specific group overriding a broader one back to the default value
            # (animal=still + pet=moving) is the whole point of overlapping
            # groups, so collapsing pet=moving would defeat it.
            if group and mode is not None:
                group_modes[group] = mode
    still_alerts: dict[str, int] = {}
    raw_still = value.get('still_alerts')
    if isinstance(raw_still, dict):
        for raw_label, raw_minutes in raw_still.items():
            label = canonical_label(raw_label)
            if not label:
                continue
            minutes = _normalize_still_alert_minutes(raw_minutes)
            if minutes is not None:
                still_alerts[label] = minutes
    return {
        'default_mode': default_mode,
        'labels': labels,
        'group_modes': group_modes,
        'still_alerts': still_alerts,
    }


def _normalize_still_alert_minutes(value: Any) -> int | None:
    """Coerce a raw still-alert threshold to whole minutes, or ``None`` if off."""
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return None
    if not minutes or minutes < _STILL_ALERT_MIN_MINUTES:
        return None
    minutes = int(round(minutes))
    return max(_STILL_ALERT_MIN_MINUTES, min(_STILL_ALERT_MAX_MINUTES, minutes))


def effective_object_settings() -> dict[str, Any]:
    """The runtime ``objects`` settings (database override + defaults)."""
    raw = None
    db = _state.database
    if db is not None:
        try:
            raw = db.get_setting('objects')
        except Exception:  # pragma: no cover - defensive; DB may be mid-startup
            raw = None
    return normalize_object_settings(raw)


def motion_mode_for_label(
    label: Any,
    settings: dict[str, Any] | None = None,
) -> str:
    """Resolve the effective detection mode for one detection label.

    Precedence: an explicit per-label override wins; otherwise the most
    specific covering object group's mode (see ``_group_mode_for_label``);
    otherwise the global ``default_mode`` applies; anything unset resolves to
    ``moving`` (the default).
    """
    resolved = settings if settings is not None else effective_object_settings()
    default_mode = normalize_mode(resolved.get('default_mode'), MODE_MOVING)
    labels = resolved.get('labels')
    if isinstance(labels, dict):
        canonical = canonical_label(label)
        if canonical:
            if canonical in labels:
                return normalize_mode(labels[canonical], default_mode)
            group_mode = _group_mode_for_label(canonical, resolved, default_mode)
            if group_mode is not None:
                return group_mode
    return default_mode


def _group_mode_for_label(
    label: str,
    resolved: dict[str, Any],
    default_mode: str,
) -> str | None:
    """Resolve a detection label's mode via the groups that contain it.

    Returns ``None`` when no covering group has an explicit mode (the caller
    falls back to the default). A label inside several groups with different
    modes resolves to the most specific group (fewest members - the tightest
    umbrella); ties break alphabetically so the result is deterministic.
    """
    group_modes = resolved.get('group_modes')
    if not isinstance(group_modes, dict) or not group_modes:
        return None
    groups = cached_label_groups()
    covering: list[tuple[int, str, str]] = []
    for group, raw_mode in group_modes.items():
        members = groups.get(group)
        if members is None or label not in members:
            continue
        covering.append((len(members), group, raw_mode))
    if not covering:
        return None
    covering.sort(key=lambda item: (item[0], item[1]))
    return normalize_mode(covering[0][2], default_mode)


def _normalize_threshold_map(value: Any) -> dict[str, int]:
    """Normalize a raw ``{label: minutes}`` still-alert map."""
    if not isinstance(value, dict):
        return {}
    thresholds: dict[str, int] = {}
    for raw_label, raw_minutes in value.items():
        label = canonical_label(raw_label)
        if not label:
            continue
        minutes = _normalize_still_alert_minutes(raw_minutes)
        if minutes is not None:
            thresholds[label] = minutes
    return thresholds


def still_alert_thresholds(settings: dict[str, Any] | None = None) -> dict[str, int]:
    """The effective per-label still-alert thresholds (``label -> minutes``).

    Only labels with a real threshold are included; everything else has no
    still-alert and never participates in dwell tracking. Accepts either a
    full ``objects`` settings dict (reads its ``still_alerts`` key) or a raw
    ``{label: minutes}`` map, so callers on the hot path can pass the already
    resolved thresholds back in.
    """
    resolved = settings if settings is not None else effective_object_settings()
    raw = resolved.get('still_alerts') if isinstance(resolved, dict) else None
    return _normalize_threshold_map(raw)


def update_still_dwell_alerts(
    camera_id: str,
    detections: list[dict[str, Any]],
    still_alerts: dict[str, Any] | None = None,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Advance still-dwell streaks and return detections that just crossed theirs.

    A label with a threshold in ``still_alerts`` starts a streak the first
    cycle it is detected **still** (``motion_state == 'still'`` -- its pixels
    have stopped changing, i.e. it has been absorbed into the background
    model). Every later cycle that still detects the label as still extends the
    streak; the cycle that first exceeds ``N`` minutes emits one dwell alert
    detection (with ``still_alert`` / ``still_alert_minutes`` flags). The
    streak is dropped as soon as a cycle no longer reports the label as still
    (it moved, left the frame, or fell below the detector), so the next still
    run starts from zero and can alert again.

    Dwell alerts are **label-level**: two still packages of the same class
    share one streak, so the first to cross the threshold fires the alert.
    Per-streak fire-once semantics are enforced with an ``alerted`` flag, so a
    subject that stays still for an hour alerts once, not every cycle.

    Returns the newly-crossed dwell detections (usually zero or one per label),
    copied from the underlying detection so the box/confidence still describe
    the object at alert time.
    """
    if still_alerts is None:
        thresholds = still_alert_thresholds()
    else:
        thresholds = _normalize_threshold_map(still_alerts)
    if not thresholds:
        return []
    ts = time.time() if now is None else float(now)
    with _state._still_dwell_lock:
        per_camera = _state._still_dwell.setdefault(str(camera_id), {})
        # An empty cycle (no still detections at all -- the subject moved or
        # left an otherwise-empty frame) must still drop every existing streak,
        # so ``detections`` being empty falls through to the reset loop below
        # rather than short-circuiting and preserving a stale streak.
        if not detections and not per_camera:
            return []
        still_now: set[str] = set()
        emitted: list[dict[str, Any]] = []
        for detection in detections:
            if detection.get('motion_state') != MODE_STILL:
                continue
            label = canonical_label(detection.get('label'))
            if not label or label not in thresholds:
                continue
            minutes = thresholds[label]
            still_now.add(label)
            entry = per_camera.get(label)
            if entry is None:
                per_camera[label] = {'still_since': ts, 'alerted': False}
                continue
            if entry.get('alerted'):
                continue
            if ts - entry['still_since'] >= minutes * 60:
                entry['alerted'] = True
                emitted.append({
                    **detection,
                    'still_alert': True,
                    'still_alert_minutes': minutes,
                })
        # Any label whose still streak broke (absent this cycle, or detected
        # moving) starts over next time it is still again.
        for label in list(per_camera):
            if label not in still_now:
                del per_camera[label]
        return emitted


def detection_motion_state(
    detection: dict[str, Any],
    diff_mask: Any,
) -> str:
    """Classify one detection as ``moving`` or ``still``.

    ``diff_mask`` is the boolean (HxW) changed-pixel thumbnail from
    ``detect_frame_motion`` (or ``None`` when no change was measured / the
    mask is unavailable). ``None`` maps to ``still``: no pixel change was
    measured, so the subject is treated as still (see module docstring).
    """
    if diff_mask is None:
        return MODE_STILL
    box = detection.get('box')
    if not isinstance(box, dict):
        return MODE_STILL
    try:
        import numpy as np
        mask = np.asarray(diff_mask)
        if mask.ndim != 2 or mask.size == 0:
            return MODE_STILL
        height, width = mask.shape
        try:
            x = float(box.get('x') or 0.0)
            y = float(box.get('y') or 0.0)
            w = float(box.get('width') or 0.0)
            h = float(box.get('height') or 0.0)
        except (TypeError, ValueError):
            return MODE_STILL
        # Map the normalised box onto the thumbnail grid. Clamp so a
        # degenerate box can never produce an empty/NaN slice.
        x1 = int(round(max(0.0, min(1.0, x)) * width))
        y1 = int(round(max(0.0, min(1.0, y)) * height))
        x2 = int(round(max(0.0, min(1.0, x + max(0.0, w))) * width))
        y2 = int(round(max(0.0, min(1.0, y + max(0.0, h))) * height))
        x1, x2 = min(x1, width), max(min(x2, width), min(x1, width))
        y1, y2 = min(y1, height), max(min(y2, height), min(y1, height))
        if x2 <= x1 or y2 <= y1:
            return MODE_STILL
        region = mask[y1:y2, x1:x2]
        if region.size == 0:
            return MODE_STILL
        changed = int(region.sum())
        if changed < _MOVING_MIN_CHANGED_PIXELS:
            return MODE_STILL
        fraction = float(changed) / float(region.size)
        return MODE_MOVING if fraction >= _MOVING_BOX_FRACTION else MODE_STILL
    except Exception as exc:  # pragma: no cover - unexpected mask/box shapes
        logger.debug('Unexpected error classifying detection motion state: %s', exc)
        return MODE_STILL


def filter_detections_by_motion_mode(
    detections: list[dict[str, Any]],
    diff_mask: Any,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Drop detections whose label's mode does not allow their motion state.

    Each surviving detection is annotated with ``motion_state`` (``moving`` /
    ``still``) so overlays and status payloads can tag how it was classified.
    The annotation is applied even when no label is restricted -- an ``any``
    configuration returns everything, but every detection still carries its
    classification so the live view and timeline can show it. Motion-zone
    detections never pass through this filter and stay untagged.

    Fast path: when no label is restricted and there is no diff mask, no pixel
    change was measured so every detection is ``still`` by definition; the
    annotation is stamped without per-box numpy work.
    """
    if not detections:
        return detections
    resolved = settings if settings is not None else effective_object_settings()
    restricted_labels: set[str] = set()
    for detection in detections:
        label = canonical_label(detection.get('label'))
        if not label:
            continue
        if motion_mode_for_label(label, resolved) != MODE_ANY:
            restricted_labels.add(label)
    if not restricted_labels and diff_mask is None:
        return [{**detection, 'motion_state': MODE_STILL} for detection in detections]

    filtered: list[dict[str, Any]] = []
    for detection in detections:
        label = canonical_label(detection.get('label'))
        mode = motion_mode_for_label(label, resolved) if label else MODE_ANY
        state = detection_motion_state(detection, diff_mask)
        if mode == MODE_ANY or mode == state:
            filtered.append({**detection, 'motion_state': state})
    return filtered


def still_dwell_candidates(
    detections: list[dict[str, Any]],
    diff_mask: Any,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Still detections for still-alert labels, ignoring the moving/still filter.

    The dwell tracker (``update_still_dwell_alerts``) can only advance a streak
    for a label it sees classified ``still`` this cycle. But
    ``filter_detections_by_motion_mode`` runs first and, under the default
    ``moving`` mode, DROPS every still detection -- so a label left on the
    default would never accrue a streak and its configured "still for N minutes"
    alert would silently never fire. Still-dwell alerts are an independent axis
    (see the module docstring), so this selects the still detections for labels
    that actually have a threshold, straight from the pre-filter detection list,
    annotated ``motion_state='still'``. The caller feeds these to the dwell
    tracker so the alert works regardless of the label's detection mode, while
    the normal alert/record pipeline keeps honouring "Moving Only".

    Returns an empty list when nothing has a still-alert threshold, so the hot
    path skips all per-box classification work in the common case.
    """
    if not detections:
        return []
    resolved = settings if settings is not None else effective_object_settings()
    thresholds = still_alert_thresholds(resolved)
    if not thresholds:
        return []
    candidates: list[dict[str, Any]] = []
    for detection in detections:
        label = canonical_label(detection.get('label'))
        if not label or label not in thresholds:
            continue
        if detection_motion_state(detection, diff_mask) == MODE_STILL:
            candidates.append({**detection, 'motion_state': MODE_STILL})
    return candidates
