"""Recording / PTZ / sound setting helpers extracted from ``app/main.py`` (Phase 19).

The 4 helpers shipped here cluster around normalizing the *detection
sub-block* of a camera's settings payload -- specifically the
``recording``, ``ptz``, and ``sound`` sub-dicts that ``camera_config.normalize_camera_settings``
threads together with the ``detection`` defaults. They were originally
siblings on ``app/main.py`` and reach the same cross-cuts
(``main.normalize_bool_setting``, ``main.normalize_email_recipients``,
``main.SOUND_CLASSES``, ``main.DEFAULT_RULES``) as bare names; Phase 19
extracts them into this module while preserving identical behaviour,
using the **hybrid-pattern template** introduced in Phase 16
(``app/auth_gates.py``) and re-applied in Phase 17
(``app/config_facades.py``) and Phase 18 (``app/camera_config.py``):

- Cluster functions reach ``main.<attr>`` at *call time* (NOT import
  time) for their cross-module dependencies, so they continue to work
  seamlessly when ``app/main.py`` is partially loaded during the
  Pool A rebind loop.
- The Pool A from-import rebinds live at the TOP of ``app/main.py``
  (alphabetically sorted, right after the existing camera_config +
  config_facades rebind blocks) so that the eager-evaluation order at
  module load has ``main.<name>`` wired correctly before any sibling
  body evaluates.

Cluster membership:

- ``normalize_camera_recording_settings`` -- single-key normaliser
  (``continuous``) used by ``normalize_camera_settings`` to coerce
  ``settings['recording']`` into a stable schema.
- ``normalize_camera_ptz_settings`` -- PTZ protocol + 4 integer field
  clampers (``http_port``, ``port``, ``address``, ``speed``) with
  nested ``_int`` helper for range-bounded coercion.
- ``_normalize_camera_sound_settings`` -- the heaviest helper in the
  cluster (28 lines); rebuilds a per-class rule list from raw sound
  config, interpolating the SOUND_CLASSES / DEFAULT_RULES constants
  from ``app.sound_detector`` re-exposed via ``main`` and applying
  confidence_threshold + cooldown_seconds clamping + email / push
  notification flags.
- ``_migrate_legacy_camera_motion`` -- folds the removed camera-level
  motion master switch (``detection.motion`` dict and ``motion_enabled``
  flat field) into each zone's per-zone ``monitor_motion`` /
  ``object_rules[label='motion'].enabled`` settings so motion stays
  off across the upgrade.

These helpers are reached almost exclusively via
``app/camera_config.py::normalize_camera_settings`` (Phase 18), which
already wires the calls through ``main.<attr>`` to defeat the
circular-import gate. The only other internal callers in ``app/main.py``
are ``camera_event_recording_config`` (the ``continuous`` flag
during event recording orchestration) and ``validate_camera_settings``
(the post-normalization shape validator) -- both inside function
bodies, so the top-of-file Pool A rebind fires before any of them
evaluates.
"""

from __future__ import annotations

from typing import Any

import app.main as main


def normalize_camera_recording_settings(settings: Any) -> dict[str, Any]:
    raw = settings if isinstance(settings, dict) else {}
    return {
        'continuous': main.normalize_bool_setting(raw.get('continuous'), False),
    }


def normalize_camera_ptz_settings(settings: Any) -> dict[str, Any]:
    raw = settings if isinstance(settings, dict) else {}
    protocol = str(raw.get('protocol') or 'onvif').strip().lower()
    if protocol not in {'onvif', 'tcp_pelcod'}:
        protocol = 'onvif'

    def _int(value: Any, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(value or default)))
        except (TypeError, ValueError):
            return default

    return {
        'enabled': main.normalize_bool_setting(raw.get('enabled'), False),
        'protocol': protocol,
        'http_port': _int(raw.get('http_port'), 80, 1, 65535),
        'port': _int(raw.get('port'), 6060, 1, 65535),
        'address': _int(raw.get('address'), 1, 1, 255),
        'speed': _int(raw.get('speed'), 5, 1, 8),
    }


def _normalize_camera_sound_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    enabled = main.normalize_bool_setting(raw.get('enabled'), False)
    raw_rules = raw.get('rules') if isinstance(raw.get('rules'), list) else []
    saved: dict[str, dict[str, Any]] = {}
    for r in raw_rules:
        if not isinstance(r, dict):
            continue
        cls = str(r.get('class') or '').strip()
        if cls in main.SOUND_CLASSES:
            saved[cls] = r
    defaults_by_class: dict[str, dict[str, Any]] = {
        d['class']: d for d in main.DEFAULT_RULES
    }
    rules = []
    for cls, r in saved.items():
        default = defaults_by_class.get(cls)
        if not default:
            continue
        try:
            threshold = max(
                0.1,
                min(1.0, float(r.get('confidence_threshold', default['confidence_threshold']))),
            )
        except (TypeError, ValueError):
            threshold = default['confidence_threshold']
        try:
            cooldown = max(5.0, float(r.get('cooldown_seconds', default['cooldown_seconds'])))
        except (TypeError, ValueError):
            cooldown = float(default['cooldown_seconds'])
        rules.append({
            'class': cls,
            'name': str(r.get('name') or main.SOUND_CLASSES[cls]['label']),
            'enabled': main.normalize_bool_setting(r.get('enabled'), False),
            'record_on_detect': main.normalize_bool_setting(r.get('record_on_detect'), True),
            'confidence_threshold': threshold,
            'cooldown_seconds': cooldown,
            'email_enabled': main.normalize_bool_setting(r.get('email_enabled'), False),
            'email_recipients': main.normalize_email_recipients(r.get('email_recipients', [])),
            'push_enabled': main.normalize_bool_setting(r.get('push_enabled'), False),
            'active_start': str(r.get('active_start') or '').strip() or None,
            'active_end': str(r.get('active_end') or '').strip() or None,
            'notify_start': str(r.get('notify_start') or '').strip() or None,
            'notify_end': str(r.get('notify_end') or '').strip() or None,
        })
    return {'enabled': enabled, 'rules': rules}


def _migrate_legacy_camera_motion(detection: dict[str, Any]) -> None:
    """Fold the removed camera-level motion master switch into each
    zone's motion rule, then drop the legacy fields.

    Motion is configured per zone via each zone's ``motion`` object
    rule; there is no camera-level motion setting any more. If a stored
    config still has the old camera-level switch turned off (either
    the short-lived ``detection.motion.enabled`` dict or the older
    flat ``motion_enabled`` field), disable the motion rule in every
    zone so motion stays off after the upgrade. The legacy
    record/email/push flags are dropped: the zone rule's own
    checkboxes are the single source of truth.
    """
    legacy = detection.pop('motion', None)
    flat_enabled = detection.pop('motion_enabled', None)
    detection.pop('motion_email_enabled', None)
    enabled = True
    if isinstance(legacy, dict):
        enabled = main.normalize_bool_setting(legacy.get('enabled'), True)
    elif flat_enabled is not None:
        enabled = main.normalize_bool_setting(flat_enabled, True)
    if enabled:
        return
    for zone in detection.get('zones', []):
        zone['monitor_motion'] = False
        for rule in zone.get('object_rules', []):
            if str(rule.get('label') or '').strip().lower() == 'motion':
                rule['enabled'] = False
