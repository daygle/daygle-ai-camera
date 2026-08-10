"""Shared recording-file cleanup helpers.

This module contains only path-validation and file-deletion logic so both the
backup retention code and recording-extension code can use it without forming
a circular import between those modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.media_utils import recording_playback_sidecar_path, safe_storage_path


def recording_track_sidecar_path(file_path: Path) -> Path:
    """Return the detection-track sidecar path for a recording."""
    return file_path.with_name(f'{file_path.stem}.track.json')


def delete_recording_files(
    recordings: list[dict[str, Any]],
    *,
    path_resolver: Callable[..., Path | None] = safe_storage_path,
) -> None:
    """Delete recording media only when paths stay inside configured storage.

    Recording rows can come from a restored SQLite backup and are therefore
    untrusted. Invalid or out-of-tree paths are deliberately skipped rather
    than allowing retention or an admin delete to unlink arbitrary host files.
    """
    for recording in recordings:
        file_path = path_resolver(recording.get('file_path'), roots=('recordings_dir',))
        if file_path is not None:
            if file_path.exists() and file_path.is_file():
                file_path.unlink(missing_ok=True)
            playback_paths = [
                recording_playback_sidecar_path(file_path),
                recording_track_sidecar_path(file_path),
                file_path.with_name(f'{file_path.stem}.playback.failed'),
                file_path.with_name(f'{file_path.stem}.h264.mp4'),
                file_path.with_name(f'{file_path.stem}.browser.mp4'),
                file_path.with_name(f'{file_path.stem}.playback.mp4'),
                file_path.with_name(f'{file_path.name}.meta.json'),
            ]
            for playback_path in playback_paths:
                if playback_path.exists() and playback_path.is_file():
                    playback_path.unlink(missing_ok=True)
        thumbnail = path_resolver(recording.get('thumbnail_path'), roots=('snapshots_dir',))
        if thumbnail is not None and thumbnail.exists() and thumbnail.is_file():
            thumbnail.unlink(missing_ok=True)
