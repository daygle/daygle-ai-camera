"""Host resource metrics (CPU, load average, RAM).

Reads directly from the Linux ``/proc`` filesystem plus ``os.getloadavg``
so the dashboard can show CPU/Load/RAM cards without pulling in a new
third-party dependency (e.g. ``psutil``). Every reader is best-effort:
on a non-Linux host, or if ``/proc`` is unavailable, the corresponding
fields come back as ``None`` and the caller renders a dash instead of
crashing.

CPU utilisation is a delta measurement - it needs two ``/proc/stat``
snapshots taken some time apart. We cache the previous snapshot at module
level so successive polls (the dashboard refreshes every few seconds)
report the busy fraction over the inter-poll interval, matching how
``psutil.cpu_percent(interval=None)`` behaves. The very first call, with
no prior snapshot, takes a short blocking sample so it still returns a
number.
"""

from __future__ import annotations

import os
import threading
import time

# Previous (idle, total) jiffie counts from /proc/stat, guarded by a lock
# because FastAPI serves requests from a threadpool and two concurrent
# polls must not corrupt the cached snapshot.
_cpu_lock = threading.Lock()
_prev_cpu: tuple[int, int] | None = None


def _read_cpu_times() -> tuple[int, int] | None:
    """Return ``(idle, total)`` jiffies from the aggregate ``/proc/stat`` row."""
    try:
        with open('/proc/stat', encoding='ascii') as handle:
            for line in handle:
                if line.startswith('cpu '):
                    fields = [int(v) for v in line.split()[1:]]
                    if len(fields) < 4:
                        return None
                    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
                    total = sum(fields)
                    return idle, total
    except (OSError, ValueError):
        return None
    return None


def cpu_percent() -> float | None:
    """Busy CPU fraction (0-100) over the interval since the previous call.

    The first call (no cached snapshot) briefly blocks ~100ms to produce a
    meaningful reading; later calls compare against the last snapshot and
    return immediately.
    """
    global _prev_cpu
    with _cpu_lock:
        current = _read_cpu_times()
        if current is None:
            return None
        prev = _prev_cpu
        if prev is None:
            time.sleep(0.1)
            second = _read_cpu_times()
            if second is None:
                return None
            prev, current = current, second
        _prev_cpu = current
        idle_delta = current[0] - prev[0]
        total_delta = current[1] - prev[1]
        if total_delta <= 0:
            return None
        busy = 100.0 * (1.0 - idle_delta / total_delta)
        return round(max(0.0, min(100.0, busy)), 1)


def load_average() -> list[float] | None:
    """Return the 1/5/15-minute load averages, or ``None`` where unsupported."""
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):
        return None
    return [round(one, 2), round(five, 2), round(fifteen, 2)]


def memory() -> dict[str, int | float] | None:
    """Return total/used/available RAM in bytes plus a used percentage."""
    values: dict[str, int] = {}
    try:
        with open('/proc/meminfo', encoding='ascii') as handle:
            for line in handle:
                key, _, rest = line.partition(':')
                if key in ('MemTotal', 'MemAvailable'):
                    values[key] = int(rest.strip().split()[0]) * 1024  # kB -> bytes
                    if len(values) == 2:
                        break
    except (OSError, ValueError, IndexError):
        return None
    total = values.get('MemTotal')
    available = values.get('MemAvailable')
    if not total or available is None:
        return None
    used = max(0, total - available)
    percent = round(100.0 * used / total, 1) if total else None
    return {
        'total': total,
        'available': available,
        'used': used,
        'percent': percent,
    }


def system_resources() -> dict[str, object]:
    """Aggregate CPU/Load/RAM snapshot for the dashboard resource cards."""
    return {
        'cpu_percent': cpu_percent(),
        'cpu_count': os.cpu_count(),
        'load_average': load_average(),
        'memory': memory(),
    }
