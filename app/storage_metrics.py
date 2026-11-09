"""Disk and storage usage for the System page and the Android app.

Two kinds of numbers, read in two different ways:

* **Volumes** - size, used and free space of each filesystem that holds the
  server's data (recordings, snapshots, events, database). Read with
  ``statvfs``, which is a cheap syscall, so they are always live. Folders
  that share a filesystem are reported once, with every label that lives on
  it.
* **Usage** - how many bytes the recordings, snapshots, events and database
  take up. That means walking the directory trees, which can be slow on a
  large install, so the result is cached for ``USAGE_TTL_SECONDS`` and
  computed by one request at a time.

Like ``app.system_metrics``, every reader is best-effort: a missing folder,
a permission error or a platform without ``statvfs`` yields ``None`` or a
partial result instead of failing the request.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

# Directory walks are cached this long; disk free/total is never cached.
USAGE_TTL_SECONDS: float = 60.0

_usage_lock = threading.Lock()
_usage_cache: dict[str, Any] | None = None
_usage_key: tuple[str, ...] | None = None
_usage_ts: float = 0.0


def volume_usage(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Size and free space of the filesystem holding ``path``, or ``None``.

    ``free_bytes`` is the space a non-root process can still write
    (``f_bavail``) and ``used_bytes`` excludes root-reserved blocks, so
    ``percent`` matches what ``df`` prints.
    """
    statvfs = getattr(os, 'statvfs', None)
    if statvfs is None:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        return _volume(usage.total, usage.used, usage.free)
    try:
        st = statvfs(os.fspath(path))
    except OSError:
        return None
    block = st.f_frsize or st.f_bsize
    if not block or not st.f_blocks:
        return None
    total = st.f_blocks * block
    used = (st.f_blocks - st.f_bfree) * block
    free = st.f_bavail * block
    return _volume(total, used, free)


def _volume(total: int, used: int, free: int) -> dict[str, Any]:
    usable = used + free
    percent = round(used / usable * 100, 1) if usable > 0 else None
    return {'total_bytes': total, 'used_bytes': used, 'free_bytes': free, 'percent': percent}


def directory_size(path: str | os.PathLike[str], *, skip_hidden: bool = True) -> int:
    """Total bytes of the regular files under ``path``.

    Symlinks are not followed (so a link cannot count a file twice or escape
    the tree), unreadable entries are skipped, and with ``skip_hidden`` the
    dot-directories (the recorder's ``.prebuffer``/``.frames``/``.audio``
    working space) are left out, as they are not stored media.
    """
    total = 0
    stack = [os.fspath(path)]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        if not (skip_hidden and entry.name.startswith('.')):
                            stack.append(entry.path)
                    elif entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
    return total


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def database_size(path: str | os.PathLike[str]) -> int:
    """The SQLite database plus its write-ahead log and shared-memory files."""
    base = Path(path)
    return sum(_file_size(Path(f'{base}{suffix}')) for suffix in ('', '-wal', '-shm'))


def _nearest_existing(path: Path) -> Path:
    """``path`` or its closest existing parent, so ``statvfs`` has a target."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def _usage(locations: dict[str, Path], database: Path | None) -> dict[str, Any]:
    """Bytes used per location, through the TTL cache (one walk at a time)."""
    global _usage_cache, _usage_key, _usage_ts
    key = tuple(f'{label}={path}' for label, path in locations.items()) + (f'database={database}',)
    with _usage_lock:
        if _usage_cache is not None and _usage_key == key and time.monotonic() - _usage_ts < USAGE_TTL_SECONDS:
            return _usage_cache
        usage: dict[str, Any] = {label: directory_size(path) for label, path in locations.items()}
        usage['database'] = database_size(database) if database else 0
        _usage_cache, _usage_key, _usage_ts = usage, key, time.monotonic()
        return usage


def reset_usage_cache() -> None:
    """Drop the cached directory sizes (test isolation hook)."""
    global _usage_cache, _usage_key, _usage_ts
    with _usage_lock:
        _usage_cache, _usage_key, _usage_ts = None, None, 0.0


def storage_status(
    locations: dict[str, Path],
    database: Path | None,
    retention: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Volumes, per-location usage and retention limits for the storage card.

    ``locations`` maps a key (``recordings``, ``snapshots``...) to its folder.
    The response lists each filesystem once with the keys stored on it, the
    bytes each key uses, and the retention settings the purge job enforces.
    """
    targets = dict(locations)
    if database is not None:
        targets['database'] = database
    volumes: list[dict[str, Any]] = []
    by_device: dict[int, dict[str, Any]] = {}
    for label, path in targets.items():
        existing = _nearest_existing(Path(path))
        try:
            device = existing.stat().st_dev
        except OSError:
            continue
        if device in by_device:
            by_device[device]['holds'].append(label)
            continue
        snapshot = volume_usage(existing)
        if snapshot is None:
            continue
        volume = {**snapshot, 'holds': [label]}
        by_device[device] = volume
        volumes.append(volume)

    usage = _usage({label: Path(path) for label, path in locations.items()}, database)
    retention = retention or {}
    max_gb = retention.get('max_storage_gb')
    return {
        'volumes': volumes,
        'usage': {f'{label}_bytes': size for label, size in usage.items()},
        'retention': {
            'max_storage_bytes': int(max_gb) * 1024 ** 3 if isinstance(max_gb, (int, float)) and max_gb > 0 else None,
            'retention_days': retention.get('retention_days'),
            'auto_purge_enabled': retention.get('auto_purge_enabled'),
        },
        'usage_cached_seconds': USAGE_TTL_SECONDS,
    }
