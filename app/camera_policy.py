"""Compiled per-camera detection policy.

Policy construction is deliberately separate from matching so the expensive
normalization/geometry work happens once per settings object. The hot path can
then use normalized label sets, zone bounds, and enabled rules without rebuilding
those structures for every detection.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any

from app.zone_schema import normalize_label_list


@dataclass(frozen=True)
class CompiledZone:
    zone: dict[str, Any]
    bounds: tuple[float, float, float, float]
    object_labels: frozenset[str]
    rules: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CameraPolicy:
    settings_id: int
    camera_labels: frozenset[str]
    zones: tuple[CompiledZone, ...]
    object_zones: tuple[CompiledZone, ...]
    face_zones: tuple[CompiledZone, ...]
    motion_zones: tuple[CompiledZone, ...]

    @classmethod
    def compile(cls, settings: dict[str, Any]) -> "CameraPolicy":
        detection = settings.get("detection") or {}
        camera_labels = frozenset(normalize_label_list(detection.get("object_labels", [])))
        compiled: list[CompiledZone] = []
        for raw_zone in detection.get("zones", []) or []:
            if not isinstance(raw_zone, dict) or not raw_zone.get("enabled", True):
                continue
            points = raw_zone.get("points") or []
            try:
                if len(points) >= 3:
                    xs = [float(point.get("x", 0.0)) for point in points]
                    ys = [float(point.get("y", 0.0)) for point in points]
                    left, top = min(xs), min(ys)
                    bounds = (left, top, max(0.01, max(xs) - left), max(0.01, max(ys) - top))
                else:
                    left, top = float(raw_zone.get("x", 0.0)), float(raw_zone.get("y", 0.0))
                    bounds = (left, top, max(0.01, float(raw_zone.get("width", 1.0))), max(0.01, float(raw_zone.get("height", 1.0))))
            except (TypeError, ValueError):
                left, top = 0.0, 0.0
                bounds = (0.0, 0.0, 1.0, 1.0)
            rules = tuple(rule for rule in raw_zone.get("object_rules", []) or [] if isinstance(rule, dict) and rule.get("enabled", True))
            compiled.append(CompiledZone(raw_zone, bounds, frozenset(normalize_label_list(raw_zone.get("object_labels", []))), rules))
        object_zones = tuple(zone for zone in compiled if zone.zone.get("monitor_objects", True))
        face_zones = tuple(zone for zone in compiled if any(str(rule.get("label", "")).lower() == "face" for rule in zone.rules))
        motion_zones = tuple(zone for zone in compiled if zone.zone.get("monitor_motion", True))
        return cls(id(settings), camera_labels, tuple(compiled), object_zones, face_zones, motion_zones)


_cache: dict[int, tuple[dict[str, Any], CameraPolicy]] = {}
_cache_lock = threading.RLock()


def camera_policy(settings: dict[str, Any]) -> CameraPolicy:
    key = id(settings)
    with _cache_lock:
        cached = _cache.get(key)
        # Retain the settings object beside its compiled policy. This keeps
        # ``id(settings)`` from being reused by a later object while the entry
        # is live; the cache remains explicitly bounded.
        if cached is not None and cached[0] is settings:
            return cached[1]
        policy = CameraPolicy.compile(settings)
        _cache[key] = (settings, policy)
        if len(_cache) > 256:
            _cache.pop(next(iter(_cache)))
        return policy


def clear_camera_policy_cache() -> None:
    with _cache_lock:
        _cache.clear()
