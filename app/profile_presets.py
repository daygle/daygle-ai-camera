"""Reusable camera day/night detection profile presets."""
from __future__ import annotations

import re
from typing import Any

from app.recording_settings import normalize_camera_detection_profiles

_PRESET_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_MAX_PRESETS = 100
_MAX_NAME_LENGTH = 80

# Keep the shipped preset aligned with the Cameras page's cat suggestion. The
# actual camera remains responsible for choosing Day/Night at runtime.
BUILTIN_PRESETS: tuple[dict[str, Any], ...] = (
    {
        'id': 'cat-small-animal',
        'name': 'Cat / Small Animal',
        'builtin': True,
        'day': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.35,
            'ingest_frame_fps': 6,
            'detection_confirm_frames': 2,
            'detection_confirm_window': 3,
            # Spatial-persistence IoU kept low: a small/distant cat walking moves
            # several box-widths per cycle, so consecutive boxes barely overlap.
            # A high value would fail exactly the small moving subject this
            # profile exists to catch; 0.05 still rejects noise that teleports
            # around the frame while the 2-of-3 label count carries the rest.
            'detection_confirm_iou': 0.05,
            'always_run_object_detection': True,
            # Region boost (motion-guided high-res crops) recovers small MOVING
            # cats without the whole-frame cost of tiling. Daytime tiling stays
            # off so a CPU host can sustain the 0.35s cadence; night keeps full
            # tiling below where IR shrinks distant cats and lighting is worst.
            'object_detection_region_boost': True,
            'object_detection_tiling': 'off',
            'periodic_scan_interval_seconds': 15,
            'motion_frame_width': 320,
            'motion_frame_height': 240,
            'motion_pixel_threshold': 25,
            'motion_gate_fraction': 0.003,
            'motion_scale_fraction': 0.025,
            'motion_background_alpha': 0.04,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'on',
        },
        'night': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.3,
            'ingest_frame_fps': 8,
            'detection_confirm_frames': 2,
            'detection_confirm_window': 3,
            'detection_confirm_iou': 0.05,
            'always_run_object_detection': True,
            'object_detection_region_boost': True,
            # IR frames make distant cats especially small, so night keeps
            # tiling on where day leaves it off. 2x2 (four passes) already ~2x
            # the pixels on a small cat over full-frame -- the bulk of the recall
            # win -- while 3x3 (nine passes) adds only marginal resolution at
            # >2x the GPU cost. On a thermally-marginal accelerator that extra
            # cost risks throttle/backlog, which drops whole frames and misses
            # moving cats outright -- a worse failure than slightly coarser
            # tiles. So night spends region boost + 2x2, not 3x3.
            'object_detection_tiling': '2x2',
            'periodic_scan_interval_seconds': 10,
            'motion_frame_width': 320,
            'motion_frame_height': 240,
            'motion_pixel_threshold': 45,
            'motion_gate_fraction': 0.002,
            'motion_scale_fraction': 0.02,
            'motion_background_alpha': 0.02,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'auto',
        },
    },
    {
        'id': 'balanced',
        'name': 'Balanced',
        'builtin': True,
        'day': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.5,
            'ingest_frame_fps': 4,
            'detection_confirm_frames': 1,
            'detection_confirm_window': 1,
            'detection_confirm_iou': 0,
            'always_run_object_detection': True,
            'object_detection_region_boost': False,
            'object_detection_tiling': 'off',
            'periodic_scan_interval_seconds': 0,
            'motion_pixel_threshold': 30,
            'motion_gate_fraction': 0.005,
            'motion_scale_fraction': 0.03,
            'motion_background_alpha': 0.05,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'on',
        },
        'night': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.5,
            'ingest_frame_fps': 4,
            'detection_confirm_frames': 1,
            'detection_confirm_window': 1,
            'detection_confirm_iou': 0,
            'always_run_object_detection': True,
            'object_detection_region_boost': True,
            'object_detection_tiling': 'off',
            'periodic_scan_interval_seconds': 15,
            'motion_pixel_threshold': 45,
            'motion_gate_fraction': 0.004,
            'motion_scale_fraction': 0.03,
            'motion_background_alpha': 0.03,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'auto',
        },
    },
    {
        'id': 'maximum-recall',
        'name': 'Maximum Recall',
        'builtin': True,
        'day': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.2,
            'ingest_frame_fps': 8,
            'detection_confirm_frames': 1,
            'detection_confirm_window': 1,
            'detection_confirm_iou': 0.05,
            'always_run_object_detection': True,
            'object_detection_region_boost': True,
            'object_detection_tiling': '2x2',
            'periodic_scan_interval_seconds': 10,
            'motion_pixel_threshold': 20,
            'motion_gate_fraction': 0.002,
            'motion_scale_fraction': 0.02,
            'motion_background_alpha': 0.04,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'on',
        },
        'night': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.2,
            'ingest_frame_fps': 10,
            'detection_confirm_frames': 1,
            'detection_confirm_window': 1,
            'detection_confirm_iou': 0,
            'always_run_object_detection': True,
            'object_detection_region_boost': True,
            'object_detection_tiling': '3x3',
            'periodic_scan_interval_seconds': 5,
            'motion_pixel_threshold': 35,
            'motion_gate_fraction': 0.0015,
            'motion_scale_fraction': 0.02,
            'motion_background_alpha': 0.02,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'auto',
        },
    },
    {
        'id': 'low-cpu',
        'name': 'Low CPU',
        'builtin': True,
        'day': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 1.0,
            'ingest_frame_fps': 2,
            'detection_confirm_frames': 2,
            'detection_confirm_window': 3,
            'detection_confirm_iou': 0,
            'always_run_object_detection': False,
            'object_detection_region_boost': False,
            'object_detection_tiling': 'off',
            'periodic_scan_interval_seconds': 0,
            'motion_pixel_threshold': 40,
            'motion_gate_fraction': 0.008,
            'motion_scale_fraction': 0.04,
            'motion_background_alpha': 0.05,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'on',
        },
        'night': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 1.5,
            'ingest_frame_fps': 2,
            'detection_confirm_frames': 2,
            'detection_confirm_window': 3,
            'detection_confirm_iou': 0,
            'always_run_object_detection': False,
            'object_detection_region_boost': False,
            'object_detection_tiling': 'off',
            'periodic_scan_interval_seconds': 0,
            'motion_pixel_threshold': 60,
            'motion_gate_fraction': 0.01,
            'motion_scale_fraction': 0.05,
            'motion_background_alpha': 0.03,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'auto',
        },
    },
    {
        'id': 'night-ir',
        'name': 'Night / IR',
        'builtin': True,
        'day': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.5,
            'ingest_frame_fps': 4,
            'detection_confirm_frames': 1,
            'detection_confirm_window': 1,
            'detection_confirm_iou': 0,
            'always_run_object_detection': True,
            'object_detection_region_boost': False,
            'object_detection_tiling': 'off',
            'periodic_scan_interval_seconds': 0,
            'motion_pixel_threshold': 30,
            'motion_gate_fraction': 0.005,
            'motion_scale_fraction': 0.03,
            'motion_background_alpha': 0.05,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'on',
        },
        'night': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.4,
            'ingest_frame_fps': 6,
            'detection_confirm_frames': 1,
            'detection_confirm_window': 1,
            'detection_confirm_iou': 0.05,
            'always_run_object_detection': True,
            'object_detection_region_boost': True,
            'object_detection_tiling': '2x2',
            'periodic_scan_interval_seconds': 10,
            'motion_pixel_threshold': 60,
            'motion_gate_fraction': 0.004,
            'motion_scale_fraction': 0.03,
            'motion_background_alpha': 0.01,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'auto',
        },
    },
    {
        'id': 'fast-motion',
        'name': 'Fast Motion',
        'builtin': True,
        'day': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.2,
            'ingest_frame_fps': 12,
            'detection_confirm_frames': 1,
            'detection_confirm_window': 1,
            'detection_confirm_iou': 0,
            'always_run_object_detection': True,
            'object_detection_region_boost': True,
            'object_detection_tiling': 'off',
            'periodic_scan_interval_seconds': 0,
            'motion_pixel_threshold': 25,
            'motion_gate_fraction': 0.004,
            'motion_scale_fraction': 0.025,
            'motion_background_alpha': 0.05,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'on',
        },
        'night': {
            'background_detection_enabled': True,
            'detection_interval_seconds': 0.25,
            'ingest_frame_fps': 12,
            'detection_confirm_frames': 1,
            'detection_confirm_window': 1,
            'detection_confirm_iou': 0,
            'always_run_object_detection': True,
            'object_detection_region_boost': True,
            'object_detection_tiling': '2x2',
            'periodic_scan_interval_seconds': 0,
            'motion_pixel_threshold': 50,
            'motion_gate_fraction': 0.003,
            'motion_scale_fraction': 0.025,
            'motion_background_alpha': 0.02,
            'motion_algorithm': 'mog2',
            'motion_denoise': True,
            'motion_shadow_suppression': 'auto',
        },
    },
)


def _copy_preset(preset: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': str(preset['id']),
        'name': str(preset['name']),
        'builtin': bool(preset.get('builtin', False)),
        'day': dict(preset.get('day') or {}),
        'night': dict(preset.get('night') or {}),
    }


def normalize_preset(raw: Any, *, preset_id: str | None = None, builtin: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError('Preset must be an object.')
    name = str(raw.get('name') or '').strip()
    if not name or len(name) > _MAX_NAME_LENGTH:
        raise ValueError(f'Preset name must be 1-{_MAX_NAME_LENGTH} characters.')
    resolved_id = str(preset_id or raw.get('id') or '').strip().lower()
    if not _PRESET_ID_RE.fullmatch(resolved_id):
        raise ValueError('Preset id must contain lowercase letters, numbers, hyphens, or underscores.')
    normalized = normalize_camera_detection_profiles({
        'day': raw.get('day') if isinstance(raw.get('day'), dict) else {},
        'night': raw.get('night') if isinstance(raw.get('night'), dict) else {},
    })
    return {
        'id': resolved_id,
        'name': name,
        'builtin': builtin,
        'day': normalized['day'],
        'night': normalized['night'],
    }


def _slugify(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.strip().lower()).strip('-')[:64]
    return slug or 'preset'


def list_presets(raw: Any) -> list[dict[str, Any]]:
    result = [_copy_preset(preset) for preset in BUILTIN_PRESETS]
    seen = {preset['id'] for preset in result}
    if isinstance(raw, list):
        for item in raw[:_MAX_PRESETS]:
            try:
                preset = normalize_preset(item, builtin=False)
            except ValueError:
                continue
            if preset['id'] in seen:
                continue
            result.append(preset)
            seen.add(preset['id'])
    return result


def create_preset(raw: Any, existing: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError('Preset must be an object.')
    name = str(raw.get('name') or '').strip()
    base_id = _slugify(name)
    used = {preset['id'] for preset in existing}
    preset_id = base_id
    suffix = 2
    while preset_id in used:
        suffix_text = f'-{suffix}'
        preset_id = f'{base_id[:64 - len(suffix_text)]}{suffix_text}'
        suffix += 1
    return normalize_preset(raw, preset_id=preset_id, builtin=False)


def get_preset(raw: Any, preset_id: str) -> dict[str, Any] | None:
    return next((preset for preset in list_presets(raw) if preset['id'] == preset_id), None)


def custom_presets(raw: Any) -> list[dict[str, Any]]:
    """Return only persisted custom presets, excluding shipped presets."""
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict) or item.get('builtin'):
            continue
        try:
            result.append(normalize_preset(item, builtin=False))
        except ValueError:
            continue
    return result
