"""Media / ffmpeg-ffprobe utility cluster extracted from ``app/main.py`` (Phase-H).

Pure helpers that wrap ``ffprobe`` and ``ffmpeg`` subprocesses and sidecar-path
helpers for recording files.  No runtime state (locks, singletons, or DB) is
needed; all dependencies are resolved from the local filesystem and stdlib.

Exported symbols:
* ``recording_playback_sidecar_path`` — ``.h264-audio.mp4`` sidecar path
* ``recording_stream_path`` — choose the best streamable copy of a clip
* ``probe_video_codec`` — first video-stream codec (e.g. ``'h264'``)
* ``probe_audio_codec`` — first audio-stream codec (e.g. ``'aac'``)
* ``probe_stream_codec`` — low-level codec probe via ``ffprobe``
* ``mp4_is_browser_playable`` — True when H.264 + compatible audio
* ``probe_video_duration`` — clip duration in seconds
* ``transcode_recording_to_mp4`` — convert a clip to browser-playable MP4
* ``mp4_has_video_stream`` — True when a video stream is present
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger('daygle.ai')

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
            pass
        return file_path
    failed_marker.unlink(missing_ok=True)
    return playback_path if playback_path.exists() else file_path


def probe_video_codec(file_path: Path) -> str | None:
    """Return the first video stream's codec name (e.g. 'h264', 'hevc'), or None."""
    return probe_stream_codec(file_path, 'v:0')


def probe_audio_codec(file_path: Path) -> str | None:
    """Return the first audio stream's codec name (e.g. 'aac', 'pcm_mulaw'), or None."""
    return probe_stream_codec(file_path, 'a:0')


def probe_stream_codec(file_path: Path, stream_selector: str) -> str | None:
    if not file_path.exists() or file_path.stat().st_size <= 0:
        return None
    if not _FFPROBE:
        return None
    ffprobe = _FFPROBE
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
    if probe_video_codec(file_path) != 'h264':
        return False
    audio_codec = probe_audio_codec(file_path)
    return audio_codec in {None, '', 'aac', 'mp3'}


def probe_video_duration(file_path: Path) -> float | None:
    if not _FFPROBE or not file_path.exists():
        return None
    ffprobe = _FFPROBE
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
