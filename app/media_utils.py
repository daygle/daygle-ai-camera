"""Media / ffmpeg-ffprobe utility cluster extracted from ``app/main.py`` (Phase-H).

Pure helpers that wrap ``ffprobe`` and ``ffmpeg`` subprocesses and sidecar-path
helpers for recording files.  No runtime state (locks, singletons, or DB) is
needed; all dependencies are resolved from the local filesystem and stdlib.

Exported symbols:
* ``recording_playback_sidecar_path`` - ``.h264-audio.mp4`` sidecar path
* ``recording_stream_path`` - choose the best streamable copy of a clip
* ``probe_video_codec`` - first video-stream codec (e.g. ``'h264'``)
* ``probe_audio_codec`` - first audio-stream codec (e.g. ``'aac'``)
* ``probe_stream_codec`` - low-level codec probe via ``ffprobe``
* ``ffmpeg_decoder_available`` - runtime decoder capability check
* ``mp4_is_browser_playable`` - True when H.264 + compatible audio
* ``probe_video_duration`` - clip duration in seconds
* ``transcode_recording_to_mp4`` - convert a clip to browser-playable MP4
* ``mp4_has_video_stream`` - True when a video stream is present
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.utils import _parse_iso_datetime


def _storage_roots(keys: tuple[str, ...]) -> tuple[Path, ...]:
    """Resolve configured media roots used to validate persisted file paths."""
    # Import lazily to avoid making this low-level ffmpeg helper participate in
    # the config-facade import graph during application bootstrap.
    from app.config_facades import effective_storage_config

    config = effective_storage_config()
    roots: list[Path] = []
    for key in keys:
        raw = config.get(key)
        if not raw:
            continue
        root = Path(str(raw)).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        roots.append(root.resolve(strict=False))
    return tuple(roots)


def safe_storage_path(
    raw_path: Any,
    *,
    roots: tuple[str, ...] = ('recordings_dir',),
) -> Path | None:
    """Return a persisted media path only when it stays inside configured roots.

    Recording metadata can be restored from an uploaded SQLite backup, so
    ``file_path`` and ``thumbnail_path`` are untrusted data. Never serve or
    unlink a path outside the configured media directories. Existing symlinks
    are rejected rather than followed; ``resolve(strict=False)`` also catches
    ``..`` traversal and symlinked parent directories that resolve elsewhere.
    """
    text = str(raw_path or '').strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.absolute()
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    for configured_root in _storage_roots(roots):
        root = Path(configured_root).expanduser().absolute()
        try:
            # Require the stored spelling to be below the configured root as
            # well as the resolved path. This rejects ``root/../secret``
            # instead of accepting a path that happens to resolve back inside.
            candidate.relative_to(root)
            resolved.relative_to(root.resolve(strict=False))
        except (ValueError, OSError):
            continue
        # Reject symlinks in every component below the configured root. Merely
        # resolving the final path is not enough: a restored path such as
        # ``recordings/camera-link/clip.mp4`` could otherwise follow a planted
        # symlinked directory. The configured root itself is the trust anchor;
        # a symlink root does not match the lexical check above.
        current = candidate
        has_symlink = False
        while current != root and current != current.parent:
            try:
                if current.is_symlink():
                    has_symlink = True
                    break
            except OSError:
                has_symlink = True
                break
            current = current.parent
        if has_symlink or resolved == root.resolve(strict=False):
            continue
        return resolved
    return None

logger = logging.getLogger('daygle.ai')

ONE_PIXEL_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'


def _recording_timeline_segment(recording: dict[str, Any], day_start: datetime, day_end: datetime) -> dict[str, Any] | None:
    started_at = _parse_iso_datetime(recording.get('started_at'))
    ended_at = _parse_iso_datetime(recording.get('ended_at'))
    duration_seconds = max(0.0, float(recording.get('duration_seconds') or 0.0))
    if started_at is None:
        return None
    if ended_at is None or ended_at <= started_at:
        ended_at = started_at + timedelta(seconds=max(duration_seconds, 1.0))
    visible_start = max(started_at, day_start)
    visible_end = min(ended_at, day_end)
    if visible_end <= visible_start:
        return None
    trigger_type = str(recording.get('trigger_type') or 'motion').lower()
    trigger_label = str(recording.get('trigger_label') or '').strip().lower()
    color_key = trigger_label if trigger_type in {'human', 'object', 'alert'} and trigger_label else trigger_type
    return {**recording, 'timeline_start_seconds': max(0.0, (visible_start - day_start).total_seconds()), 'timeline_end_seconds': min(86400.0, (visible_end - day_start).total_seconds()), 'timeline_duration_seconds': max(1.0, (visible_end - visible_start).total_seconds()), 'color_key': color_key, 'color_label': color_key}

_FFPROBE: str | None = shutil.which('ffprobe')
_FFMPEG: str | None = shutil.which('ffmpeg')


def recording_playback_sidecar_path(file_path: Path) -> Path:
    return file_path.with_name(f'{file_path.stem}.h264-audio.mp4')


# One conversion per recording at a time. A browser opening a clip usually
# sends more than one stream request (setting ``src`` then calling ``load()``),
# and each one ran its own ffmpeg into the same temporary file: one deleted the
# other's output mid-write, both reported failure, and the clip was then marked
# unplayable (recording 21395: "Converted MP4 does not contain a video stream"
# and "MP4 conversion did not create an output file" in the same second).
_conversion_locks: dict[str, threading.Lock] = {}
_conversion_locks_guard = threading.Lock()

# A failed conversion is not retried on every play, but it is retried after
# this long, so a transient failure does not leave a clip unplayable for good.
PLAYBACK_FAILURE_RETRY_SECONDS = 1800


def _conversion_lock(file_path: Path) -> threading.Lock:
    key = str(file_path.resolve(strict=False))
    with _conversion_locks_guard:
        lock = _conversion_locks.get(key)
        if lock is None:
            lock = _conversion_locks[key] = threading.Lock()
        return lock


def _fresh_sidecar(file_path: Path, playback_path: Path) -> bool:
    return playback_path.exists() and file_path.exists() and playback_path.stat().st_mtime >= file_path.stat().st_mtime


def recording_stream_path(file_path: Path, *, low_priority: bool = False) -> Path:
    playback_path = recording_playback_sidecar_path(file_path)
    if _fresh_sidecar(file_path, playback_path):
        return playback_path
    if file_path.suffix.lower() == '.mp4' and mp4_is_browser_playable(file_path):
        return file_path
    failed_marker = file_path.with_name(f'{file_path.stem}.playback.failed')
    with _conversion_lock(file_path):
        # Another request may have finished (or failed) the conversion while
        # this one waited.
        if _fresh_sidecar(file_path, playback_path):
            return playback_path
        if (
            failed_marker.exists() and file_path.exists()
            and failed_marker.stat().st_mtime >= file_path.stat().st_mtime
            and time.time() - failed_marker.stat().st_mtime < PLAYBACK_FAILURE_RETRY_SECONDS
        ):
            return file_path
        return _convert_for_playback(file_path, playback_path, failed_marker, low_priority)


def _convert_for_playback(file_path: Path, playback_path: Path, failed_marker: Path, low_priority: bool = False) -> Path:
    try:
        transcode_recording_to_mp4(file_path, playback_path, **({'low_priority': True} if low_priority else {}))
    except Exception as exc:
        logger.warning('Recording playback conversion failed for %s: %s', file_path, exc)
        try:
            failed_marker.write_bytes(b'')
        except OSError:
            pass  # A marker is only an optimization; keep the playable fallback.
        return file_path
    failed_marker.unlink(missing_ok=True)
    return playback_path if playback_path.exists() else file_path


def probe_video_codec(file_path: Path) -> str | None:
    """Return the first video stream's codec name (e.g. 'h264', 'hevc'), or None."""
    return probe_stream_codec(file_path, 'v:0')


def probe_audio_codec(file_path: Path) -> str | None:
    """Return the first audio stream's codec name (e.g. 'aac', 'pcm_mulaw'), or None."""
    return probe_stream_codec(file_path, 'a:0')


# FFmpeg normally reports both H.265 and camera-vendor H.265+ streams as
# ``hevc``. Keep the aliases here so callers can also handle metadata supplied
# by an NVR/camera as ``h265`` or ``h265+`` without treating them as unrelated
# codecs.
H264_CODECS = frozenset({'h264', 'avc', 'avc1'})
HEVC_CODECS = frozenset({'hevc', 'h265', 'h265+', 'hev1', 'hvc1'})


def normalize_video_codec(codec: str | None) -> str | None:
    """Return the stable codec family name for a video codec string."""
    value = str(codec or '').strip().lower()
    if not value:
        return None
    if value in H264_CODECS:
        return 'h264'
    if value in HEVC_CODECS:
        return 'hevc'
    return value


def is_h264_codec(codec: str | None) -> bool:
    return normalize_video_codec(codec) == 'h264'


def is_hevc_codec(codec: str | None) -> bool:
    return normalize_video_codec(codec) == 'hevc'


def video_codec_label(codec: str | None) -> str | None:
    """Return a stable human-readable label for H.264/H.265 codecs."""
    normalized = normalize_video_codec(codec)
    if normalized == 'h264':
        return 'H.264/AVC'
    if normalized == 'hevc':
        return 'H.265/HEVC'
    return str(codec).strip() if codec else None


def ffmpeg_decoder_available(codec: str | None) -> bool | None:
    """Return whether the runtime FFmpeg advertises a decoder for ``codec``.

    ``None`` means the runtime capability could not be determined (for example,
    FFmpeg is not installed). A false result is useful before starting a
    camera worker: H.265+ is usually reported as ``hevc`` and needs the HEVC
    decoder, while H.264 needs the H.264 decoder.
    """
    normalized = normalize_video_codec(codec)
    if normalized not in {'h264', 'hevc'}:
        return None
    ffmpeg = _FFMPEG or shutil.which('ffmpeg')
    if not ffmpeg:
        return None
    try:
        result = subprocess.run(
            [ffmpeg, '-hide_banner', '-decoders'],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    decoder = normalized
    for line in (result.stdout or '').splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == decoder:
            return True
    return False


def probe_stream_codec(file_path: Path, stream_selector: str) -> str | None:
    if not file_path.exists() or file_path.stat().st_size <= 0:
        return None
    ffprobe = _FFPROBE or shutil.which('ffprobe')
    if not ffprobe:
        return None
    command = [ffprobe, '-v', 'error', '-select_streams', stream_selector,
               '-show_entries', 'stream=codec_name',
               '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    codec = (result.stdout or '').strip().lower()
    return codec or None if result.returncode == 0 else None


def mp4_is_browser_playable(file_path: Path) -> bool:
    # HEVC/H.265+ recordings are intentionally not treated as directly
    # browser-playable. ``recording_stream_path`` will create the H.264
    # sidecar instead, while the original recording can remain stream-copied.
    if not is_h264_codec(probe_video_codec(file_path)):
        return False
    audio_codec = probe_audio_codec(file_path)
    return audio_codec in {None, '', 'aac', 'mp3'}


def probe_video_duration(file_path: Path) -> float | None:
    ffprobe = _FFPROBE or shutil.which('ffprobe')
    if not ffprobe or not file_path.exists():
        return None
    command = [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
               '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        return float((result.stdout or '').strip()) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# H.264 encoders for the browser-playback copy, tried in order. NVENC (the
# NVIDIA card's video encoder) is used when the app already decodes video on the
# GPU (Settings > Video Decoding) and this ffmpeg has it: an H.265/H.265+ clip
# then converts many times faster than real time without loading the CPU. Any
# failure falls back to libx264 on the CPU.
_NVENC_PROBE_TTL_SECONDS = 600.0
_nvenc_probe: dict[str, Any] = {'at': 0.0, 'ok': False}


def _ffmpeg_has_h264_nvenc() -> bool:
    if _nvenc_probe['at'] and time.monotonic() - float(_nvenc_probe['at']) < _NVENC_PROBE_TTL_SECONDS:
        return bool(_nvenc_probe['ok'])
    ffmpeg = _FFMPEG or shutil.which('ffmpeg')
    ok = False
    if ffmpeg:
        try:
            result = subprocess.run([ffmpeg, '-hide_banner', '-encoders'], capture_output=True, text=True, timeout=10, check=False)
            ok = result.returncode == 0 and any(
                len(line.split()) >= 2 and line.split()[1] == 'h264_nvenc' for line in (result.stdout or '').splitlines()
            )
        except (OSError, subprocess.SubprocessError):
            ok = False
    _nvenc_probe.update(at=time.monotonic(), ok=ok)
    return ok


def _playback_encode_attempts() -> list[tuple[str, list[str], list[str]]]:
    """``(name, input_args, video_output_args)`` per encoder, in order."""
    cpu = ('cpu', [], ['-c:v', 'libx264', '-preset', 'veryfast', '-profile:v', 'main', '-level', '4.0', '-pix_fmt', 'yuv420p'])
    try:
        from app.config_facades import effective_live_config
        from app.video_decode import resolve_video_decode
        decode = resolve_video_decode(effective_live_config().get('video_decode'))
    except Exception:  # noqa: BLE001 - no settings yet (tests, early start-up): CPU
        decode = 'cpu'
    if decode == 'gpu' and _ffmpeg_has_h264_nvenc():
        gpu = ('gpu', ['-hwaccel', 'cuda'], ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '23', '-profile:v', 'main', '-pix_fmt', 'yuv420p'])
        return [gpu, cpu]
    return [cpu]


def _lower_priority() -> None:  # pragma: no cover - runs in the ffmpeg child
    try:
        os.nice(10)
    except OSError:
        pass


def transcode_recording_to_mp4(source_path: Path, output_path: Path, *, low_priority: bool = False) -> None:
    """Write a browser-playable H.264/AAC copy of ``source_path`` to ``output_path``.

    ``low_priority`` (background pre-conversion) runs ffmpeg at a lower CPU
    priority so it never competes with live detection.
    """
    ffmpeg = _FFMPEG or shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required to convert recordings for browser playback.')
    duration = probe_video_duration(source_path) or 0.0
    timeout_seconds = max(120, int(duration * 3) + 60)
    last_error: BaseException | None = None
    for name, input_args, video_args in _playback_encode_attempts():
        try:
            _transcode_attempt(ffmpeg, source_path, output_path, input_args, video_args, timeout_seconds, low_priority)
            return
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            last_error = exc
            if name != 'cpu':
                logger.info('GPU playback conversion failed for %s, retrying on the CPU: %s', source_path, exc)
    assert last_error is not None
    raise last_error


def _transcode_attempt(
    ffmpeg: str,
    source_path: Path,
    output_path: Path,
    input_args: list[str],
    video_args: list[str],
    timeout_seconds: int,
    low_priority: bool,
) -> None:
    # Unique per attempt, so a concurrent attempt can never delete or replace
    # this one's output while ffmpeg is writing it.
    tmp_path = output_path.with_name(f'{output_path.stem}.{uuid.uuid4().hex[:8]}.tmp{output_path.suffix}')
    command = [
        ffmpeg, '-y',
        '-fflags', '+discardcorrupt', '-err_detect', 'ignore_err',
        *input_args,
        '-i', str(source_path),
        '-map', '0:v:0', '-map', '0:a:0?',
        *video_args,
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        str(tmp_path),
    ]
    extra: dict[str, Any] = {'preexec_fn': _lower_priority} if low_priority and os.name == 'posix' else {}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False, **extra)
        if not tmp_path.exists():
            raise RuntimeError('MP4 conversion did not create an output file.')
        if result.returncode != 0 and (not mp4_has_video_stream(tmp_path)):
            error_detail = f'{result.stderr[:500]}\n...\n{result.stderr[-1000:]}'
            raise RuntimeError(f'ffmpeg failed to convert recording for browser playback: {error_detail}')
        if not mp4_has_video_stream(tmp_path):
            raise RuntimeError('Converted MP4 does not contain a video stream.')
        tmp_path.replace(output_path)
    finally:
        # Whatever happened (including an ffmpeg timeout), never leave this
        # attempt's partial file behind.
        tmp_path.unlink(missing_ok=True)


def hevc_mp4_tag_args(file_path: Path | None) -> list[str]:
    """``['-tag:v', 'hvc1']`` when ``file_path``'s video is H.265, else ``[]``.

    ffmpeg stream-copies H.265 into MP4 with the ``hev1`` tag, which Apple
    players (iPhone, iPad, Mac / QuickTime) refuse; ``hvc1`` plays everywhere.
    The tag must not be set on H.264 - ffmpeg then fails to write the file.
    """
    if file_path is None:
        return []
    return ['-tag:v', 'hvc1'] if mp4_sample_entry_codec(Path(file_path)) == 'hevc' else []


_MP4_CODEC_ENTRIES = ((b'hvc1', 'hevc'), (b'hev1', 'hevc'), (b'avc1', 'h264'), (b'avc3', 'h264'))
_MP4_SCAN_BYTES = 256 * 1024


def mp4_sample_entry_codec(file_path: Path) -> str | None:
    """``'hevc'`` / ``'h264'`` from an MP4's video sample entry, else None.

    Reads the codec's four-character code (``hvc1``/``hev1``/``avc1``) from the
    file's ``moov`` box at the start (fragmented or faststart files) or the end
    (plain segment output) without starting an ffprobe process: it runs for
    every recorded segment and chunk. Unknown -> None, i.e. no special tag.
    """
    try:
        size = file_path.stat().st_size
        with file_path.open('rb') as handle:
            head = handle.read(_MP4_SCAN_BYTES)
            tail = b''
            if size > _MP4_SCAN_BYTES:
                handle.seek(max(_MP4_SCAN_BYTES, size - _MP4_SCAN_BYTES))
                tail = handle.read(_MP4_SCAN_BYTES)
    except OSError:
        return None
    for chunk in (head, tail):
        stsd = chunk.find(b'stsd')
        if stsd < 0:
            continue
        entry = chunk[stsd:stsd + 64]
        for code, codec in _MP4_CODEC_ENTRIES:
            if code in entry:
                return codec
    return None


def newest_video_file(directory: Path | None) -> Path | None:
    """The most recent finished ``.mp4`` in ``directory`` (to learn a camera's codec)."""
    if directory is None:
        return None
    try:
        candidates = [
            path for path in Path(directory).glob('*.mp4')
            if path.is_file() and '.tmp' not in path.name and path.stat().st_size > 0
        ]
    except OSError:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


# Background pre-conversion: an H.265/H.265+ recording gets its H.264 playback
# copy right after it is saved, one clip at a time and at low priority, so
# opening it later is instant instead of waiting (possibly past a reverse
# proxy's request timeout) for the conversion.
_preconvert_queue: queue.Queue[Path] = queue.Queue()
_preconvert_pending: set[str] = set()
_preconvert_guard = threading.Lock()
_preconvert_thread: threading.Thread | None = None


def schedule_playback_conversion(file_path: Path | str | None) -> bool:
    """Queue ``file_path`` for its browser-playback copy. Returns whether queued."""
    global _preconvert_thread
    if not file_path:
        return False
    path = Path(file_path)
    key = str(path)
    with _preconvert_guard:
        if key in _preconvert_pending:
            return False
        _preconvert_pending.add(key)
        if _preconvert_thread is None or not _preconvert_thread.is_alive():
            _preconvert_thread = threading.Thread(target=_preconvert_worker, name='playback-preconvert', daemon=True)
            _preconvert_thread.start()
    _preconvert_queue.put(path)
    return True


def _preconvert_worker() -> None:
    while True:
        path = _preconvert_queue.get()
        try:
            if path.exists() and not mp4_is_browser_playable(path):
                recording_stream_path(path, low_priority=True)
        except Exception as exc:  # noqa: BLE001 - background best effort
            logger.debug('Background playback conversion skipped for %s: %s', path, exc)
        finally:
            with _preconvert_guard:
                _preconvert_pending.discard(str(path))
            _preconvert_queue.task_done()


def mp4_has_video_stream(file_path: Path) -> bool:
    if not _FFPROBE:
        return file_path.exists() and file_path.stat().st_size > 0
    command = [_FFPROBE, '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', 'stream=codec_name',
               '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool((result.stdout or '').strip())
