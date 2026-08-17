"""Host resource metrics (CPU, load average, RAM, GPU).

Reads directly from the Linux ``/proc`` filesystem plus ``os.getloadavg``
so the dashboard can show CPU/Load/RAM cards without pulling in a new
third-party dependency (e.g. ``psutil``). The GPU card shells out to
``nvidia-smi`` (shipped with the NVIDIA driver), also without a new pip
dependency. Every reader is best-effort: on a non-Linux host, if ``/proc``
is unavailable, or if ``nvidia-smi`` is absent, the corresponding fields
come back as ``None`` and the caller renders a dash instead of crashing.

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
import subprocess
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


# Thermal thresholds for the GPU health card. The validated Tesla P4
# deployment (Pascal, sm_61) throttles at ~90 C, so warn well before that
# and treat 90+ as critical. They are generic enough for other NVIDIA
# cards too (90 C is a common hard thermal limit across the lineup).
GPU_TEMP_WARN_C = 85
GPU_TEMP_CRITICAL_C = 90

# Fields requested from nvidia-smi, in the same order the CSV columns come
# back. Names never contain commas, so splitting on ',' is safe.
_NVIDIA_SMI_QUERY = (
    'name,temperature.gpu,utilization.gpu,clocks.current.graphics,'
    'clocks.current.memory,power.draw,power.limit,memory.used,memory.total'
)
_NVIDIA_SMI_FIELDS = (
    'name',
    'temperature_c',
    'utilization_percent',
    'graphics_clock_mhz',
    'memory_clock_mhz',
    'power_draw_watts',
    'power_limit_watts',
    'memory_used_mb',
    'memory_total_mb',
)


def _parse_number(value: str) -> int | float | None:
    """Parse an nvidia-smi CSV cell; ``[N/A]`` (and junk) become ``None``."""
    value = value.strip()
    if not value or value == '[N/A]':
        return None
    try:
        return float(value) if '.' in value else int(value)
    except ValueError:
        return None


def nvidia_smi_devices() -> list[dict] | None:
    """Snapshot every NVIDIA GPU via ``nvidia-smi``, or ``None`` if unavailable.

    Best-effort: a missing binary, a non-zero exit, an unexpected column
    count, or a timeout all yield ``None`` so the dashboard card degrades to
    a dash instead of erroring the whole resources endpoint.
    """
    try:
        result = subprocess.run(
            [
                'nvidia-smi',
                f'--query-gpu={_NVIDIA_SMI_QUERY}',
                '--format=csv,noheader,nounits',
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    devices: list[dict] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) != len(_NVIDIA_SMI_FIELDS):
            continue
        device: dict = {}
        for field, raw in zip(_NVIDIA_SMI_FIELDS, parts):
            if field == 'name':
                device[field] = raw or None
            else:
                device[field] = _parse_number(raw)
        devices.append(device)
    return devices or None


def _thermal_status(temperature_c: int | float | None, warn_c: int, critical_c: int) -> str:
    """Map a GPU temperature to ``ok`` / ``warn`` / ``critical``."""
    if temperature_c is None:
        return 'unknown'
    if temperature_c >= critical_c:
        return 'critical'
    if temperature_c >= warn_c:
        return 'warn'
    return 'ok'


def gpu_status(
    warn_c: int = GPU_TEMP_WARN_C,
    critical_c: int = GPU_TEMP_CRITICAL_C,
    devices: list[dict] | None = None,
) -> dict | None:
    """GPU snapshot for the dashboard health card, or ``None`` without NVIDIA.

    ``warn_c`` / ``critical_c`` are the thermal thresholds; they default to
    the module constants and are overridable by the admin via the system
    settings (``/api/settings/system/gpu``). ``devices`` lets a caller pass an
    already-fetched ``nvidia_smi_devices`` snapshot (e.g. the TTL-cached one)
    so a single nvidia-smi read can be classified against different thresholds
    without re-shelling; omitted, it takes a fresh snapshot.
    """
    if devices is None:
        devices = nvidia_smi_devices()
    if not devices:
        return None
    primary = dict(devices[0])
    primary['thermal_status'] = _thermal_status(primary.get('temperature_c'), warn_c, critical_c)
    return {
        'count': len(devices),
        'devices': devices,
        'primary': primary,
        'warn_temp_c': warn_c,
        'critical_temp_c': critical_c,
    }


# The dashboard polls /api/system/resources every ~5s, and several open tabs
# poll independently -- each call would otherwise spawn a fresh nvidia-smi
# process (up to the 3s timeout, blocking a threadpool worker). A short TTL
# snapshot collapses those into at most one nvidia-smi read per window,
# regardless of tab count. Kept out of ``nvidia_smi_devices`` / ``gpu_status``
# so those stay pure for direct callers and tests; only the polled
# ``system_resources`` path reads through the cache.
_GPU_SNAPSHOT_TTL_SECONDS: float = 4.0
_gpu_snapshot_lock: threading.Lock = threading.Lock()
_gpu_snapshot: list[dict] | None = None
_gpu_snapshot_ts: float = 0.0
_gpu_snapshot_valid: bool = False


def _cached_nvidia_smi_devices() -> list[dict] | None:
    """``nvidia_smi_devices`` behind a short TTL so rapid pollers share one read.

    Caches the ``None`` result too, so a host without an NVIDIA GPU does not
    re-spawn nvidia-smi on every poll just to fail again.
    """
    global _gpu_snapshot, _gpu_snapshot_ts, _gpu_snapshot_valid
    now = time.monotonic()
    with _gpu_snapshot_lock:
        if _gpu_snapshot_valid and (now - _gpu_snapshot_ts) < _GPU_SNAPSHOT_TTL_SECONDS:
            return _gpu_snapshot
    devices = nvidia_smi_devices()
    with _gpu_snapshot_lock:
        _gpu_snapshot = devices
        _gpu_snapshot_ts = time.monotonic()
        _gpu_snapshot_valid = True
    return devices


def reset_gpu_snapshot_cache() -> None:
    """Drop the cached nvidia-smi snapshot (tests / after a driver change)."""
    global _gpu_snapshot, _gpu_snapshot_valid
    with _gpu_snapshot_lock:
        _gpu_snapshot = None
        _gpu_snapshot_valid = False


def system_resources(
    gpu_warn_c: int = GPU_TEMP_WARN_C,
    gpu_critical_c: int = GPU_TEMP_CRITICAL_C,
) -> dict[str, object]:
    """Aggregate CPU/Load/RAM/GPU snapshot for the dashboard resource cards."""
    # Read the cached snapshot once and only classify when it is non-empty:
    # passing a cached ``None`` (no GPU) straight into ``gpu_status`` would hit
    # its fallback branch and re-shell nvidia-smi, defeating the cache on the
    # very hosts that never have a GPU.
    gpu_devices = _cached_nvidia_smi_devices()
    gpu = (
        gpu_status(warn_c=gpu_warn_c, critical_c=gpu_critical_c, devices=gpu_devices)
        if gpu_devices else None
    )
    return {
        'cpu_percent': cpu_percent(),
        'cpu_count': os.cpu_count(),
        'load_average': load_average(),
        'memory': memory(),
        'gpu': gpu,
    }
