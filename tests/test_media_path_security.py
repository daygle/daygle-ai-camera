"""Regression tests for restored persisted-media path containment."""

from __future__ import annotations

from pathlib import Path
import app.media_utils as media_utils


def _storage(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(media_utils, "_storage_roots", lambda _keys: (root,))


def test_safe_storage_path_accepts_file_inside_configured_root(monkeypatch, tmp_path):
    root = tmp_path / "recordings"
    root.mkdir()
    media = root / "clip.mp4"
    media.write_bytes(b"video")
    _storage(monkeypatch, root)

    assert media_utils.safe_storage_path(media, roots=("recordings_dir",)) == media.resolve()


def test_safe_storage_path_rejects_parent_traversal(monkeypatch, tmp_path):
    root = tmp_path / "recordings"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("do not expose")
    _storage(monkeypatch, root)

    assert media_utils.safe_storage_path(root / ".." / "secret.txt", roots=("recordings_dir",)) is None


def test_safe_storage_path_rejects_symlinked_media_when_supported(monkeypatch, tmp_path):
    root = tmp_path / "recordings"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("do not expose")
    link = root / "clip.mp4"
    try:
        link.symlink_to(outside)
    except OSError:
        # Windows CI without symlink privileges cannot exercise this branch.
        return
    _storage(monkeypatch, root)

    assert media_utils.safe_storage_path(link, roots=("recordings_dir",)) is None


def test_recording_sidecars_do_not_follow_symlinks(monkeypatch, tmp_path):
    from app import recording_extension

    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'video')
    secret = tmp_path / 'secret.json'
    secret.write_text('[{"detections":[{"label":"private"}]}]')
    (tmp_path / 'clip.track.json').symlink_to(secret)
    assert recording_extension.load_recording_detection_track(clip) is None

    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'video')
    sidecar = tmp_path / 'clip.h264-audio.mp4'
    sidecar.symlink_to(outside)
    monkeypatch.setattr(media_utils, 'mp4_is_browser_playable', lambda _path: True)
    assert media_utils.recording_stream_path(clip) == clip


def test_recording_track_write_does_not_follow_old_temp_symlink(tmp_path):
    from app import recording_extension

    clip = tmp_path / 'clip.mp4'
    protected = tmp_path / 'protected'
    protected.write_bytes(b'private')
    (tmp_path / 'clip.track.json.tmp').symlink_to(protected)
    track = [{'detections': [{'label': 'person'}]}]
    recording_extension.write_recording_detection_track(clip, track)
    assert protected.read_bytes() == b'private'
    assert recording_extension.load_recording_detection_track(clip) == track


def test_failed_playback_marker_does_not_overwrite_hardlinked_file(monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'video')
    protected = tmp_path / 'protected'
    protected.write_bytes(b'private')
    marker = tmp_path / 'clip.playback.failed'
    marker.hardlink_to(protected)

    def fail(*_args, **_kwargs):
        raise RuntimeError('conversion failed')

    monkeypatch.setattr(media_utils, 'transcode_recording_to_mp4', fail)
    assert media_utils._convert_for_playback(clip, tmp_path / 'clip.h264-audio.mp4', marker) == clip
    assert protected.read_bytes() == b'private'
    assert marker.read_bytes() == b''


def test_delete_recording_files_skips_out_of_tree_paths(monkeypatch, tmp_path):
    from app import recording_extension

    root = tmp_path / "recordings"
    root.mkdir()
    protected = tmp_path / "protected.mp4"
    protected.write_bytes(b"keep")
    monkeypatch.setattr(
        recording_extension,
        "safe_storage_path",
        lambda raw, **_kwargs: None if str(raw) == str(protected) else Path(str(raw)),
    )

    recording_extension.delete_recording_files([{"file_path": str(protected)}])

    assert protected.exists()


def test_delete_recording_files_keeps_going_when_one_file_is_locked(monkeypatch, tmp_path):
    """One held file must not abort the rest of a retention/delete batch.

    The rows are deleted before their media is unlinked, so an exception here
    leaves every later recording's files behind with nothing left referencing
    them. Windows raises ``PermissionError`` for a clip a player still has open.
    """
    from app import recording_extension

    root = tmp_path / "recordings"
    root.mkdir()
    locked = root / "locked.mp4"
    locked.write_bytes(b"busy")
    deletable = root / "deletable.mp4"
    deletable.write_bytes(b"free")
    monkeypatch.setattr(
        recording_extension,
        "safe_storage_path",
        lambda raw, **_kwargs: Path(str(raw)) if raw else None,
    )
    original_unlink = Path.unlink

    def _unlink(path, *args, **kwargs):
        if path == locked:
            raise PermissionError(13, "The process cannot access the file", str(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)

    recording_extension.delete_recording_files([
        {"file_path": str(locked)},
        {"file_path": str(deletable)},
    ])

    assert locked.exists()
    assert not deletable.exists()


def test_playable_stream_path_reads_a_purge_race_as_unplayable(monkeypatch, tmp_path):
    """A clip purged while its playback path is probed answers 415, not 500."""
    from app.api import recordings_router

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")

    def _purged(_path):
        raise FileNotFoundError(str(_path))

    monkeypatch.setattr(recordings_router, "recording_stream_path", _purged)

    assert recordings_router._playable_stream_path(clip) is None


def test_database_media_checks_ignore_out_of_tree_recording_paths(monkeypatch, tmp_path):
    from app.database import EventDatabase
    import app.db.recordings as recordings_db

    database = EventDatabase(str(tmp_path / "events.sqlite3"))
    external = tmp_path / "external.mp4"
    external.write_bytes(b"must remain")
    recording_id = database.add_recording(
        event_id=None,
        camera_id="cam-1",
        started_at="2020-01-01T00:00:00+00:00",
        ended_at="2020-01-01T00:00:05+00:00",
        duration_seconds=5,
        file_path=str(external),
        thumbnail_path=None,
        source="rtsp",
        created_at="2020-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(recordings_db, "safe_storage_path", lambda *_args, **_kwargs: None)

    assert database.get_recording(recording_id)["media_ready"] is False
    removed = database.cleanup_incomplete_recordings()

    assert [row["id"] for row in removed] == [recording_id]
    assert external.read_bytes() == b"must remain"
