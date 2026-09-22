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


# How long a track may go unseen inside a zone before its visit is considered
# finished (and its dwell folded into the baseline as a completed sample). A few
# seconds absorbs the tracker's own miss/re-acquire gaps at 2-4 Hz.
DEFAULT_DEPARTURE_GRACE_SECONDS = 5.0


def _cooldown_ok(last_fired: float | None, now: float, cooldown_seconds: float) -> bool:
    if cooldown_seconds <= 0 or last_fired is None:
        return True
    return (now - last_fired) >= cooldown_seconds


def loiter_step(
    presence: Any,
    baselines: Any,
    cooldowns: Any,
    observations: Any,
    now: float,
    *,
    departure_grace_seconds: float = DEFAULT_DEPARTURE_GRACE_SECONDS,
) -> dict[str, Any]:
    """Advance one detection cycle of loiter tracking. Pure (mutates the three
    passed dicts, does no I/O), so the whole decision is unit-testable.

    ``presence`` maps a per-visit key (``camera|zone|track``) to a record of
    when the track was first/last seen inside the zone and whether it has
    already fired. ``baselines`` maps ``camera|zone|label`` to a Welford dwell
    baseline; ``cooldowns`` maps the same key to the last loiter-alert time.
    ``observations`` is the list of tracks present inside a loiter-enabled zone
    THIS cycle, each::

        {"key", "baseline_key", "zone_id", "zone_name", "label", "track_id",
         "confidence", "min_dwell_seconds", "sensitivity", "cooldown_seconds"}

    Returns ``{"fires": [...], "learned": int}`` where each fire is a loiter
    anomaly that should alert now::

        {"zone_id", "zone_name", "label", "track_id", "dwell", "threshold",
         "mean", "confidence"}

    A visit fires at most once (while it stays present); a completed visit -- a
    track not seen for ``departure_grace_seconds`` -- is removed from
    ``presence`` and its total dwell folded into the matching baseline, so the
    baseline learns from *finished* visits rather than the currently-loitering
    one.
    """
    if not isinstance(presence, dict):
        presence = {}
    if not isinstance(baselines, dict):
        baselines = {}
    if not isinstance(cooldowns, dict):
        cooldowns = {}
    fires: list[dict[str, Any]] = []
    present_keys: set[str] = set()

    for obs in observations or []:
        if not isinstance(obs, dict):
            continue
        key = obs.get('key')
        if not key:
            continue
        present_keys.add(key)
        baseline_key = obs.get('baseline_key') or ''
        rec = presence.get(key)
        if rec is None:
            rec = {
                'first_seen': now,
                'last_seen': now,
                'fired': False,
                'baseline_key': baseline_key,
                'label': obs.get('label'),
                'zone_id': obs.get('zone_id'),
                'zone_name': obs.get('zone_name'),
                'track_id': obs.get('track_id'),
            }
            presence[key] = rec
        else:
            rec['last_seen'] = now
            if baseline_key:
                rec['baseline_key'] = baseline_key
        if rec['fired']:
            continue
        try:
            min_dwell = float(obs.get('min_dwell_seconds', DEFAULT_MIN_DWELL_SECONDS))
        except (TypeError, ValueError):
            min_dwell = float(DEFAULT_MIN_DWELL_SECONDS)
        try:
            sensitivity = float(obs.get('sensitivity', DEFAULT_SENSITIVITY))
        except (TypeError, ValueError):
            sensitivity = DEFAULT_SENSITIVITY
        result = evaluate_dwell(
            baselines.get(baseline_key), now - rec['first_seen'],
            min_dwell_seconds=min_dwell, sensitivity=sensitivity,
        )
        if not result['anomalous']:
            continue
        try:
            cooldown = max(0.0, float(obs.get('cooldown_seconds', 0)))
        except (TypeError, ValueError):
            cooldown = 0.0
        if not _cooldown_ok(cooldowns.get(baseline_key), now, cooldown):
            continue
        rec['fired'] = True
        cooldowns[baseline_key] = now
        fires.append({
            'zone_id': rec['zone_id'],
            'zone_name': rec['zone_name'],
            'label': rec['label'],
            'track_id': rec['track_id'],
            'dwell': result['dwell'],
            'threshold': result['threshold'],
            'mean': result['mean'],
            'confidence': obs.get('confidence'),
        })

    # Fold completed visits (departed tracks) into their baselines and drop them.
    learned = 0
    cutoff = now - departure_grace_seconds
    for key in list(presence.keys()):
        rec = presence[key]
        if key in present_keys or rec.get('last_seen', now) >= cutoff:
            continue
        baseline_key = rec.get('baseline_key') or ''
        baselines[baseline_key] = update_dwell_baseline(
            baselines.get(baseline_key), rec.get('last_seen', now) - rec.get('first_seen', now),
        )
        learned += 1
        del presence[key]

    return {'fires': fires, 'learned': learned}


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
