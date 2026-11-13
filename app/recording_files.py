"""Shared recording-file cleanup helpers.

This module contains only path-validation and file-deletion logic so both the
backup retention code and recording-extension code can use it without forming
a circular import between those modules.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
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


# Start-up artifact sweep
#
# The recording pipeline writes every intermediate beside the file it belongs
# to and renames or deletes it on success: ``<stem>.recording.tmp.mp4`` (event
# capture), ``<stem>.prebuffer.tmp.mp4`` + ``<stem>.concat.txt`` (prebuffer
# render), ``<stem>.audio.mp4`` (audio mux) and ``<stem>.<id>.tmp.mp4``
# (playback conversion). A crash or SIGKILL between write and rename leaves one
# behind for good - nothing in the service ever looks at a file whose recording
# row was never created, so these accumulate on the recordings drive forever.
# The same is true of footage the database never learned about: a
# continuous-recording chunk finalised after its callback stopped, or one whose
# insert failed, is invisible to the row-driven retention purge. It is also true
# of an event clip whose row retention deleted but whose file could not be
# unlinked at the time (a player or a still-finalising render held it open).
#
# The sweep runs at start-up (see ``app_lifespan``) BEFORE the ingest and
# continuous workers start, and every deletion is additionally age-guarded so a
# later call can never remove a file that is still being written.
ORPHAN_ARTIFACT_MIN_AGE_SECONDS = 3600.0

# Root-level event clips are named ``event_<id>_<YYYYmmdd>_<HHMMSS>_<micros>.<ext>``
# (app.recordings.event_recording_metadata). Matching the exact shape keeps the
# sweep away from derived artifacts that start with the same stem but carry a
# second suffix -- ``<stem>.audio.mp4`` (audio mux), ``<stem>.browser.mp4`` /
# ``.h264.mp4`` / ``.playback.mp4`` (playback copies) and ``<stem>.track.json``
# -- all of which are either rebuilt on demand or cleaned by their own rule.
_EVENT_CLIP_NAME_RE = re.compile(r'^event_\d+_\d{8}_\d{6}_\d{6}\.[a-z0-9]+$')


def _normalise_media_path(path: str | Path) -> str:
    """Comparable form of a persisted path: absolute, case-folded on Windows."""
    return os.path.normcase(os.path.abspath(str(path)))


def _is_within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or below it."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def sweep_orphaned_recording_artifacts(
    recordings_root: Path | str,
    *,
    referenced_paths: set[str] | frozenset[str],
    min_age_seconds: float = ORPHAN_ARTIFACT_MIN_AGE_SECONDS,
) -> dict[str, int]:
    """Delete recording artifacts a previous session left behind.

    ``referenced_paths`` is every path the database still knows about (see
    ``RecordingsMixin.recording_file_paths``); a continuous chunk that is not in
    it and is older than ``min_age_seconds`` was never registered, so nothing
    else will ever delete it. Temp intermediates and aged-out ``.prebuffer`` /
    ``.audio`` ingest segments are matched by name and age alone - no row ever
    references them. ``.frames`` (last-frame fallbacks), playback sidecars and
    the ``.playback.failed`` retry markers are deliberately left alone.

    Returns per-category counts plus the total bytes reclaimed. A single locked
    or unreadable artifact is logged and left for the next start instead of
    aborting the sweep.
    """
    root = Path(recordings_root).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.absolute()
    report = {
        'files_deleted': 0,
        'bytes_deleted': 0,
        'temp_files': 0,
        'orphaned_chunks': 0,
        'orphaned_event_clips': 0,
        'ingest_segments': 0,
        'staging_dirs': 0,
    }
    if not root.is_dir():
        return report
    cutoff = time.time() - max(0.0, float(min_age_seconds))
    referenced = {_normalise_media_path(path) for path in referenced_paths}
    prebuffer_root = root / '.prebuffer'
    audio_root = root / '.audio'

    def remove(path: Path) -> bool:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning('Could not remove orphaned recording artifact %s: %s', path, exc)
            return False
        report['files_deleted'] += 1
        report['bytes_deleted'] += size
        return True

    def aged_out(path: Path) -> bool:
        try:
            return path.stat().st_mtime < cutoff
        except OSError:
            # Vanished between listing and stat: nothing left to clean. Metadata
            # that cannot be read is left alone rather than deleted blindly.
            return False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        # Never follow a symlinked directory: restored libraries are untrusted
        # and the sweep must not be able to reach outside the recordings root.
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in list(dirnames):
            if not name.startswith('daygle-audio-mux-'):
                continue
            staging = current / name
            if not aged_out(staging):
                continue
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                logger.warning('Could not remove abandoned audio-mux staging %s: %s', staging, exc)
                continue
            dirnames.remove(name)
            report['staging_dirs'] += 1
        in_prebuffer = _is_within(current, prebuffer_root)
        in_audio = _is_within(current, audio_root)
        in_chunks = current.parent == root and current.name.startswith('continuous-')
        for name in filenames:
            path = current / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
            except OSError:
                continue
            lower = name.lower()
            if lower.endswith('.concat.txt') or '.tmp' in lower or lower.startswith('.playback-failed-'):
                # ``.playback-failed-*`` are mkstemp leftovers from writing a
                # failure marker; the marker itself (``.playback.failed``) is a
                # deliberate retry guard and does not match this rule.
                if aged_out(path) and remove(path):
                    report['temp_files'] += 1
                continue
            if in_audio:
                if lower.startswith('aud-') and lower.endswith('.wav') and aged_out(path) and remove(path):
                    report['ingest_segments'] += 1
                continue
            if in_prebuffer:
                if lower.startswith('segment-') and lower.endswith(('.mp4', '.ts')) and aged_out(path) and remove(path):
                    report['ingest_segments'] += 1
                continue
            if current == root and _EVENT_CLIP_NAME_RE.match(lower):
                # Event clips are row-backed from the moment of trigger, so an
                # unreferenced one is footage whose row retention (or an admin
                # delete) already removed but whose file could not be unlinked
                # -- a player, a finishing render or an antivirus scanner held
                # it open, which is common on Windows. The row is the only
                # index to that file, so without this rule its space is never
                # reclaimed. Age-guarded like every other category so a clip
                # being written right now is never at risk.
                if _normalise_media_path(path) not in referenced and aged_out(path) and remove(path):
                    report['orphaned_event_clips'] += 1
                continue
            if lower.endswith('.audio.mp4'):
                # Audio-mux output: the mux renames it over the silent render,
                # so a surviving one belongs to a session that died mid-mux.
                if _normalise_media_path(path) not in referenced and aged_out(path) and remove(path):
                    report['temp_files'] += 1
                continue
            if in_chunks and lower.startswith('continuous_') and lower.endswith('.mp4'):
                if _normalise_media_path(path) in referenced:
                    continue
                if aged_out(path) and remove(path):
                    report['orphaned_chunks'] += 1
    return report
