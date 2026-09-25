"""Versioned runtime configuration snapshots.

The settings repository exposes a monotonically increasing cache generation.  Facades
use that generation to rebuild their normalized dictionaries only after an operator
write, while still returning defensive copies to preserve the historical API contract.
"""
from __future__ import annotations

import copy
import threading
from typing import Any, Callable

_lock = threading.RLock()
_canonical: dict[tuple[int, int, str], Any] = {}
_owners: dict[tuple[int, int, str], Any] = {}


def settings_generation(database: Any) -> int | None:
    value = getattr(database, "_settings_cache_gen", None) if database is not None else None
    return int(value) if isinstance(value, int) else None


def cached_snapshot(database: Any, name: str, builder: Callable[[], Any]) -> Any:
    """Return a defensive copy of a cached normalized settings snapshot.

    Test/embedding database doubles without the generation attribute deliberately
    bypass the cache, which keeps their mutation-based fixtures deterministic.
    """
    generation = settings_generation(database)
    if generation is None:
        return copy.deepcopy(builder())
    key = (id(database), generation, name)
    with _lock:
        if key not in _canonical or _owners.get(key) is not database:
            _canonical[key] = copy.deepcopy(builder())
            # Retain the owner alongside the snapshot so a recycled object id
            # cannot alias a previous database instance's configuration.
            _owners[key] = database
            # A settings write can invalidate many facades; retaining old
            # generations would retain old camera settings indefinitely.
            stale = [entry for entry in _canonical if entry[1] != generation]
            for entry in stale:
                _canonical.pop(entry, None)
                _owners.pop(entry, None)
        return copy.deepcopy(_canonical[key])


def clear_runtime_config_cache() -> None:
    with _lock:
        _canonical.clear()
        _owners.clear()
