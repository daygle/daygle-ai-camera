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
