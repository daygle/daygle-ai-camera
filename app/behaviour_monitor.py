"""Behavioural intelligence monitor (Tier 1): tripwire crossing emission.

Turns the pure line-crossing detections from ``app/behaviour.py`` into events,
optional recordings, and email/push alerts, mirroring the self-contained sound
alert path (``app/sound_monitor._on_sound_detected``). Kept out of
``app/behaviour.py`` so that module stays dependency-free and unit-testable; the
heavy alert/recording dependencies here are imported lazily inside the functions
so this module also imports cleanly on its own.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, UTC
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import app.state as _state
from app import behaviour, behaviour_baseline
from app.alert_dispatch import submit_alert_notification

logger = logging.getLogger('daygle.ai.detection')

# Per-crossing cooldown: last wall-clock fire time keyed by
# ``camera|zone|direction|track`` so a subject straddling the line does not
# emit a burst of duplicate crossings.
_tripwire_last_fired: dict[str, float] = {}
_tripwire_lock = threading.Lock()

# Co-presence state, keyed ``camera|zone``: the labels seen recently in that
# area, whether the current "together" streak has already alerted, and when it
# last did. One alert goes out per co-presence episode instead of once per
# detection cycle; the streak re-arms once a required label has not been seen
# inside the rule's recent-sighting window.
_co_presence_state: dict[str, dict[str, Any]] = {}
_co_presence_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Camera-motion pause token (Item 10, P2 follow-up)
# ---------------------------------------------------------------------------
# The live monitor already SKIPS the behavioural engines while the camera-motion
# guard is active. That alone is not enough: a pause is only "conservative" if
# the first sample after resume cannot be influenced by presence accumulated
# BEFORE the camera moved. A PTZ pan makes every tracked box jump in image
# space, so without a reset the first post-motion cycle can read as a subject
# that has been loitering for the whole pan, or as a line crossing caused by
# the pan rather than by a subject.
#
# This token makes the pause explicit and gives it semantics:
#
#   * ONSET  (motion becomes active)  -> transitional state is dropped, so the
#     engines resume from nothing rather than from pre-pan presence.
#   * RESUME (motion clears)          -> the same reset, so the first post-motion
#     sample starts a fresh visit and a fresh crossing test.
#
# Baselines (learned distributions) and cooldowns are deliberately PRESERVED:
# a 0.4 s nudge must not erase an hour of learning or let the same anomaly
# re-fire immediately after the camera settles.
_behaviour_pause: dict[str, bool] = {}
_behaviour_pause_lock = threading.Lock()

PAUSE_ONSET = 'onset'
PAUSE_RESUME = 'resume'


def sync_behaviour_pause(camera_id: str, camera_motion_active: bool) -> str | None:
    """Track camera-motion suppression and reset behavioural state on a transition.

    Call once per detection cycle with the camera-motion verdict BEFORE running
    the behavioural engines. Returns ``'onset'``, ``'resume'``, or ``None`` when
    the suppression state did not change, so the caller can log/telemetry the
    transition without recomputing it.

    The reset drops per-camera TRANSITIONAL state only:

    * loiter presence (an in-progress visit) for this camera;
    * the time-of-day per-day observation tally for this camera;
    * tripwire per-track cooldowns for this camera.

    Learned baselines and the remaining cameras' state are untouched.
    """
    key = str(camera_id)
    try:
        with _behaviour_pause_lock:
            previous = _behaviour_pause.get(key, False)
            if bool(camera_motion_active) == previous:
                return None
            _behaviour_pause[key] = bool(camera_motion_active)
            transition = PAUSE_ONSET if camera_motion_active else PAUSE_RESUME
        if transition == PAUSE_ONSET:
            logger.debug('Behavioural engines paused for %s (camera motion onset)', key)
        else:
            logger.debug('Behavioural engines resumed for %s (camera motion cleared)', key)
        reset_behavioural_state(key)
        return transition
    except Exception as exc:  # noqa: BLE001 - pause bookkeeping must never break a cycle
        logger.debug('Behavioural pause sync failed for %s: %s', key, exc)
        return None


def reset_behavioural_state(camera_id: str) -> None:
    """Drop per-camera transitional behavioural state accumulated before a pause.

    Safe to call unconditionally; each map is simply re-seeded empty for this
    camera. Best-effort per store so one failing lock cannot leave the rest of
    the state stale. The pause FLAG is not touched: use
    :func:`clear_behavioural_state` to forget a camera entirely.
    """
    key = str(camera_id)
    try:
        with _loiter_lock:
            _loiter_presence.pop(key, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug('Loiter presence reset failed for %s: %s', key, exc)
    try:
        # ``_time_today`` is keyed ``camera|zone|label`` (like
        # ``_activity_today``), so a bare camera-id pop never matched.
        prefix = f'{key}|'
        with _time_lock:
            for stale in [k for k in _time_today if k.startswith(prefix)]:
                _time_today.pop(stale, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug('Time-of-day state reset failed for %s: %s', key, exc)
    try:
        prefix = f'{key}|'
        with _tripwire_lock:
            for stale in [k for k in _tripwire_last_fired if k.startswith(prefix)]:
                _tripwire_last_fired.pop(stale, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug('Tripwire cooldown reset failed for %s: %s', key, exc)
    try:
        prefix = f'{key}|'
        with _co_presence_lock:
            for stale in [k for k in _co_presence_state if k.startswith(prefix)]:
                entry = _co_presence_state[stale]
                # What this camera saw before it moved is not evidence of who is
                # together after it settles, so the sightings and the in-progress
                # streak are dropped. The cooldown is preserved (like the
                # tripwire/loiter cooldowns); an entry that never fired is
                # forgotten entirely so the map cannot grow without bound.
                entry['labels'] = {}
                entry['active'] = False
                if entry.get('last_fired') is None:
                    _co_presence_state.pop(stale, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug('Co-presence state reset failed for %s: %s', key, exc)
    try:
        # ``_activity_today`` is keyed ``camera|zone|label``; drop this camera's
        # in-progress hour buckets so a burst isn't falsely continued after a pause.
        prefix = f'{key}|'
        with _activity_lock:
            for stale in [k for k in _activity_today if k.startswith(prefix)]:
                _activity_today.pop(stale, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug('Activity-spike state reset failed for %s: %s', key, exc)


def clear_behavioural_state(camera_id: str) -> None:
    """Forget a camera entirely (deletion / config prune).

    Same transitional-state reset as a pause transition, but the camera's pause
    flag is dropped too, so a camera that is later re-added with the same id
    starts unsuppressed instead of inheriting a stale "paused" verdict.
    """
    key = str(camera_id)
    try:
        with _behaviour_pause_lock:
            _behaviour_pause.pop(key, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug('Behavioural pause flag reset failed for %s: %s', key, exc)
    reset_behavioural_state(key)


def _enabled_zones_with_tripwire(settings: Any) -> list[dict[str, Any]]:
    zones = (settings.get('detection') or {}).get('zones', []) if isinstance(settings, dict) else []
    out: list[dict[str, Any]] = []
    for zone in zones or []:
        if not isinstance(zone, dict) or zone.get('enabled') is False:
            continue
        wire = zone.get('tripwire')
        if isinstance(wire, dict) and wire.get('enabled') is not False:
            out.append(zone)
    return out


def emit_tripwire_crossings(camera_id: str, settings: dict[str, Any], detections: list[dict[str, Any]]) -> int:
    """Detect and emit tripwire crossings for one detection cycle.

    Returns the number of crossings that fired an event (i.e. were past their
    cooldown). Best-effort: an individual crossing's recording/notification
    failure is logged and never raised, so behavioural code cannot break the
    detection loop.
    """
    zones = _enabled_zones_with_tripwire(settings)
    if not zones:
        return 0
    crossings = behaviour.zone_tripwire_crossings(detections, zones)
    if not crossings:
        return 0
    wires = {str(z.get('id') or z.get('name') or ''): z.get('tripwire') for z in zones}
    now = time.time()
    fired = 0
    for crossing in crossings:
        wire = wires.get(crossing['zone_id']) or {}
        try:
            cooldown = max(0, int(wire.get('cooldown_seconds') or 0))
        except (TypeError, ValueError):
            cooldown = 0
        key = f"{camera_id}|{crossing['zone_id']}|{crossing['direction']}|{crossing['track_id']}"
        with _tripwire_lock:
            if not behaviour.cooldown_passed(_tripwire_last_fired.get(key), now, cooldown):
                continue
            _tripwire_last_fired[key] = now
            # Track ids climb forever, so the cooldown map would grow without
            # bound; opportunistically drop entries older than an hour once it
            # gets large. An expired entry only means "no cooldown", which is
            # already the default for a never-seen key.
            if len(_tripwire_last_fired) > 2048:
                cutoff = now - 3600
                for stale in [k for k, ts in _tripwire_last_fired.items() if ts < cutoff]:
                    _tripwire_last_fired.pop(stale, None)
        try:
            _emit_one(camera_id, settings, crossing, wire)
            fired += 1
        except Exception as exc:  # noqa: BLE001 - one crossing must not break the loop
            logger.warning('Tripwire crossing emit failed on %s: %s', camera_id, exc)
    return fired


def _emit_one(camera_id: str, settings: dict[str, Any], crossing: dict[str, Any], wire: dict[str, Any]) -> None:
    now_iso = datetime.now(UTC).isoformat()
    zone_name = crossing.get('zone_name') or 'zone'
    wire_name = crossing.get('tripwire_name') or 'Tripwire'
    direction = crossing['direction']
    label = crossing.get('label') or 'object'
    confidence = _as_float(crossing.get('confidence'))
    event_id = _emit_behaviour_event(
        camera_id, settings, wire, now_iso,
        source='line-crossing', fire=crossing, label=label, confidence=confidence,
        rule_name=f'{zone_name} · {wire_name}',
        message=f'{str(label).title()} crossed {wire_name} ({direction}) in {zone_name}',
        details={
            'tripwire_name': wire_name,
            'direction': direction,
            'label': label,
            'track_id': crossing.get('track_id'),
        },
    )
    logger.info('Tripwire crossing on %s: %s %s -> %s (event %s)', camera_id, label, wire_name, direction, event_id)


# ─── Multi-object co-presence (cat + person, dog + person, ...) ─────────────
# Unlike the per-label object rules, this fires only when several kinds of
# object are in the same area at once. It costs no extra detection work: it
# reads the tracked detections of the cycle that already have a label and a box.

def _enabled_zones_with_co_presence(settings: Any) -> list[dict[str, Any]]:
    zones = (settings.get('detection') or {}).get('zones', []) if isinstance(settings, dict) else []
    out: list[dict[str, Any]] = []
    for zone in zones or []:
        if not isinstance(zone, dict) or zone.get('enabled') is False:
            continue
        rule = zone.get('co_presence')
        if isinstance(rule, dict) and rule.get('enabled') is not False:
            out.append(zone)
    return out


def _co_presence_group(label: Any) -> set[str]:
    """A configured label plus every concrete label its group covers."""
    from app.label_groups import cached_label_groups
    from app.zone_schema import canonical_label

    configured = canonical_label(label)
    if not configured:
        return set()
    return {configured} | set(cached_label_groups().get(configured) or ())


def _co_presence_rule_groups(rule: dict[str, Any]) -> tuple[list[set[str]], list[set[str]]]:
    """``all_of`` / ``any_of`` as lists of concrete-label sets."""
    all_of = [_co_presence_group(label) for label in (rule.get('all_of') or [])]
    any_of = [_co_presence_group(label) for label in (rule.get('any_of') or [])]
    return [group for group in all_of if group], [group for group in any_of if group]


def _co_presence_phrase(labels: list[str]) -> str:
    names = [str(label).replace('_', ' ').title() for label in labels if label]
    if not names:
        return 'Objects'
    if len(names) == 1:
        return names[0]
    return ' + '.join(names)


def emit_co_presence_alerts(camera_id: str, settings: dict[str, Any], detections: list[dict[str, Any]]) -> int:
    """Emit one alert per co-presence episode for each enabled zone rule.

    A sighting is remembered per (camera, zone) instead of being read only from
    the current frame, so an object that was seen a few seconds ago still counts
    as present -- a sitting cat under Moving Only is not detected every cycle.
    Returns how many episodes were emitted. Best-effort and isolated: a failure
    is logged and never raised out of the detection loop.
    """
    zones = _enabled_zones_with_co_presence(settings)
    from app.zone_detection import detection_matches_zone
    from app.zone_schema import canonical_label

    now = time.time()
    to_fire: list[tuple[dict[str, Any], dict[str, Any], list[str], float]] = []
    with _co_presence_lock:
        live_keys: set[str] = set()
        for zone in zones:
            rule = zone.get('co_presence') or {}
            all_of, any_of = _co_presence_rule_groups(rule)
            if not all_of and not any_of:
                continue
            zone_id = str(zone.get('id') or zone.get('name') or '')
            key = f'{camera_id}|{zone_id}'
            live_keys.add(key)
            entry = _co_presence_state.setdefault(
                key,
                {'labels': {}, 'active': False, 'last_fired': None},
            )
            labels: dict[str, float] = entry.setdefault('labels', {})
            try:
                window = max(1.0, float(rule.get('window_seconds') or 15))
            except (TypeError, ValueError):
                window = 15.0
            # The episode belongs to the rule that was in force when it began.
            # A different label set or recent-sighting window is a different
            # condition: sightings gathered for the old one are not evidence for
            # the new one, and the episode must re-arm so the edited rule can
            # alert. The cooldown is cleared too: the flag above is set before
            # the cooldown test, so a preserved cooldown would swallow the rest
            # of this episode entirely instead of merely delaying its first
            # alert.
            signature = (
                tuple(sorted(str(label) for label in (rule.get('all_of') or []))),
                tuple(sorted(str(label) for label in (rule.get('any_of') or []))),
                window,
            )
            if entry.get('signature') != signature:
                labels.clear()
                entry['active'] = False
                entry['last_fired'] = None
                entry['signature'] = signature
            confidences: list[float] = []
            for det in detections or []:
                if not isinstance(det, dict) or det.get('motion_event'):
                    continue
                label = canonical_label(det.get('label'))
                if not label or label in ('motion', 'face'):
                    continue
                if not detection_matches_zone(det, zone):
                    continue
                labels[label] = now
                try:
                    confidences.append(float(det.get('confidence') or 0.0))
                except (TypeError, ValueError):
                    confidences.append(0.0)
            # Bound the map: a sighting older than the window (with a generous
            # margin for a group's label flips) can never satisfy the rule.
            cutoff = now - max(window, 60.0)
            for stale in [label for label, ts in labels.items() if ts < cutoff]:
                labels.pop(stale, None)
            if not behaviour.co_presence_met(all_of, any_of, labels, now, window):
                entry['active'] = False
                continue
            if entry.get('active'):
                continue  # already alerted for this episode
            entry['active'] = True
            try:
                cooldown = max(0.0, float(rule.get('cooldown_seconds') or 0))
            except (TypeError, ValueError):
                cooldown = 0.0
            if not behaviour.cooldown_passed(entry.get('last_fired'), now, cooldown):
                # Still the same episode: the streak stays marked active, so it
                # does not fire the moment the cooldown lapses.
                continue
            entry['last_fired'] = now
            recent = behaviour.labels_seen_recently(labels, now, window)
            present = sorted(
                label for label in recent
                if any(label in group for group in all_of + any_of)
            ) or sorted(recent)
            to_fire.append((zone, rule, present, max(confidences, default=0.0)))
        # A zone or rule that was deleted or disabled (or whose label lists
        # were emptied) is not visited by the loop above, so its episode would
        # otherwise live on forever. Drop this camera's orphaned entries.
        prefix = f'{camera_id}|'
        for stale in [key for key in _co_presence_state if key.startswith(prefix) and key not in live_keys]:
            _co_presence_state.pop(stale, None)

    fired = 0
    now_iso = datetime.now(UTC).isoformat()
    for zone, rule, present, confidence in to_fire:
        try:
            _emit_co_presence(camera_id, settings, zone, rule, present, confidence, now_iso)
            fired += 1
        except Exception as exc:  # noqa: BLE001 - one episode must not break the loop
            logger.warning('Co-presence emit failed on %s: %s', camera_id, exc)
    return fired


def _emit_co_presence(
    camera_id: str,
    settings: dict[str, Any],
    zone: dict[str, Any],
    rule: dict[str, Any],
    present: list[str],
    confidence: float,
    now_iso: str,
) -> None:
    zone_id = str(zone.get('id') or zone.get('name') or '')
    zone_name = str(zone.get('name') or zone.get('id') or '').strip() or 'zone'
    primary = present[0] if present else 'object'
    # Name every object that is together in the alert-history row and in the
    # email/push text, while the event still records the concrete detection.
    combined = ' + '.join(label for label in present if label) or 'object'
    event_id = _emit_behaviour_event(
        camera_id, settings, rule, now_iso,
        source='co_presence',
        fire={'zone_id': zone_id, 'zone_name': zone_name},
        label=primary,
        alert_label=combined,
        confidence=confidence,
        rule_name=f'{zone_name} · {_rule_display(rule, "Co-presence")}',
        message=f'{_co_presence_phrase(present)} together in {zone_name}',
        details={
            'labels': present,
            'all_of': list(rule.get('all_of') or []),
            'any_of': list(rule.get('any_of') or []),
            'window_seconds': rule.get('window_seconds'),
        },
    )
    logger.info(
        'Co-presence on %s: %s in %s (event %s)',
        camera_id, ', '.join(present) or 'objects', zone_name, event_id,
    )


# ─── Tier-2 behavioural intelligence: statistical loitering / long-dwell ─────
# Per-visit presence + per-(camera, zone, label) learned dwell baselines, all
# guarded by one lock. The baseline map is loaded from the app_settings KV store
# on first use and flushed back periodically (throttled), so what the site has
# learned survives a restart. The pure decision logic lives in
# ``behaviour_baseline.loiter_step``; this layer only supplies observations,
# persistence, and the event/recording/alert emission.
# Presence is per-camera (``camera_id -> {visit_key -> record}``): loiter_step
# prunes any visit not seen in the cycle's observations, so each camera must
# only ever see its own presence dict, or one camera's cycle would retire
# another's still-active visits. Baselines and cooldowns are global maps keyed by
# ``camera|zone|label``, so they never collide across cameras.
_loiter_presence: dict[str, dict[str, dict[str, Any]]] = {}
_loiter_baselines: dict[str, dict[str, float]] = {}
_loiter_cooldowns: dict[str, float] = {}
_loiter_lock = threading.Lock()
_loiter_state: dict[str, Any] = {'loaded': False, 'dirty': False, 'last_flush': 0.0}
_LOITER_BASELINES_KEY = 'behaviour_loiter_baselines'
_LOITER_FLUSH_INTERVAL_SECONDS = 60.0


def _enabled_zones_with_loiter(settings: Any) -> list[dict[str, Any]]:
    zones = (settings.get('detection') or {}).get('zones', []) if isinstance(settings, dict) else []
    out: list[dict[str, Any]] = []
    for zone in zones or []:
        if not isinstance(zone, dict) or zone.get('enabled') is False:
            continue
        rule = zone.get('loiter')
        if isinstance(rule, dict) and rule.get('enabled') is not False:
            out.append(zone)
    return out


def _ensure_loiter_loaded_locked() -> None:
    if _loiter_state['loaded']:
        return
    try:
        raw = _state.database.get_setting(_LOITER_BASELINES_KEY)
    except Exception as exc:  # noqa: BLE001 - a missing/locked DB just means "start fresh"
        # NOT marked loaded: the read is retried next cycle. Marking it loaded
        # here would leave the in-memory map empty, and the first flush then
        # writes that empty map over the stored baselines -- a transient DB
        # error would erase everything the site has learned.
        logger.debug('Loiter baseline load failed: %s', exc)
        return
    _loiter_state['loaded'] = True
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                _loiter_baselines[str(key)] = {
                    'count': max(0.0, float(value.get('count') or 0.0)),
                    'mean': max(0.0, float(value.get('mean') or 0.0)),
                    'm2': max(0.0, float(value.get('m2') or 0.0)),
                }
            except (TypeError, ValueError):
                continue


def _flush_loiter_baselines_locked(now: float, now_iso: str) -> None:
    if not _loiter_state['loaded']:
        # Never write a map that could not be read: this flush would replace the
        # stored baselines with whatever partial state is in memory.
        return
    if not _loiter_state['dirty'] or (now - _loiter_state['last_flush']) < _LOITER_FLUSH_INTERVAL_SECONDS:
        return
    try:
        _state.database.set_setting(_LOITER_BASELINES_KEY, dict(_loiter_baselines), now_iso)
        _loiter_state['dirty'] = False
        _loiter_state['last_flush'] = now
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort; keep learning in memory
        logger.warning('Loiter baseline flush failed: %s', exc)


def emit_loiter_anomalies(camera_id: str, settings: dict[str, Any], detections: list[dict[str, Any]]) -> int:
    """Detect and emit loitering (long-dwell) anomalies for one detection cycle.

    Tracks how long each object dwells inside a loiter-enabled zone, learns the
    zone's normal dwell distribution from completed visits, and fires when a
    visit runs far longer than normal. Returns the number of anomalies emitted.
    Best-effort and fully isolated: an individual emission failure is logged and
    never raised.
    """
    zones = _enabled_zones_with_loiter(settings)
    if not zones:
        return 0
    from app.zone_detection import detection_matches_zone

    observations: list[dict[str, Any]] = []
    loiter_by_zone: dict[str, dict[str, Any]] = {}
    for zone in zones:
        rule = zone.get('loiter') or {}
        zone_id = str(zone.get('id') or zone.get('name') or '')
        zone_name = str(zone.get('name') or zone.get('id') or '').strip() or None
        loiter_by_zone[zone_id] = rule
        wanted = {str(label).strip().lower() for label in (rule.get('labels') or []) if str(label).strip()}
        for det in detections or []:
            if not isinstance(det, dict) or det.get('track_id') is None:
                continue
            label = str(det.get('label') or '').strip().lower()
            if wanted and label not in wanted:
                continue
            if not detection_matches_zone(det, zone):
                continue
            try:
                confidence = float(det.get('confidence') or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            observations.append({
                'key': f"{camera_id}|{zone_id}|{det.get('track_id')}",
                'baseline_key': f"{camera_id}|{zone_id}|{label}",
                'zone_id': zone_id,
                'zone_name': zone_name,
                'label': label,
                'track_id': det.get('track_id'),
                'confidence': confidence,
                'min_dwell_seconds': rule.get('min_dwell_seconds', 30),
                'sensitivity': rule.get('sensitivity', 3.0),
                'cooldown_seconds': rule.get('cooldown_seconds', 120),
            })

    now = time.time()
    now_iso = datetime.now(UTC).isoformat()
    with _loiter_lock:
        _ensure_loiter_loaded_locked()
        presence = _loiter_presence.setdefault(camera_id, {})
        result = behaviour_baseline.loiter_step(
            presence, _loiter_baselines, _loiter_cooldowns, observations, now,
        )
        if result['learned']:
            _loiter_state['dirty'] = True
        # Track ids climb forever; keep the cooldown map from growing without
        # bound by dropping day-old entries once it gets large.
        if len(_loiter_cooldowns) > 4096:
            cutoff = now - 86400
            for stale in [k for k, ts in _loiter_cooldowns.items() if ts < cutoff]:
                _loiter_cooldowns.pop(stale, None)
        _flush_loiter_baselines_locked(now, now_iso)

    fired = 0
    for fire in result['fires']:
        rule = loiter_by_zone.get(fire['zone_id']) or {}
        try:
            _emit_loiter(camera_id, settings, fire, rule, now_iso)
            fired += 1
        except Exception as exc:  # noqa: BLE001 - one anomaly must not break the loop
            logger.warning('Loiter emit failed on %s: %s', camera_id, exc)
    return fired


def _emit_loiter(camera_id: str, settings: dict[str, Any], fire: dict[str, Any], rule: dict[str, Any], now_iso: str) -> None:
    zone_name = fire.get('zone_name') or 'zone'
    label = fire.get('label') or 'object'
    dwell = _as_float(fire.get('dwell'))
    event_id = _emit_behaviour_event(
        camera_id, settings, rule, now_iso,
        source='loiter', fire=fire, label=label, confidence=_as_float(fire.get('confidence')),
        rule_name=f'{zone_name} · {_rule_display(rule, "Loitering")}',
        message=f'{str(label).title()} loitering in {zone_name} ({int(round(dwell))}s)',
        details={
            'label': label,
            'track_id': fire.get('track_id'),
            'dwell_seconds': round(dwell, 1),
            'threshold_seconds': round(_as_float(fire.get('threshold')), 1),
        },
    )
    logger.info('Loiter on %s: %s in %s (%.0fs, event %s)', camera_id, label, zone_name, dwell, event_id)


def _camera_wall_clock(settings: Any, now: float) -> datetime:
    """The camera's own wall clock for an epoch instant.

    Hour-of-day learning and its day boundary are the SITE's local time: a
    camera stores an IANA ``timezone`` (``normalize_camera_settings``) and every
    other schedule in the app -- solar day/night windows, profile automation --
    is evaluated in it. Reading the host clock instead learned the wrong hour
    and rolled the day over at the wrong instant whenever the server ran in
    another zone, which also split one local day's activity across two commits
    and drifted the baseline. An absent or unresolvable zone keeps the previous
    host-clock behaviour (Windows images without the optional IANA tzdata).
    """
    name = str(settings.get('timezone') or '').strip() if isinstance(settings, dict) else ''
    if name.upper() == 'UTC':
        return datetime.fromtimestamp(now, tz=UTC)
    if name:
        try:
            return datetime.fromtimestamp(now, tz=ZoneInfo(name))
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return datetime.fromtimestamp(now)


# ─── Tier-2 behavioural intelligence: unusual time-of-day ────────────────────
# Per-(camera, zone, label) hour-of-day baselines learn which local hours a
# zone is normally active, and fire on activity in a normally-quiet hour. The
# in-progress "today" map and the learned baselines are guarded by one lock; the
# baseline map persists to the app_settings KV store (loaded once, flushed
# throttled). The pure decision lives in ``behaviour_baseline.time_of_day_step``.
_time_today: dict[str, dict[str, Any]] = {}
_time_baselines: dict[str, dict[str, Any]] = {}
_time_cooldowns: dict[str, float] = {}
_time_lock = threading.Lock()
_time_state: dict[str, Any] = {'loaded': False, 'dirty': False, 'last_flush': 0.0}
_TIME_BASELINES_KEY = 'behaviour_time_baselines'
_TIME_FLUSH_INTERVAL_SECONDS = 60.0


def _enabled_zones_with_time(settings: Any) -> list[dict[str, Any]]:
    zones = (settings.get('detection') or {}).get('zones', []) if isinstance(settings, dict) else []
    out: list[dict[str, Any]] = []
    for zone in zones or []:
        if not isinstance(zone, dict) or zone.get('enabled') is False:
            continue
        rule = zone.get('time_of_day')
        if isinstance(rule, dict) and rule.get('enabled') is not False:
            out.append(zone)
    return out


def _ensure_time_loaded_locked() -> None:
    if _time_state['loaded']:
        return
    try:
        raw = _state.database.get_setting(_TIME_BASELINES_KEY)
    except Exception as exc:  # noqa: BLE001 - a missing/locked DB just means "start fresh"
        # Retried next cycle, never marked loaded: see the loiter loader.
        logger.debug('Time-of-day baseline load failed: %s', exc)
        return
    _time_state['loaded'] = True
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                # commit_time_day coerces on read anyway; store as-is.
                _time_baselines[str(key)] = value


def _flush_time_baselines_locked(now: float, now_iso: str) -> None:
    if not _time_state['loaded']:
        # Never overwrite baselines that could not be read this run.
        return
    if not _time_state['dirty'] or (now - _time_state['last_flush']) < _TIME_FLUSH_INTERVAL_SECONDS:
        return
    try:
        _state.database.set_setting(_TIME_BASELINES_KEY, dict(_time_baselines), now_iso)
        _time_state['dirty'] = False
        _time_state['last_flush'] = now
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort; keep learning in memory
        logger.warning('Time-of-day baseline flush failed: %s', exc)


def emit_time_of_day_anomalies(camera_id: str, settings: dict[str, Any], detections: list[dict[str, Any]]) -> int:
    """Detect and emit unusual time-of-day anomalies for one detection cycle.

    Learns, per (zone, label), which local hours a zone is normally active, and
    fires when a tracked object appears in a normally-quiet hour. Returns the
    number of anomalies emitted. Best-effort and fully isolated.
    """
    zones = _enabled_zones_with_time(settings)
    if not zones:
        return 0
    from app.zone_detection import detection_matches_zone

    now = time.time()
    # "Time of day" is the CAMERA's wall clock, not the server's: the learned
    # hour bucket, the day boundary and the reported hour all follow the zone
    # its operator reads (see ``_camera_wall_clock``).
    local = _camera_wall_clock(settings, now)
    hour = local.hour
    day = local.toordinal()

    observations: list[dict[str, Any]] = []
    time_by_zone: dict[str, dict[str, Any]] = {}
    seen_keys: set[str] = set()
    for zone in zones:
        rule = zone.get('time_of_day') or {}
        zone_id = str(zone.get('id') or zone.get('name') or '')
        zone_name = str(zone.get('name') or zone.get('id') or '').strip() or None
        time_by_zone[zone_id] = rule
        wanted = {str(label).strip().lower() for label in (rule.get('labels') or []) if str(label).strip()}
        for det in detections or []:
            if not isinstance(det, dict) or det.get('track_id') is None:
                continue
            label = str(det.get('label') or '').strip().lower()
            if wanted and label not in wanted:
                continue
            if not detection_matches_zone(det, zone):
                continue
            key = f"{camera_id}|{zone_id}|{label}"
            if key in seen_keys:
                continue  # one observation per (zone, label) per cycle is enough
            seen_keys.add(key)
            try:
                confidence = float(det.get('confidence') or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            observations.append({
                'key': key,
                'zone_id': zone_id,
                'zone_name': zone_name,
                'label': label,
                'track_id': det.get('track_id'),
                'confidence': confidence,
                'day': day,
                'hour': hour,
                'threshold': rule.get('threshold', 0.15),
                'cooldown_seconds': rule.get('cooldown_seconds', 1800),
            })

    if not observations:
        return 0

    now_iso = datetime.now(UTC).isoformat()
    with _time_lock:
        _ensure_time_loaded_locked()
        result = behaviour_baseline.time_of_day_step(
            _time_today, _time_baselines, _time_cooldowns, observations, now,
        )
        if result['committed']:
            _time_state['dirty'] = True
        if len(_time_cooldowns) > 4096:
            cutoff = now - 86400
            for stale in [k for k, ts in _time_cooldowns.items() if ts < cutoff]:
                _time_cooldowns.pop(stale, None)
        _flush_time_baselines_locked(now, now_iso)

    fired = 0
    for fire in result['fires']:
        rule = time_by_zone.get(fire['zone_id']) or {}
        try:
            _emit_time(camera_id, settings, fire, rule, now_iso)
            fired += 1
        except Exception as exc:  # noqa: BLE001 - one anomaly must not break the loop
            logger.warning('Time-of-day emit failed on %s: %s', camera_id, exc)
    return fired


def _emit_time(camera_id: str, settings: dict[str, Any], fire: dict[str, Any], rule: dict[str, Any], now_iso: str) -> None:
    zone_name = fire.get('zone_name') or 'zone'
    label = fire.get('label') or 'object'
    try:
        hour = int(fire.get('hour'))
    except (TypeError, ValueError):
        hour = 0
    event_id = _emit_behaviour_event(
        camera_id, settings, rule, now_iso,
        source='time_of_day', fire=fire, label=label, confidence=_as_float(fire.get('confidence')),
        rule_name=f'{zone_name} · {_rule_display(rule, "Unusual Time")}',
        message=f'{str(label).title()} in {zone_name} at an unusual time ({hour:02d}:00)',
        details={
            'label': label,
            'track_id': fire.get('track_id'),
            'hour': hour,
            'probability': fire.get('probability'),
            'days_learned': fire.get('days'),
        },
    )
    logger.info('Unusual time on %s: %s in %s at %02d:00 (event %s)', camera_id, label, zone_name, hour, event_id)


# ─── Tier-2 behavioural intelligence: activity spike ─────────────────────────
# Per-(camera, zone, label, hour) count baselines learn the normal number of
# distinct objects an hour sees, and fire on an unusual burst. Same lock +
# KV-store persistence pattern as the other behaviour monitors; the pure
# decision lives in ``behaviour_baseline.activity_step``.
_activity_today: dict[str, dict[str, Any]] = {}
_activity_baselines: dict[str, dict[str, Any]] = {}
_activity_cooldowns: dict[str, float] = {}
_activity_lock = threading.Lock()
_activity_state: dict[str, Any] = {'loaded': False, 'dirty': False, 'last_flush': 0.0}
_ACTIVITY_BASELINES_KEY = 'behaviour_activity_baselines'
_ACTIVITY_FLUSH_INTERVAL_SECONDS = 60.0


def _enabled_zones_with_activity(settings: Any) -> list[dict[str, Any]]:
    zones = (settings.get('detection') or {}).get('zones', []) if isinstance(settings, dict) else []
    out: list[dict[str, Any]] = []
    for zone in zones or []:
        if not isinstance(zone, dict) or zone.get('enabled') is False:
            continue
        rule = zone.get('activity_spike')
        if isinstance(rule, dict) and rule.get('enabled') is not False:
            out.append(zone)
    return out


def _ensure_activity_loaded_locked() -> None:
    if _activity_state['loaded']:
        return
    try:
        raw = _state.database.get_setting(_ACTIVITY_BASELINES_KEY)
    except Exception as exc:  # noqa: BLE001 - a missing/locked DB just means "start fresh"
        # Retried next cycle, never marked loaded: see the loiter loader.
        logger.debug('Activity baseline load failed: %s', exc)
        return
    _activity_state['loaded'] = True
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                _activity_baselines[str(key)] = {
                    'count': max(0.0, float(value.get('count') or 0.0)),
                    'mean': max(0.0, float(value.get('mean') or 0.0)),
                    'm2': max(0.0, float(value.get('m2') or 0.0)),
                }
            except (TypeError, ValueError):
                continue


def _flush_activity_baselines_locked(now: float, now_iso: str) -> None:
    if not _activity_state['loaded']:
        # Never overwrite baselines that could not be read this run.
        return
    if not _activity_state['dirty'] or (now - _activity_state['last_flush']) < _ACTIVITY_FLUSH_INTERVAL_SECONDS:
        return
    try:
        _state.database.set_setting(_ACTIVITY_BASELINES_KEY, dict(_activity_baselines), now_iso)
        _activity_state['dirty'] = False
        _activity_state['last_flush'] = now
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        logger.warning('Activity baseline flush failed: %s', exc)


def emit_activity_spike_anomalies(camera_id: str, settings: dict[str, Any], detections: list[dict[str, Any]]) -> int:
    """Detect and emit activity-spike anomalies for one detection cycle.

    Learns, per (zone, label, hour), the normal number of distinct objects, and
    fires on an unusual burst. Returns the number of anomalies emitted.
    Best-effort and fully isolated.
    """
    zones = _enabled_zones_with_activity(settings)
    if not zones:
        return 0
    from app.zone_detection import detection_matches_zone

    now = time.time()
    # The learned hour bucket is the CAMERA's wall clock, like the unusual-time
    # monitor above (see ``_camera_wall_clock``).
    local = _camera_wall_clock(settings, now)
    hour = local.hour
    day = local.toordinal()

    observations: list[dict[str, Any]] = []
    activity_by_zone: dict[str, dict[str, Any]] = {}
    for zone in zones:
        rule = zone.get('activity_spike') or {}
        zone_id = str(zone.get('id') or zone.get('name') or '')
        zone_name = str(zone.get('name') or zone.get('id') or '').strip() or None
        activity_by_zone[zone_id] = rule
        wanted = {str(label).strip().lower() for label in (rule.get('labels') or []) if str(label).strip()}
        for det in detections or []:
            if not isinstance(det, dict) or det.get('track_id') is None:
                continue
            label = str(det.get('label') or '').strip().lower()
            if wanted and label not in wanted:
                continue
            if not detection_matches_zone(det, zone):
                continue
            try:
                confidence = float(det.get('confidence') or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            observations.append({
                'key': f"{camera_id}|{zone_id}|{label}",
                'baseline_key': f"{camera_id}|{zone_id}|{label}|{hour}",
                'zone_id': zone_id,
                'zone_name': zone_name,
                'label': label,
                'track_id': det.get('track_id'),
                'confidence': confidence,
                'day': day,
                'hour': hour,
                'min_count': rule.get('min_count', 5),
                'sensitivity': rule.get('sensitivity', 3.0),
                'cooldown_seconds': rule.get('cooldown_seconds', 900),
            })

    if not observations:
        return 0

    now_iso = datetime.now(UTC).isoformat()
    with _activity_lock:
        _ensure_activity_loaded_locked()
        result = behaviour_baseline.activity_step(
            _activity_today, _activity_baselines, _activity_cooldowns, observations, now,
        )
        if result['committed']:
            _activity_state['dirty'] = True
        if len(_activity_cooldowns) > 4096:
            cutoff = now - 86400
            for stale in [k for k, ts in _activity_cooldowns.items() if ts < cutoff]:
                _activity_cooldowns.pop(stale, None)
        _flush_activity_baselines_locked(now, now_iso)

    fired = 0
    for fire in result['fires']:
        rule = activity_by_zone.get(fire['zone_id']) or {}
        try:
            _emit_activity(camera_id, settings, fire, rule, now_iso)
            fired += 1
        except Exception as exc:  # noqa: BLE001 - one anomaly must not break the loop
            logger.warning('Activity-spike emit failed on %s: %s', camera_id, exc)
    return fired


def _emit_activity(camera_id: str, settings: dict[str, Any], fire: dict[str, Any], rule: dict[str, Any], now_iso: str) -> None:
    zone_name = fire.get('zone_name') or 'zone'
    label = fire.get('label') or 'object'
    try:
        count = int(fire.get('count') or 0)
    except (TypeError, ValueError):
        count = 0
    plural = 's' if label and not label.endswith('s') else ''
    event_id = _emit_behaviour_event(
        camera_id, settings, rule, now_iso,
        source='activity_spike', fire=fire, label=label, confidence=_as_float(fire.get('confidence')),
        rule_name=f'{zone_name} · {_rule_display(rule, "Activity Spike")}',
        message=f'Unusual activity in {zone_name}: {count} {str(label).title()}{plural} (normally ~{fire.get("mean")})',
        details={
            'label': label,
            'count': count,
            'threshold': round(_as_float(fire.get('threshold')), 2),
            'mean': fire.get('mean'),
            'hour': fire.get('hour'),
        },
    )
    logger.info('Activity spike on %s: %d %s in %s (event %s)', camera_id, count, label, zone_name, event_id)


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rule_display(rule: dict[str, Any], default: str) -> str:
    return str(rule.get('name') or default).strip() or default


def _emit_behaviour_event(
    camera_id: str,
    settings: dict[str, Any],
    rule: dict[str, Any],
    now_iso: str,
    *,
    source: str,
    fire: dict[str, Any],
    label: str,
    confidence: float,
    rule_name: str,
    message: str,
    details: dict[str, Any],
    alert_label: str | None = None,
) -> int:
    """Store one behaviour event, then record and notify per *rule*.

    Shared by the tripwire, co-presence, loiter, time-of-day and
    activity-spike monitors. *details* is the monitor-specific metadata; *fire*
    supplies the zone. *label* is the concrete detection the event records;
    *alert_label* (when given) is the text the alert-history row and the
    notification name the subject by -- a co-presence alert uses it to name
    every object that is together (``cat + person``) rather than one of them.
    """
    from app.alert_dispatch import _rule_notify_active_now, deliver_alert_notifications
    from app.utils import normalize_bool_setting, normalize_email_recipients, normalize_push_recipients

    email_enabled = normalize_bool_setting(rule.get('email_enabled'), False)
    push_enabled = normalize_bool_setting(rule.get('push_enabled'), False)
    camera_name = str((settings or {}).get('name') or '').strip() or None
    metadata = {
        'source': source,
        'camera_id': camera_id,
        'camera_name': camera_name,
        'zone_id': fire.get('zone_id'),
        'zone_name': fire.get('zone_name'),
        **details,
        'confidence': round(confidence, 3),
    }
    notify_rule = {
        'name': rule_name,
        'email_enabled': email_enabled,
        'push_enabled': push_enabled,
        'email_recipients': normalize_email_recipients(rule.get('email_recipients') or []),
        'push_recipients': normalize_push_recipients(rule.get('push_recipients') or []),
        'notify_start': str(rule.get('notify_start') or '').strip() or None,
        'notify_end': str(rule.get('notify_end') or '').strip() or None,
    }
    alert_label = str(alert_label or label)
    alert_active = (email_enabled or push_enabled) and _rule_notify_active_now(notify_rule)
    alerts = [{
        'created_at': now_iso, 'rule_name': rule_name, 'label': alert_label,
        'confidence': confidence, 'message': message,
    }] if alert_active else []
    event_id = _state.database.add_event_with_alerts(
        created_at=now_iso, source='behaviour', snapshot_path=None,
        detections=[], alerts=alerts, alert_triggered=alert_active,
        metadata=metadata,
    )

    if normalize_bool_setting(rule.get('record_on_detect'), True):
        _attach_recording(camera_id, settings, event_id, now_iso, label, confidence)

    if alert_active:
        alert_payload = {'rule_name': rule_name, 'label': alert_label, 'confidence': confidence, 'message': message}
        submit_alert_notification(deliver_alert_notifications, [alert_payload], event_id, [notify_rule])
    return event_id


def _attach_recording(camera_id: str, settings: dict[str, Any], event_id: int, now_iso: str, label: str, confidence: float) -> int | None:
    try:
        from app.recording_extension import attach_event_recording
        from app.utils import build_stream_url

        stream_url = build_stream_url(settings)
        if not stream_url:
            return None
        rec_config = _state.camera_event_recording_config(settings)
        _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=rec_config)
        detection = {'label': label, 'confidence': confidence, 'alert_triggered': True}
        return attach_event_recording(event_id, now_iso, 'rtsp', [detection], camera_id=camera_id, recording_config=rec_config)
    except Exception as exc:  # noqa: BLE001 - recording is best-effort
        logger.warning('Behaviour recording attach failed on %s: %s', camera_id, exc)
        return None
