"""Zone / schema normalization helpers extracted from ``app/main.py`` (Phase-21).

The 7 helpers shipped here (plus the ``_LABEL_ALIASES`` label-canonicalization
dict used by the public ``normalize_label_list`` helper) cluster around the
**monitoring-zone schema**: per-zone geometry (point / rectangle / bounds),
per-zone object-detection rules (label + per-rule min_confidence +
cooldown + e-mail / push notification enablement), and the multi-zone
orchestrator that bundles ``detection.zones`` back into the canonical shape.

Like the prior-cluster extractions (``app/auth_gates.py`` Phase-16,
``app/config_facades.py`` Phase-17, ``app/camera_config.py`` Phase-18,
``app/recording_settings.py`` Phase-19, ``app/ai_settings.py`` Phase-20),
these are extracted using the **hybrid-pattern template**:

- Cluster functions reach ``main.<attr>`` at *call time* (NOT import
  time) for their cross-module dependencies, so they continue to work
  seamlessly when ``app/main.py`` is partially loaded during the
  Pool A rebind loop.
- The Pool A from-import rebinds live at the TOP of ``app/main.py``
  (alphabetically sorted, in the existing rebind section) so that the
  eager-evaluation order at module load has ``main.<name>`` wired
  correctly before any sibling body references it as a bare name.

Cluster membership:

- ``_LABEL_ALIASES`` -- module-private dict (e.g. ``{'human': 'person'}``)
  used exclusively by ``normalize_label_list``. Moved alongside the
  helper to eliminate a cross-module Pool C reach; main.py's Pool A
  rebind keeps the 4 main.py-internal usages (e.g.
  ``detection_label_allowed_for_zone``, ``filter_detections_for_camera_zones``)
  resolving via a bare-name.

- ``normalize_label_list`` -- accepts a string (comma-separated), a
  list, or other (returns ``[]``). Splits, lowercases + strips, applies
  ``_LABEL_ALIASES`` for label canonicalization ("human" -> "person"),
  deduplicates, returns the sorted-unique label list.

- ``normalize_zone_object_rules`` -- per-zone object-detection rules
  builder. Reads ``zone.object_rules`` OR synthesizes one rule per
  label in ``zone.object_labels``. For each rule: parses min_confidence
  (float [0,1]) + cooldown_seconds (int >=0), composes the canonical
  shape with normalized en-/disable + e-mail / push flags + 4
  notification-window optional strings.

- ``normalize_zone_point`` -- single zone-coordinate sanitizer.
  Coerces ``{'x': fx, 'y': fy}`` into ``{'x': round(fx,4), 'y': round(fy,4)}``
  clamped to [0.0, 1.0]. Returns ``None`` on non-dict input or
  TypeError/ValueError.

- ``rectangle_zone_points`` -- trivial helper; produces 4-corner points
  for the rectangle case from a (x, y, width, height) tuple.

- ``zone_bounds`` -- inverse: bounding rect (left, top, right, bottom)
  from a list of points with a 0.01 minimum width / height.

- ``zone_motion_min_confidence`` -- reads the per-zone "motion" object
  rule; returns its min_confidence (clamped [0.0, 1.0]), or 0.45 when no
  motion rule exists. Used by ``detection_label_allowed_for_zone`` and
  ``filter_detections_for_camera_zones`` (both stay on main.py).

- ``normalize_monitoring_zones`` -- orchestrator. Walks the input zone
  list (non-list returns ``[]``); for each zone sanitizes x/y/width/
  height to [0.0, 1.0]; picks 3+ points from ``zone.points`` OR falls
  back to ``rectangle_zone_points`` (for the legacy rectangle-only
  schema); re-derives (x, y, width, height) via ``zone_bounds``;
  rebuilds ``object_rules`` via ``normalize_zone_object_rules``; if the
  legacy ``monitor_motion`` was true but no motion rule exists, inserts
  the motion rule; returns the canonical normalized zone list with
  ``monitor_motion`` / ``object_labels`` / ``object_rules`` derived.

Pool C reach sites (resolved via ``main.<attr>`` at call time):

- ``main.normalize_bool_setting`` (called 4-6 times in
  ``normalize_zone_object_rules``)
- ``main.normalize_email_recipients`` (called once in
  ``normalize_zone_object_rules``)
- ``main.normalize_camera_id`` (called once in
  ``normalize_monitoring_zones`` -- reached via Phase-18's
  camera_config rebind)
"""

from __future__ import annotations

from typing import Any

from app.camera_id import normalize_camera_id
from app.label_groups import cached_label_groups
from app.utils import normalize_bool_setting, normalize_email_recipients, normalize_hhmm


# ``HH:MM`` normalisation lives in ``app.utils`` (canonical home for the
# ``normalize_*`` settings helpers) so the zone-rule and sound-rule settings
# paths share one implementation. Kept under the original module-private name
# for the four call sites below.
_normalize_hhmm = normalize_hhmm


# Module-private label canonicalization: maps alternative spellings of the
# same detection label to a single canonical form. Used exclusively by
# ``normalize_label_list`` below. main.py keeps a Pool A rebind so its 4
# bare-name references (``detection_label_allowed_for_zone`` L799,
# ``filter_detections_for_camera_zones`` L815 + L911 + L919) still resolve
# to this dict after the Phase-21 splice.
_LABEL_ALIASES: dict[str, str] = {
    'human': 'person',
    'people': 'person',
    'pedestrian': 'person',
}


# Umbrella / group labels: a single configurable label that matches ANY of a
# set of concrete detector labels. Unlike ``_LABEL_ALIASES`` (a one-to-one
# canonicalization that rewrites a label to its single canonical form), a group
# is one-to-many *expansion* used only when the label appears on the CONFIGURED
# side (a zone's ``object_labels`` allow-list or an object rule's label).
#
# This keeps every individual label working exactly as before: a rule or
# allow-list for ``cat`` still matches only ``cat``. A user who instead
# configures ``animal`` (or ``pet``) opts into the umbrella -- e.g. at night an
# IR-lit cat is frequently misclassified as ``dog``, so an ``animal``/``pet``
# rule still fires.
#
# The group *definitions* are user-managed and live in ``app.label_groups``
# (``animal`` / ``pet`` are only first-run defaults). The two matching helpers
# below read them through ``cached_label_groups()``, so an operator-created
# group works exactly like the built-ins without touching this module.


def _optional_fraction(value: Any, low: float, high: float) -> float | None:
    """Coerce ``value`` to a float clamped to ``[low, high]``, or ``None``.

    ``None`` / ``''`` / non-numeric input all resolve to ``None`` ("inherit the
    camera/global value") so an unset per-zone override never forces a value.
    Used for the optional per-zone motion ``gate_fraction`` / ``scale_fraction``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return round(max(low, min(high, float(value))), 6)
    except (TypeError, ValueError):
        return None


def canonical_label(value: Any) -> str:
    """Lowercase, strip, and apply ``_LABEL_ALIASES`` to a single label."""
    text = str(value or '').strip().lower()
    return _LABEL_ALIASES.get(text, text)


def label_matches(detection_label: Any, configured_label: Any) -> bool:
    """Return True when a detection label satisfies a configured label.

    ``detection_label`` is what the detector produced (always a concrete label
    such as ``cat``); ``configured_label`` is what an operator set on a zone
    allow-list or object rule and MAY be an umbrella group name (``animal`` /
    ``pet``). The match succeeds when the two canonicalize to the same label OR
    the configured label is a group whose members include the detection label.
    Group expansion is intentionally one-directional -- a rule for ``cat`` never
    matches ``dog`` -- so non-group labels behave exactly as before.
    """
    detection = canonical_label(detection_label)
    configured = canonical_label(configured_label)
    if not detection or not configured:
        return False
    if detection == configured:
        return True
    members = cached_label_groups().get(configured)
    return bool(members and detection in members)


def detection_label_in_allowed(detection_label: Any, allowed_labels: set[str]) -> bool:
    """Return True when a detection label is covered by an allow-list.

    ``allowed_labels`` is a set of already-canonicalized configured labels
    (from ``normalize_label_list``) that may contain umbrella group names. A
    detection matches when its canonical label is listed directly or falls
    inside any configured group's member set.
    """
    detection = canonical_label(detection_label)
    if not detection:
        return False
    if detection in allowed_labels:
        return True
    groups = cached_label_groups()
    return any(
        detection in members
        for members in (groups.get(label) for label in allowed_labels)
        if members
    )


def normalize_label_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_labels = value.split(',')
    elif isinstance(value, list):
        raw_labels = value
    else:
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for raw_label in raw_labels:
        label = _LABEL_ALIASES.get(
            str(raw_label).strip().lower(),
            str(raw_label).strip().lower(),
        )
        if label and label not in seen:
            labels.append(label)
            seen.add(label)
    return labels


def normalize_zone_object_rules(zone: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rules = zone.get('object_rules')
    if isinstance(raw_rules, list):
        source_rules = raw_rules
    else:
        source_rules = [
            {'label': label}
            for label in normalize_label_list(zone.get('object_labels', []))
        ]
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in source_rules:
        if not isinstance(rule, dict):
            continue
        labels = normalize_label_list(rule.get('label') or '')
        if not labels:
            continue
        label = labels[0]
        if label in seen:
            continue
        seen.add(label)
        # Motion and faces are non-object-class axes, so their canonical
        # confidence default is 0.45 (matching zone_motion_min_confidence /
        # the global Face Confidence default / the frontend's defaultObjectRule)
        # rather than the 0.5 object-class default. Keeping the defaults
        # distinct matters: a persisted rule that omits min_confidence must
        # gate at the same value the detection / recording / alerting axes
        # fall back to, or those axes silently disagree about what fires.
        non_object_default = 0.45 if label in ('motion', 'face') else 0.5
        try:
            min_confidence = float(rule.get('min_confidence', non_object_default))
        except (TypeError, ValueError):
            min_confidence = non_object_default
        try:
            cooldown_seconds = int(rule.get('cooldown_seconds', 60))
        except (TypeError, ValueError):
            cooldown_seconds = 60
        min_confidence = max(0.0, min(1.0, min_confidence))
        # ``max_confidence`` is the inclusive upper bound of the per-rule
        # confidence window: a detection fires the rule only when
        # ``min_confidence <= confidence <= max_confidence``. It defaults to
        # 1.0 (no upper limit) so pre-existing rules keep their behavior. A
        # value below ``min_confidence`` would define an empty window that can
        # never match, so we clamp it up to ``min_confidence`` -- the window is
        # always non-empty and ``max`` is never below ``min``.
        try:
            max_confidence = float(rule.get('max_confidence', 1.0))
        except (TypeError, ValueError):
            max_confidence = 1.0
        max_confidence = max(min_confidence, min(1.0, max_confidence))
        # Optional per-zone motion sensitivity overrides. When set on a motion
        # rule they replace the camera/global gate + scale for THIS zone only, so
        # a sensitive doorway and a noisy tree-line can coexist on one camera.
        # ``None`` means "inherit". Clamped to the same ranges the global live
        # settings enforce. Ignored for non-motion (object) rules.
        gate_fraction = _optional_fraction(
            rule.get('gate_fraction'), 0.0001, 0.5,
        ) if label == 'motion' else None
        scale_fraction = _optional_fraction(
            rule.get('scale_fraction'), 0.001, 1.0,
        ) if label == 'motion' else None
        rules.append({
            'label': label,
            'enabled': normalize_bool_setting(rule.get('enabled'), True),
            'record_on_detect': normalize_bool_setting(rule.get('record_on_detect'), True),
            'min_confidence': min_confidence,
            'max_confidence': max_confidence,
            'gate_fraction': gate_fraction,
            'scale_fraction': scale_fraction,
            'cooldown_seconds': max(0, cooldown_seconds),
            'email_enabled': normalize_bool_setting(rule.get('email_enabled'), False),
            'email_recipients': normalize_email_recipients(rule.get('email_recipients', [])),
            'active_start': _normalize_hhmm(rule.get('active_start')),
            'active_end': _normalize_hhmm(rule.get('active_end')),
            'notify_start': _normalize_hhmm(rule.get('notify_start')),
            'notify_end': _normalize_hhmm(rule.get('notify_end')),
            'push_enabled': normalize_bool_setting(rule.get('push_enabled'), False),
        })
    return rules


def normalize_zone_point(point: Any) -> dict[str, float] | None:
    if not isinstance(point, dict):
        return None
    try:
        x = max(0.0, min(1.0, float(point.get('x') or 0)))
        y = max(0.0, min(1.0, float(point.get('y') or 0)))
    except (TypeError, ValueError):
        return None
    return {'x': round(x, 4), 'y': round(y, 4)}


def rectangle_zone_points(
    x: float,
    y: float,
    width: float,
    height: float,
) -> list[dict[str, float]]:
    return [
        {'x': round(x, 4), 'y': round(y, 4)},
        {'x': round(x + width, 4), 'y': round(y, 4)},
        {'x': round(x + width, 4), 'y': round(y + height, 4)},
        {'x': round(x, 4), 'y': round(y + height, 4)},
    ]


def zone_bounds(points: list[dict[str, float]]) -> tuple[float, float, float, float]:
    xs = [point['x'] for point in points]
    ys = [point['y'] for point in points]
    left = min(xs)
    top = min(ys)
    right = max(xs)
    bottom = max(ys)
    return (left, top, max(0.01, right - left), max(0.01, bottom - top))


def zone_motion_min_confidence(zone: dict[str, Any]) -> float:
    for rule in zone.get('object_rules', []):
        if (
            str(rule.get('label') or '').strip().lower() == 'motion'
            and rule.get('enabled', True)
        ):
            try:
                return max(0.0, min(1.0, float(rule.get('min_confidence', 0.45))))
            except (TypeError, ValueError):
                return 0.45
    return 0.45


def zone_motion_gate_fraction(zone: dict[str, Any]) -> float | None:
    """Return the enabled motion rule's per-zone ``gate_fraction`` override, or
    ``None`` when the zone inherits the camera/global gate."""
    for rule in zone.get('object_rules', []):
        if (
            str(rule.get('label') or '').strip().lower() == 'motion'
            and rule.get('enabled', True)
        ):
            return _optional_fraction(rule.get('gate_fraction'), 0.0001, 0.5)
    return None


def zone_motion_scale_fraction(zone: dict[str, Any]) -> float | None:
    """Return the enabled motion rule's per-zone ``scale_fraction`` override, or
    ``None`` when the zone inherits the camera/global scale."""
    for rule in zone.get('object_rules', []):
        if (
            str(rule.get('label') or '').strip().lower() == 'motion'
            and rule.get('enabled', True)
        ):
            return _optional_fraction(rule.get('scale_fraction'), 0.001, 1.0)
    return None


def zone_motion_max_confidence(zone: dict[str, Any]) -> float:
    """Return the enabled motion rule's ``max_confidence`` upper bound.

    Mirrors ``zone_motion_min_confidence`` so a zone's motion detections are
    gated by the SAME ``[min, max]`` window as its object rules. Defaults to
    1.0 (no upper limit) when no enabled motion rule exists or the value is
    absent/invalid, so pre-existing zones keep firing on any motion at or
    above their min threshold.
    """
    for rule in zone.get('object_rules', []):
        if (
            str(rule.get('label') or '').strip().lower() == 'motion'
            and rule.get('enabled', True)
        ):
            try:
                return max(0.0, min(1.0, float(rule.get('max_confidence', 1.0))))
            except (TypeError, ValueError):
                return 1.0
    return 1.0


def normalize_monitoring_zones(zones: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(zones, list):
        return normalized
    for index, zone in enumerate(zones, start=1):
        if not isinstance(zone, dict):
            continue
        try:
            x = max(0.0, min(1.0, float(zone.get('x') or 0)))
        except (TypeError, ValueError):
            x = 0.0
        try:
            y = max(0.0, min(1.0, float(zone.get('y') or 0)))
        except (TypeError, ValueError):
            y = 0.0
        try:
            width = max(0.01, min(1.0 - x, float(zone.get('width') or 0)))
        except (TypeError, ValueError):
            width = 0.01
        try:
            height = max(0.01, min(1.0 - y, float(zone.get('height') or 0)))
        except (TypeError, ValueError):
            height = 0.01
        points = [
            point
            for point in (
                normalize_zone_point(point) for point in zone.get('points') or []
            )
            if point is not None
        ]
        if len(points) < 3:
            points = rectangle_zone_points(x, y, width, height)
        x, y, width, height = zone_bounds(points)
        object_rules = normalize_zone_object_rules(zone)
        had_monitor_motion = 'monitor_motion' in zone and bool(zone['monitor_motion'])
        if had_monitor_motion and (not any(
            str(r.get('label') or '').strip().lower() == 'motion'
            for r in object_rules
        )):
            object_rules.insert(0, {
                'label': 'motion',
                'enabled': True,
                'record_on_detect': True,
                'min_confidence': 0.45,
                'max_confidence': 1.0,
                'gate_fraction': None,
                'scale_fraction': None,
                'cooldown_seconds': 60,
                'email_enabled': False,
                'email_recipients': [],
                'active_start': None,
                'active_end': None,
                'notify_start': None,
                'notify_end': None,
                'push_enabled': False,
            })
        monitor_motion = any(
            str(r.get('label') or '').strip().lower() == 'motion'
            and r.get('enabled', True)
            for r in object_rules
        )
        normalized.append({
            'id': normalize_camera_id(zone.get('id'), f'zone-{index}'),
            'name': str(zone.get('name') or f'Zone {index}').strip() or f'Zone {index}',
            'x': round(x, 4),
            'y': round(y, 4),
            'width': round(width, 4),
            'height': round(height, 4),
            'points': points,
            'enabled': bool(zone.get('enabled', True)),
            'monitor_motion': monitor_motion,
            'monitor_objects': bool(zone.get('monitor_objects', True)),
            # ``object_labels`` feeds OBJECT allow-lists only -- motion and
            # face are their own detection axes, not object classes, so they
            # never belong in the allow-list derivation.
            'object_labels': [
                rule['label']
                for rule in object_rules
                if str(rule.get('label') or '').strip().lower() not in ('motion', 'face')
            ],
            # Faces axis flag: True when the zone carries an enabled ``face``
            # rule. Consumed by the live pipeline to scope face processing to
            # these zones (faces detected elsewhere are dropped BEFORE the
            # expensive recognition pass).
            'monitor_faces': any(
                str(r.get('label') or '').strip().lower() == 'face'
                and r.get('enabled', True)
                for r in object_rules
            ),
            'object_rules': object_rules,
        })
    return normalized
