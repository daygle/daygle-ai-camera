"""Recording / PTZ / sound setting helpers extracted from ``app/main.py`` (Phase-19).

The 4 helpers shipped here cluster around normalizing the *detection
sub-block* of a camera's settings payload -- specifically the
``recording``, ``ptz``, and ``sound`` sub-dicts that ``camera_config.normalize_camera_settings``
threads together with the ``detection`` defaults. They were originally
siblings on ``app/main.py`` and reach the same cross-cuts
(``main.normalize_bool_setting``, ``main.normalize_email_recipients``,
``main.SOUND_CLASSES``, ``main.DEFAULT_RULES``) as bare names; Phase-19
extracts them into this module while preserving identical behaviour,
using the **hybrid-pattern template** introduced in Phase-16
(``app/auth_gates.py``) and re-applied in Phase-17
(``app/config_facades.py``) and Phase-18 (``app/camera_config.py``):

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
``app/camera_config.py::normalize_camera_settings`` (Phase-18), which
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

from app.sound_detector import DEFAULT_RULES, SOUND_CLASSES
from app.utils import normalize_bool_setting, normalize_email_recipients, normalize_hhmm


CAMERA_MOTION_PROFILE_FIELDS = (
    'background_detection_enabled',
    'detection_interval_seconds',
    'ingest_frame_fps',
    'detection_confirm_frames',
    'detection_confirm_window',
    'detection_confirm_iou',
    'always_run_object_detection',
    'object_detection_region_boost',
    'object_detection_tiling',
    'object_detection_motion_mode',
    'periodic_scan_interval_seconds',
    'motion_frame_width',
    'motion_frame_height',
    'motion_pixel_threshold',
    'motion_gate_fraction',
    'motion_scale_fraction',
    'motion_background_alpha',
    'motion_algorithm',
    'motion_denoise',
    'motion_shadow_suppression',
)
_CAMERA_MOTION_PROFILE_MODES = frozenset({'day', 'night'})


def _normalize_profile_value(key: str, value: Any) -> int | float | str | bool | None:
    """Normalize an optional per-camera live/detection profile value."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        if key == 'detection_interval_seconds':
            return round(max(0.1, min(10.0, float(value))), 3)
        if key == 'ingest_frame_fps':
            return max(1, min(30, int(value)))
        if key in {'detection_confirm_frames'}:
            return max(1, min(10, int(value)))
        if key in {'detection_confirm_window'}:
            return max(1, min(30, int(value)))
        if key == 'detection_confirm_iou':
            return round(max(0.0, min(0.9, float(value))), 4)
        if key == 'periodic_scan_interval_seconds':
            return max(0, min(3600, int(value)))
        if key == 'motion_frame_width':
            return max(40, min(640, int(value)))
        if key == 'motion_frame_height':
            return max(30, min(480, int(value)))
        if key == 'motion_pixel_threshold':
            return max(1, min(255, int(value)))
        if key == 'motion_gate_fraction':
            return round(max(0.0001, min(0.5, float(value))), 6)
        if key == 'motion_scale_fraction':
            return round(max(0.001, min(1.0, float(value))), 6)
        if key == 'motion_background_alpha':
            return round(max(0.001, min(0.5, float(value))), 6)
    except (TypeError, ValueError):
        return None
    if key in {
        'background_detection_enabled', 'always_run_object_detection',
        'object_detection_region_boost', 'motion_denoise',
    }:
        return normalize_bool_setting(value, True)
    if key == 'object_detection_tiling':
        text = str(value).strip().lower()
        return text if text in {'off', '2x2', '3x3', '4x4'} else None
    if key == 'object_detection_motion_mode':
        # Per-profile override for the Objects moving/still default. ``None``
        # (unset / anything unrecognised) inherits the global Objects default so
        # existing cameras are unaffected; a profile that wants still subjects
        # counted (e.g. cats, which sit still constantly) ships ``any``.
        text = str(value).strip().lower()
        return text if text in {'any', 'moving', 'still'} else None
    if key == 'motion_algorithm':
        text = str(value).strip().lower()
        return text if text in {'mog2', 'diff'} else None
    if key == 'motion_shadow_suppression':
        text = str(value).strip().lower()
        return text if text in {'on', 'off', 'auto'} else None
    return None


def effective_camera_live_settings(
    camera: dict[str, Any],
    global_settings: dict[str, Any],
) -> dict[str, Any]:
    """Merge the active camera Day/Night profile over global live defaults."""
    merged = dict(global_settings)
    profiles = normalize_camera_detection_profiles(
        camera.get('detection_profiles'), camera,
    )
    active = profiles.get('active', 'day')
    merged.update(profiles.get(active, {}))
    merged['detection_profiles'] = profiles
    return merged


def _normalize_profile_motion_value(key: str, value: Any) -> int | float | str | bool | None:
    """Backward-compatible name for the expanded profile value normalizer."""
    return _normalize_profile_value(key, value)


def normalize_camera_detection_profiles(
    raw_profiles: Any,
    legacy_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the backward-compatible manual day/night profile structure.

    Older camera rows have flat ``motion_*`` overrides only. Those overrides are
    copied into both profiles and the active profile defaults to ``day``, so
    normalization preserves behavior exactly. New rows may set either profile
    field to ``None`` to inherit the global value.
    """
    legacy = legacy_settings if isinstance(legacy_settings, dict) else {}
    legacy_values = {
        key: _normalize_profile_motion_value(key, legacy.get(key))
        for key in CAMERA_MOTION_PROFILE_FIELDS
        if legacy.get(key) is not None
    }
    raw = raw_profiles if isinstance(raw_profiles, dict) else {}
    active = str(raw.get('active') or 'day').strip().lower()
    if active not in _CAMERA_MOTION_PROFILE_MODES:
        active = 'day'
    automation_source = str(raw.get('source') or 'manual').strip().lower()
    if automation_source not in {'manual', 'schedule', 'solar', 'onvif'}:
        automation_source = 'manual'
    day_start = normalize_hhmm(raw.get('day_start')) or '07:00'
    night_start = normalize_hhmm(raw.get('night_start')) or '19:00'
    preset_id = str(raw.get('preset_id') or '').strip().lower()
    if not preset_id or len(preset_id) > 64 or any(character not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for character in preset_id):
        preset_id = None
    has_profiles = any(isinstance(raw.get(mode), dict) for mode in _CAMERA_MOTION_PROFILE_MODES)
    profiles: dict[str, dict[str, Any]] = {}
    for mode in _CAMERA_MOTION_PROFILE_MODES:
        profile_source = raw.get(mode) if isinstance(raw.get(mode), dict) else None
        if profile_source is None and not has_profiles:
            profile_source = legacy_values
        profile_source = {**legacy_values, **(profile_source or {})}
        profiles[mode] = {
            key: _normalize_profile_motion_value(key, profile_source.get(key))
            for key in CAMERA_MOTION_PROFILE_FIELDS
            if profile_source.get(key) is not None and _normalize_profile_motion_value(key, profile_source.get(key)) is not None
        }
    result = {
        'active': active,
        'source': automation_source,
        'day_start': day_start,
        'night_start': night_start,
        'day': profiles['day'],
        'night': profiles['night'],
    }
    if preset_id:
        result['preset_id'] = preset_id
    return result


def apply_active_camera_detection_profile(settings: dict[str, Any]) -> dict[str, Any]:
    """Normalize profiles and project the selected profile onto legacy runtime keys."""
    legacy = dict(settings)
    profiles = normalize_camera_detection_profiles(settings.get('detection_profiles'), legacy)
    for key in CAMERA_MOTION_PROFILE_FIELDS:
        settings.pop(key, None)
    settings['detection_profiles'] = profiles
    settings.update(profiles[profiles['active']])
    return settings


def normalize_camera_recording_settings(settings: Any) -> dict[str, Any]:
    raw = settings if isinstance(settings, dict) else {}
    return {
        'continuous': normalize_bool_setting(raw.get('continuous'), False),
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

    def _float(value: Any, default: float, lo: float, hi: float) -> float:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return default
        if f != f:  # NaN guard
            return default
        return max(lo, min(hi, f))

    # ``step_duration`` is the ContinuousMove SOAP ``<Timeout>`` value in
    # seconds. The camera self-stops after this many seconds even if the
    # explicit /api/.../ptz ``stop`` command is dropped - addressing the
    # "press left, see nothing, press left again, camera pans too far"
    # failure mode by bounding every fresh send. The lower bound of 0.1s
    # filters out machine-gun sub-second moves that thrash cheap motor
    # controllers; the 5s upper bound keeps the safety net from
    # accidentally disabling the safety.
    step_duration = _float(raw.get('step_duration'), 0.4, 0.1, 5.0)

    return {
        'enabled': normalize_bool_setting(raw.get('enabled'), False),
        'protocol': protocol,
        'http_port': _int(raw.get('http_port'), 80, 1, 65535),
        'port': _int(raw.get('port'), 6060, 1, 65535),
        'address': _int(raw.get('address'), 1, 1, 255),
        'speed': _int(raw.get('speed'), 5, 1, 8),
        'step_duration': step_duration,
    }


def _normalize_camera_sound_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    enabled = normalize_bool_setting(raw.get('enabled'), False)
    raw_rules = raw.get('rules') if isinstance(raw.get('rules'), list) else []
    saved: dict[str, dict[str, Any]] = {}
    for r in raw_rules:
        if not isinstance(r, dict):
            continue
        cls = str(r.get('class') or '').strip()
        if cls in SOUND_CLASSES:
            saved[cls] = r
    defaults_by_class: dict[str, dict[str, Any]] = {
        d['class']: d for d in DEFAULT_RULES
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
            'name': str(r.get('name') or SOUND_CLASSES[cls]['label']),
            'enabled': normalize_bool_setting(r.get('enabled'), False),
            'record_on_detect': normalize_bool_setting(r.get('record_on_detect'), True),
            'confidence_threshold': threshold,
            'cooldown_seconds': cooldown,
            'email_enabled': normalize_bool_setting(r.get('email_enabled'), False),
            'email_recipients': normalize_email_recipients(r.get('email_recipients', [])),
            'push_enabled': normalize_bool_setting(r.get('push_enabled'), False),
            # Zero-pad the active/notify windows exactly like the zone-rule path
            # (app.zone_schema): both are compared lexically against the current
            # HH:MM, so a non-padded "9:00" (from a direct API call or restored
            # config) would otherwise evaluate the window wrong.
            'active_start': normalize_hhmm(r.get('active_start')),
            'active_end': normalize_hhmm(r.get('active_end')),
            'notify_start': normalize_hhmm(r.get('notify_start')),
            'notify_end': normalize_hhmm(r.get('notify_end')),
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
        enabled = normalize_bool_setting(legacy.get('enabled'), True)
    elif flat_enabled is not None:
        enabled = normalize_bool_setting(flat_enabled, True)
    if enabled:
        return
    for zone in detection.get('zones', []):
        zone['monitor_motion'] = False
        for rule in zone.get('object_rules', []):
            if str(rule.get('label') or '').strip().lower() == 'motion':
                rule['enabled'] = False
