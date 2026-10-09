"""User-managed object-label groups.

A *group* is a named umbrella label (e.g. ``animal``) that a zone allow-list
or object rule can reference to match ANY of its member detector labels
(``cat``, ``dog``, ...). The matching engine itself lives in
``app.zone_schema`` (``label_matches`` / ``detection_label_in_allowed``); this
module owns the group *definitions* -- the first-run defaults, validation, the
database persistence key, and the process-wide cache the detection hot path
reads.

The two original hard-coded groups (``animal`` / ``pet``) are seeded here as
defaults only. Once an operator edits the set from the Objects page, the whole
group map is persisted under the ``label_groups`` settings key and becomes the
single source of truth -- so the built-ins can be renamed, reshaped, or removed
like any other group. An absent / unset key falls back to the defaults, keeping
pre-existing ``animal`` / ``pet`` zone rules working on a fresh install.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import app.state as _state

logger = logging.getLogger('daygle.ai')

# The two original hard-coded groups, kept as the first-run defaults so a
# fresh install behaves exactly like the previous release.
DEFAULT_LABEL_GROUPS: dict[str, frozenset[str]] = {
    'animal': frozenset({
        'bird', 'cat', 'dog', 'horse', 'sheep', 'cow',
        'elephant', 'bear', 'zebra', 'giraffe',
    }),
    'pet': frozenset({'cat', 'dog'}),
}

# Bounds that keep a hand-edited / hostile group map from growing without limit.
_MAX_GROUPS = 64
_MAX_MEMBERS_PER_GROUP = 128

# Reserved configured labels that must never be used as a group name. ``motion``
# is a pixel-diff axis handled by a dedicated rule, not an object class.
_RESERVED_GROUP_NAMES: frozenset[str] = frozenset({'motion'})

# Process-wide cache for the detection hot path. ``label_matches`` runs once per
# detection per rule, so it must not pay a SQLite read on every call; the cache
# is lazily resolved and refreshed whenever the setting changes (or a database
# restore swaps the backing store).
_cache_lock: threading.Lock = threading.Lock()
_cache: dict[str, frozenset[str]] | None = None


def _canonical_token(value: Any) -> str:
    """Lowercase + strip a label token, returning ``''`` for junk input."""
    return str(value or '').strip().lower()


def normalize_label_groups(value: Any) -> dict[str, list[str]]:
    """Tolerantly canonicalize a raw ``label_groups`` setting dict.

    Returns ``{group_name: [member, ...]}`` with member lists sorted and
    de-duplicated. Invalid groups are dropped rather than raised -- this is the
    read-path normalizer that must survive a hand-edited DB row. Non-dict input
    returns ``{}`` (the router is the layer that reports precise errors to the
    operator on the write path).
    """
    if not isinstance(value, dict):
        return {}
    groups: dict[str, list[str]] = {}
    for raw_name, raw_members in value.items():
        name = _canonical_token(raw_name)
        # Group names appear inside comma-separated allow-lists, so they must
        # be a single token (no spaces/commas) and can't be the reserved motion
        # rule label.
        if not name or name in _RESERVED_GROUP_NAMES or ' ' in name or ',' in name:
            continue
        if len(groups) >= _MAX_GROUPS:
            break
        members = _normalize_members(raw_members, name)
        if members:
            groups[name] = members
    return groups


def _normalize_members(raw_members: Any, group_name: str) -> list[str]:
    """Canonicalize one group's member list (sorted, de-duplicated, non-empty)."""
    if isinstance(raw_members, str):
        raw_members = raw_members.split(',')
    if not isinstance(raw_members, (list, tuple, set, frozenset)):
        return []
    members: list[str] = []
    seen: set[str] = set()
    for raw in raw_members:
        member = _canonical_token(raw)
        # Skip empties, the reserved motion axis, and a self-reference (a group
        # cannot contain itself).
        if not member or member in _RESERVED_GROUP_NAMES or member == group_name:
            continue
        if member in seen:
            continue
        if len(members) >= _MAX_MEMBERS_PER_GROUP:
            break
        members.append(member)
        seen.add(member)
    return sorted(members)


def effective_label_groups() -> dict[str, frozenset[str]]:
    """Resolve the runtime group map (persisted setting, or defaults).

    An absent setting returns the built-in defaults; a *present* setting is
    authoritative even when empty, so removing every group persists correctly.
    """
    raw = None
    db = _state.database
    if db is not None:
        try:
            raw = db.get_setting('label_groups')
        except Exception:  # pragma: no cover - defensive; DB may be mid-startup
            logger.debug('Failed to read label_groups setting; using defaults.', exc_info=True)
            raw = None
    if raw is None:
        return {name: frozenset(members) for name, members in DEFAULT_LABEL_GROUPS.items()}
    return {
        name: frozenset(members)
        for name, members in normalize_label_groups(raw).items()
    }


def cached_label_groups() -> dict[str, frozenset[str]]:
    """The process-wide group map for the detection hot path.

    Lazily resolved on first use and refreshed via ``refresh_label_groups``
    whenever the setting changes or a database restore replaces the store, so
    ``label_matches`` never pays a per-detection SQLite read.
    """
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = effective_label_groups()
        return _cache


def refresh_label_groups() -> None:
    """Re-read the persisted group map into the cache (after a save / restore)."""
    global _cache
    with _cache_lock:
        _cache = effective_label_groups()


# Zone sub-rules that carry their own ``labels`` allow-list.
_ZONE_LABEL_RULES: tuple[str, ...] = ('tripwire', 'loiter', 'time_of_day', 'activity_spike')


def _rename_labels(labels: Any, renames: dict[str, str]) -> tuple[Any, bool]:
    """Swap renamed group names in a label list, keeping order and dropping duplicates."""
    if not isinstance(labels, list):
        return labels, False
    out: list[Any] = []
    changed = False
    for label in labels:
        new = renames.get(_canonical_token(label))
        if new is not None:
            changed = True
            label = new
        if label not in out:
            out.append(label)
    return out, changed


def rename_group_references(cameras: list[dict[str, Any]], renames: dict[str, str]) -> tuple[list[dict[str, Any]], bool]:
    """Point every camera's object labels, zone rules and zone sub-rules that name
    a renamed group at its new name.

    Returns ``(cameras, changed)``; the input list is not modified. Without this a
    renamed group would leave zone rules naming a group that no longer exists, so
    they would quietly stop matching anything.
    """
    renames = {_canonical_token(old): _canonical_token(new) for old, new in (renames or {}).items()}
    renames = {old: new for old, new in renames.items() if old and new and old != new}
    if not renames:
        return cameras, False
    changed = False
    result: list[dict[str, Any]] = []
    for camera in cameras:
        detection = camera.get('detection') if isinstance(camera, dict) else None
        if not isinstance(detection, dict):
            result.append(camera)
            continue
        detection = dict(detection)
        camera_changed = False
        if 'object_labels' in detection:
            detection['object_labels'], camera_changed = _rename_labels(detection['object_labels'], renames)
        zones = []
        for zone in detection.get('zones') or []:
            if not isinstance(zone, dict):
                zones.append(zone)
                continue
            zone = dict(zone)
            rules = []
            for rule in zone.get('object_rules') or []:
                new = renames.get(_canonical_token(rule.get('label'))) if isinstance(rule, dict) else None
                if new is not None:
                    rule = {**rule, 'label': new}
                    camera_changed = True
                rules.append(rule)
            if 'object_rules' in zone:
                zone['object_rules'] = rules
            if 'object_labels' in zone:
                zone['object_labels'], hit = _rename_labels(zone['object_labels'], renames)
                camera_changed = camera_changed or hit
            for key in _ZONE_LABEL_RULES:
                sub = zone.get(key)
                if isinstance(sub, dict) and 'labels' in sub:
                    labels, hit = _rename_labels(sub['labels'], renames)
                    if hit:
                        zone[key] = {**sub, 'labels': labels}
                        camera_changed = True
            zones.append(zone)
        if 'zones' in detection:
            detection['zones'] = zones
        if camera_changed:
            changed = True
            result.append({**camera, 'detection': detection})
        else:
            result.append(camera)
    return result, changed
