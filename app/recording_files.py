"""Shared recording-file cleanup helpers.

This module contains only path-validation and file-deletion logic so both the
backup retention code and recording-extension code can use it without forming
a circular import between those modules.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from collections.abc import Callable

from app.media_utils import recording_playback_sidecar_path, safe_storage_path

logger = logging.getLogger('daygle.ai.recording')


def recording_track_sidecar_path(file_path: Path) -> Path:
    """Return the detection-track sidecar path for a recording."""
    return file_path.with_name(f'{file_path.stem}.track.json')


def delete_snapshot_files(events: list[dict[str, Any]]) -> tuple[int, int]:
    """Delete event snapshot artifacts inside configured snapshot storage.

    Returns ``(files_deleted, bytes_deleted)``. Invalid or out-of-tree paths are
    skipped, matching the safety boundary used for recording media cleanup.
    """
    files_deleted = 0
    bytes_deleted = 0
    seen_paths: set[Path] = set()
    for event in events:
        for artifact_value in (event.get('snapshot_path'), event.get('thumbnail_path')):
            artifact = safe_storage_path(artifact_value, roots=('snapshots_dir',))
            if artifact is None or artifact in seen_paths:
                continue
            seen_paths.add(artifact)
            if artifact.exists() and artifact.is_file():
                try:
                    bytes_deleted += artifact.stat().st_size
                    artifact.unlink(missing_ok=True)
                    files_deleted += 1
                except OSError:
                    continue
    return files_deleted, bytes_deleted


def _unlink_quietly(path: Path) -> None:
    """Remove one recording artifact, tolerating a file another process holds.

    Retention deletes a whole batch of rows in one transaction and only then
    unlinks their media. On Windows (and for a denied/read-only file anywhere)
    ``unlink`` raises while a player or a finishing capture still has the file
    open, so letting it propagate aborted the rest of the batch: the remaining
    rows were already deleted, which orphaned their media with nothing left to
    find it, and the admin delete returned 500 after destroying the row.
    Best-effort per artifact matches ``delete_snapshot_files`` and
    ``_safe_rmtree_no_follow``; one locked file is logged and left behind.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning('Could not delete recording artifact %s: %s', path, exc)


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
                _unlink_quietly(file_path)
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
                    _unlink_quietly(playback_path)
        thumbnail = path_resolver(recording.get('thumbnail_path'), roots=('snapshots_dir',))
        if thumbnail is not None and thumbnail.exists() and thumbnail.is_file():
            _unlink_quietly(thumbnail)
