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
import shutil
import subprocess
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


def recording_stream_path(file_path: Path) -> Path:
    playback_path = recording_playback_sidecar_path(file_path)
    if playback_path.exists() and file_path.exists() and (playback_path.stat().st_mtime >= file_path.stat().st_mtime):
        return playback_path
    if file_path.suffix.lower() == '.mp4' and mp4_is_browser_playable(file_path):
        return file_path
    failed_marker = file_path.with_name(f'{file_path.stem}.playback.failed')
    if failed_marker.exists() and file_path.exists() and (failed_marker.stat().st_mtime >= file_path.stat().st_mtime):
        return file_path
    try:
        transcode_recording_to_mp4(file_path, playback_path)
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


def transcode_recording_to_mp4(source_path: Path, output_path: Path) -> None:
    ffmpeg = _FFMPEG or shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required to convert recordings for browser playback.')
    tmp_path = output_path.with_name(f'{output_path.stem}.tmp{output_path.suffix}')
    if tmp_path.exists():
        tmp_path.unlink(missing_ok=True)
    command = [
        ffmpeg, '-y',
        '-fflags', '+discardcorrupt', '-err_detect', 'ignore_err',
        '-i', str(source_path),
        '-map', '0:v:0', '-map', '0:a:0?',
        '-c:v', 'libx264', '-c:a', 'aac', '-b:a', '128k',
        '-preset', 'veryfast', '-profile:v', 'main', '-level', '4.0',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
        str(tmp_path),
    ]
    duration = probe_video_duration(source_path) or 0.0
    timeout_seconds = max(120, int(duration * 3) + 60)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    if not tmp_path.exists():
        raise RuntimeError('MP4 conversion did not create an output file.')
    if result.returncode != 0 and (not mp4_has_video_stream(tmp_path)):
        tmp_path.unlink(missing_ok=True)
        error_detail = f'{result.stderr[:500]}\n...\n{result.stderr[-1000:]}'
        raise RuntimeError(f'ffmpeg failed to convert recording for browser playback: {error_detail}')
    if not mp4_has_video_stream(tmp_path):
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError('Converted MP4 does not contain a video stream.')
    tmp_path.replace(output_path)


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
