"""Capture timestamps for the ingest's decoded detection frames.

The shared ingest ffmpeg fans one RTSP connection out to stream-copied prebuffer
segments (the clip timeline) and a decoded ``latest.jpg`` (object detection).
Stamping a detection frame with ``latest.jpg``'s mtime puts it on a different
clock from the clip: the copy path writes packets as they arrive, while the
decode path only writes a frame after decoder pipelining (frame threads, NVDEC
surfaces), filtering and the JPEG encode. Measured against the app's own
segment timeline that is 0.3-0.8s on a CPU decoder and grows with thread
count, so every playback box trailed the object it described.

This module recovers the frame's real place on the clip clock instead:

* ffmpeg reports each detection frame's stream time (``-stats_mux_pre`` with
  ``{t}``) down a pipe, read here into a short per-camera ring along with the
  wall time each line arrived. Frames are chosen with ``select`` and
  ``-fps_mode passthrough`` rather than ``fps=N``, which would snap them onto a
  synthetic grid and report the slot time instead of the frame's own.
* The prebuffer segment muxer writes a short CSV list (``-segment_list``) of
  each closed segment's stream start/end. A closed segment's mtime is its
  content end on the wall clock - the same anchor the clip timeline uses - so
  ``mtime - end`` maps stream time onto that clock.

``capture_ts`` returns ``stream_time + offset`` for the frame ``latest.jpg``
holds, or ``None`` whenever anything is missing or implausible so the caller
keeps the mtime. Needs ffmpeg 6.1+ (``-stats_mux_pre``) and a POSIX host (the
stats pipe is an inherited fd); otherwise the ingest runs exactly as before.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

SEGMENT_LIST_NAME = '.segment_list.csv'
# Entries the capped segment list keeps; only the newest closed one is read.
SEGMENT_LIST_SIZE = 3
# Stats lines kept per camera: several seconds at the detection frame rate,
# far more than the gap between a frame's stats line and its JPEG landing.
_RING_SIZE = 64
# A stats line is written just before its JPEG is muxed; allow for the reader
# thread being scheduled a little after the rename it describes.
_ARRIVAL_SLACK_SECONDS = 0.05
# A frame is never captured after its JPEG was written, and the decode path is
# not plausibly several seconds behind; outside this window the mapping is
# stale (RTSP timestamp reset, reconnect) and the mtime is the safer answer.
_MAX_DECODE_LAG_SECONDS = 5.0

_support_lock = threading.Lock()
_support_cache: dict[str, bool] = {}


def stream_frame_stats_supported(ffmpeg: str) -> bool:
    """True when ``ffmpeg`` can report per-frame stream times down a pipe."""
    if os.name != 'posix':
        return False
    with _support_lock:
        cached = _support_cache.get(ffmpeg)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [ffmpeg, '-hide_banner', '-h', 'full'],
            capture_output=True, text=True, timeout=20, check=False,
        )
        supported = '-stats_mux_pre_fmt' in (result.stdout or '')
    except Exception:  # noqa: BLE001 - any probe failure just means "use the mtime"
        # Not cached: a transient failure (ffmpeg mid-upgrade) should not pin
        # the camera to the less accurate stamp for the life of the process.
        return False
    with _support_lock:
        _support_cache[ffmpeg] = supported
    return supported


def detection_frame_output_args(fps: int, stats_fd: int | None) -> list[str]:
    """Frame-selection and stats options for the decoded detection output.

    Without a stats fd this is the historical ``fps=N`` filter. With one, frames
    are selected at roughly the same rate but keep their own timestamps, and
    each frame's stream time is reported on ``stats_fd``.
    """
    rate = max(1, int(fps))
    if stats_fd is None:
        return ['-vf', f'fps={rate}']
    # 0.9x the interval so jitter in the source frame times never skips a slot
    # the fps filter would have filled.
    min_gap = round(0.9 / rate, 4)
    return [
        '-vf', f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{min_gap})'",
        '-fps_mode:v', 'passthrough',
        '-stats_mux_pre', f'pipe:{stats_fd}',
        '-stats_mux_pre_fmt', '{t}',
    ]


def segment_list_args(camera_dir: Path) -> list[str]:
    """Segment-muxer options writing the capped CSV list the clock reads.

    Removes a list left by a previous ffmpeg first: its stream times belong to
    that process's timeline, and mapping the new process's frames through it
    would yield a plausible-looking but wrong capture time until the new
    process closed its first segment.
    """
    try:
        (camera_dir / SEGMENT_LIST_NAME).unlink(missing_ok=True)
    except OSError:
        pass
    return [
        '-segment_list', str(camera_dir / SEGMENT_LIST_NAME),
        '-segment_list_type', 'csv',
        '-segment_list_size', str(SEGMENT_LIST_SIZE),
    ]


class FrameCaptureClock:
    """Per-camera stream-time ring plus the segment-list offset lookup."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rings: dict[str, deque[tuple[float, float]]] = {}
        self._segment_lists: dict[str, Path] = {}
        # camera_key -> ((list row, segment mtime_ns), offset)
        self._offsets: dict[str, tuple[tuple[str, int], float]] = {}

    def attach(self, camera_key: str, read_fd: int, camera_dir: Path) -> threading.Thread:
        """Start reading one ffmpeg process's stats pipe for ``camera_key``.

        Takes ownership of ``read_fd``. Clears the previous process's state,
        whose stream times and offsets belong to a different timeline.
        """
        with self._lock:
            self._rings[camera_key] = deque(maxlen=_RING_SIZE)
            self._segment_lists[camera_key] = camera_dir / SEGMENT_LIST_NAME
            self._offsets.pop(camera_key, None)
            ring = self._rings[camera_key]
        thread = threading.Thread(
            target=self._read_stats, args=(camera_key, read_fd, ring),
            name=f'frame-clock-{camera_key}', daemon=True,
        )
        thread.start()
        return thread

    def detach(self, camera_key: str) -> None:
        """Forget a camera's mapping (ingest now running without stats)."""
        with self._lock:
            self._rings.pop(camera_key, None)
            self._segment_lists.pop(camera_key, None)
            self._offsets.pop(camera_key, None)

    def _read_stats(self, camera_key: str, read_fd: int, ring: deque[tuple[float, float]]) -> None:
        # Runs until ffmpeg exits and the pipe reaches EOF. Draining promptly
        # matters: a full pipe would block ffmpeg's muxing.
        try:
            with os.fdopen(read_fd, 'rb', buffering=0) as stream:
                pending = b''
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    arrived = time.time()
                    pending += chunk
                    *lines, pending = pending.split(b'\n')
                    for line in lines:
                        try:
                            stream_t = float(line)
                        except ValueError:
                            continue
                        with self._lock:
                            ring.append((stream_t, arrived))
        except OSError as exc:
            logger.debug('Frame clock stats pipe for %s closed: %s', camera_key, exc)

    def _offset(self, camera_key: str) -> float | None:
        """Wall-minus-stream offset from the newest closed prebuffer segment."""
        with self._lock:
            list_path = self._segment_lists.get(camera_key)
            cached = self._offsets.get(camera_key)
        if list_path is None:
            return None
        try:
            # The muxer rewrites this small file in place; a read that lands
            # mid-rewrite sees nothing usable and keeps the last good offset.
            rows = [row for row in list_path.read_text(encoding='utf-8').splitlines() if row.strip()]
            name, _start, end = rows[-1].rsplit(',', 2)
            segment_mtime_ns = (list_path.parent / name).stat().st_mtime_ns
            end_seconds = float(end)
        except (OSError, ValueError, IndexError):
            return cached[1] if cached else None
        key = (rows[-1], segment_mtime_ns)
        if cached and cached[0] == key:
            return cached[1]
        offset = segment_mtime_ns / 1e9 - end_seconds
        with self._lock:
            # Only store against the list this process is still writing.
            if self._segment_lists.get(camera_key) == list_path:
                self._offsets[camera_key] = (key, offset)
        return offset

    def capture_ts(self, camera_key: str, jpeg_mtime: float) -> float | None:
        """Clip-clock capture time of the frame written at ``jpeg_mtime``."""
        with self._lock:
            ring = self._rings.get(camera_key)
            entries = list(ring) if ring else []
        if not entries:
            return None
        # The JPEG's own stats line is the newest one written before it landed.
        stream_t = None
        for value, arrived in reversed(entries):
            if arrived <= jpeg_mtime + _ARRIVAL_SLACK_SECONDS:
                stream_t = value
                break
        if stream_t is None:
            return None
        offset = self._offset(camera_key)
        if offset is None:
            return None
        captured = stream_t + offset
        if not jpeg_mtime - _MAX_DECODE_LAG_SECONDS <= captured <= jpeg_mtime + _ARRIVAL_SLACK_SECONDS:
            return None
        return captured
