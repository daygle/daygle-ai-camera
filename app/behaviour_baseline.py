"""Behavioural intelligence primitives (Tier 2): statistical dwell baselines.

Pure, dependency-free statistics for **loitering / long-dwell** anomaly
detection. Where Tier 1 (``app/behaviour.py``) is a fixed geometric rule (a
line you draw), Tier 2 *learns your site*: it accumulates how long tracked
objects normally dwell in a zone and flags one that stays far longer than
that norm. Keeping this module free of heavy dependencies means it is cheap on
the per-frame path and trivially unit-testable; the live wiring and
persistence layer live elsewhere (a later phase), mirroring how Tier 1 split
``behaviour.py`` (pure) from ``behaviour_monitor.py`` (wiring).

The baseline is a running mean/variance (Welford's online algorithm) over
observed dwell times, per ``(zone, label)``. The anomaly threshold is::

    threshold = max(min_dwell_seconds, mean + sensitivity * std)

so a configured floor guarantees a minimum, while the learned term raises the
bar in zones where objects *naturally* linger (a patio) and leaves it at the
floor where they never do (a driveway). Until the baseline has
``min_samples`` observations it is still "learning" and only the floor applies,
so the feature is useful on day one and sharpens as history accrues.
"""
from __future__ import annotations

import math
from typing import Any

# Defaults, all overridable per zone. ``MIN_SAMPLES`` is how many dwell
# observations a (zone, label) baseline needs before its learned mean/std is
# trusted; below it, only the configured floor fires. ``DEFAULT_SENSITIVITY``
# is the std multiplier ``k`` in ``mean + k*std``. ``DEFAULT_MIN_DWELL_SECONDS``
# is the absolute floor below which nothing is ever "loitering".
MIN_SAMPLES = 30
DEFAULT_SENSITIVITY = 3.0
DEFAULT_MIN_DWELL_SECONDS = 30

# A single dwell observation longer than this is treated as a stuck/merged
# track rather than real presence, and is ignored when updating a baseline so
# one runaway value cannot poison the learned mean. It does NOT stop such a
# dwell from being flagged as anomalous -- only from teaching the baseline that
# "normal" is enormous.
MAX_SANE_DWELL_SECONDS = 3600.0


def new_dwell_baseline() -> dict[str, float]:
    """A fresh, empty baseline (Welford accumulator)."""
    return {'count': 0.0, 'mean': 0.0, 'm2': 0.0}


def _coerce_baseline(baseline: Any) -> dict[str, float]:
    """Coerce a possibly-persisted baseline dict to the canonical shape,
    tolerating missing keys / bad types (returns a fresh one on junk)."""
    if not isinstance(baseline, dict):
        return new_dwell_baseline()
    out = new_dwell_baseline()
    for key in ('count', 'mean', 'm2'):
        try:
            value = float(baseline.get(key, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        out[key] = value if math.isfinite(value) else 0.0
    # count and m2 are non-negative by construction, and dwell seconds are never
    # negative, so clamp defensively against a corrupted persisted baseline.
    out['count'] = max(0.0, out['count'])
    out['m2'] = max(0.0, out['m2'])
    out['mean'] = max(0.0, out['mean'])
    return out


def update_dwell_baseline(baseline: Any, dwell_seconds: Any) -> dict[str, float]:
    """Fold one dwell observation into ``baseline`` (Welford). Returns the
    updated baseline dict (mutated in place when already canonical).

    A non-finite, negative, or absurdly large dwell is ignored so the learned
    "normal" stays representative; the baseline is returned unchanged.
    """
    state = _coerce_baseline(baseline)
    try:
        value = float(dwell_seconds)
    except (TypeError, ValueError):
        return state
    if not math.isfinite(value) or value < 0.0 or value > MAX_SANE_DWELL_SECONDS:
        return state
    state['count'] += 1.0
    delta = value - state['mean']
    state['mean'] += delta / state['count']
    delta2 = value - state['mean']
    state['m2'] += delta * delta2
    return state


def baseline_mean(baseline: Any) -> float:
    return _coerce_baseline(baseline)['mean']


def baseline_std(baseline: Any) -> float:
    """Sample standard deviation of the observed dwells (0 with <2 samples)."""
    state = _coerce_baseline(baseline)
    count = state['count']
    if count < 2.0:
        return 0.0
    variance = state['m2'] / (count - 1.0)
    return math.sqrt(variance) if variance > 0.0 else 0.0


def dwell_threshold(
    baseline: Any,
    *,
    min_dwell_seconds: float = DEFAULT_MIN_DWELL_SECONDS,
    sensitivity: float = DEFAULT_SENSITIVITY,
    min_samples: int = MIN_SAMPLES,
) -> float:
    """The dwell (seconds) at or above which a track counts as loitering.

    ``max(min_dwell_seconds, mean + sensitivity*std)`` once the baseline has
    ``min_samples`` observations; just ``min_dwell_seconds`` while still
    learning.
    """
    floor = max(0.0, float(min_dwell_seconds))
    state = _coerce_baseline(baseline)
    if state['count'] < float(min_samples):
        return floor
    learned = state['mean'] + max(0.0, float(sensitivity)) * baseline_std(baseline)
    return max(floor, learned)


def evaluate_dwell(
    baseline: Any,
    dwell_seconds: Any,
    *,
    min_dwell_seconds: float = DEFAULT_MIN_DWELL_SECONDS,
    sensitivity: float = DEFAULT_SENSITIVITY,
    min_samples: int = MIN_SAMPLES,
) -> dict[str, Any]:
    """Classify one dwell against a learned baseline. Pure / no side effects.

    Returns::

        {
            "anomalous": bool,   # dwell >= threshold (loitering)
            "dwell": float,      # the observed dwell, seconds
            "threshold": float,  # the effective threshold used
            "mean": float,       # learned normal dwell (0 until sampled)
            "std": float,
            "score": float,      # dwell / threshold (>= 1.0 when anomalous)
            "learning": bool,    # True until min_samples observations
        }

    A non-finite or negative dwell is never anomalous.
    """
    state = _coerce_baseline(baseline)
    learning = state['count'] < float(min_samples)
    threshold = dwell_threshold(
        baseline,
        min_dwell_seconds=min_dwell_seconds,
        sensitivity=sensitivity,
        min_samples=min_samples,
    )
    try:
        dwell = float(dwell_seconds)
    except (TypeError, ValueError):
        dwell = float('nan')
    valid = math.isfinite(dwell) and dwell >= 0.0
    anomalous = bool(valid and threshold > 0.0 and dwell >= threshold)
    score = (dwell / threshold) if (valid and threshold > 0.0) else 0.0
    return {
        'anomalous': anomalous,
        'dwell': dwell if valid else 0.0,
        'threshold': threshold,
        'mean': state['mean'],
        'std': baseline_std(baseline),
        'score': score,
        'learning': learning,
    }


def dwell_seconds(first_ts: Any, last_ts: Any) -> float | None:
    """Dwell (seconds) between a track's first and last observation, or None
    when either timestamp is missing/invalid. Negative spans (clock skew)
    clamp to 0."""
    try:
        start = float(first_ts)
        end = float(last_ts)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end):
        return None
    return max(0.0, end - start)
