"""Unit tests for ``app.backup.create_full_backup`` archive creation."""

from __future__ import annotations

import json
import sqlite3
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.state as _state  # noqa: E402
import app.backup as backup_module  # noqa: E402


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)")
        conn.execute("INSERT INTO users(username) VALUES ('admin')")
        conn.commit()
    finally:
        conn.close()


def _configure(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "daygle.sqlite3"
    _make_db(db_path)
    recordings = tmp_path / "recordings"
    snapshots = tmp_path / "snapshots"
    monkeypatch.setattr(_state, "database", SimpleNamespace(database_path=db_path))
    monkeypatch.setattr(
        backup_module,
        "effective_storage_config",
        lambda: {
            "data_dir": str(data),
            "recordings_dir": str(recordings),
            "snapshots_dir": str(snapshots),
        },
    )
    return db_path, recordings, snapshots


def test_create_full_backup_includes_database_recordings_and_snapshots(tmp_path, monkeypatch):
    _db_path, recordings, snapshots = _configure(monkeypatch, tmp_path)
    (recordings / ".prebuffer").mkdir(parents=True)
    (recordings / ".frames").mkdir()
    (recordings / ".audio").mkdir()
    (recordings / "clip.mp4").write_bytes(b"video-bytes")
    (recordings / ".prebuffer" / "seg.mp4").write_bytes(b"transient")
    (recordings / ".frames" / "latest.jpg").write_bytes(b"transient")
    (recordings / ".audio" / "aud.wav").write_bytes(b"transient")
    snapshots.mkdir()
    (snapshots / "snap.jpg").write_bytes(b"jpg-bytes")

    archive = backup_module.create_full_backup()
    assert archive.suffix == ".zip" and archive.exists()
    try:
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            # The archived database must carry its REAL filename, not the
            # temporary snapshot name used while the archive is built.
            assert f"database/{_db_path.name}" in names
            db_entries = [n for n in names if n.startswith("database/") and n.endswith(".sqlite3")]
            assert db_entries, "the database snapshot must be inside the archive"
            assert "recordings/clip.mp4" in names
            assert "snapshots/snap.jpg" in names
            # Transient ingest state must never be archived.
            assert not any(
                ".prebuffer" in name or ".frames" in name or ".audio" in name
                for name in names
            )
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["format"] == "daygle-full-backup"
            assert manifest["database_filename"] == _db_path.name
            # Storage roots are recorded so a future restore can remap the
            # absolute file_path values stored in the database.
            assert manifest["storage"]["recordings_dir"] == str(recordings)
            assert manifest["storage"]["snapshots_dir"] == str(snapshots)
            assert manifest["included"]["recordings"]["files"] == 1
            assert manifest["included"]["snapshots"]["files"] == 1
            assert "events" in manifest["included"]
            assert "models" in manifest["included"]
            assert manifest["included"]["database"]["files"] == 1
            assert manifest["version"] == backup_module.FULL_BACKUP_VERSION

            # The embedded database must be a valid SQLite file that survived the zip.
            embedded = zf.read(db_entries[0])
        extracted = tmp_path / "embedded.sqlite3"
        extracted.write_bytes(embedded)
        conn = sqlite3.connect(str(extracted))
        try:
            assert conn.execute("SELECT username FROM users").fetchone()[0] == "admin"
        finally:
            conn.close()
    finally:
        archive.unlink(missing_ok=True)
    # The temporary snapshot must be cleaned up.
    assert not list(tmp_path.rglob(".full-backup-*.sqlite3"))


def test_create_full_backup_handles_empty_dirs_and_relative_roots(tmp_path, monkeypatch):
    _db_path, recordings, snapshots = _configure(monkeypatch, tmp_path)
    recordings.mkdir(parents=True)
    snapshots.mkdir(parents=True)

    # Relative configured roots must resolve against the process CWD.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        backup_module,
        "effective_storage_config",
        lambda: {
            "data_dir": str(tmp_path / "data"),
            "recordings_dir": "recordings",
            "snapshots_dir": "snapshots",
        },
    )
    archive = backup_module.create_full_backup()
    assert archive.exists()
    try:
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert not any(name.startswith("recordings/") for name in names)
    finally:
        archive.unlink(missing_ok=True)


def test_create_full_backup_skips_backups_dir_inside_recordings(tmp_path, monkeypatch):
    # A config where recordings_dir == data_dir means the backups directory (and
    # any archive written there) lives INSIDE the walked tree. It must be pruned
    # so the archive never contains itself or accumulated backups.
    _configure(monkeypatch, tmp_path)
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    (data / "clip.mp4").write_bytes(b"video")
    monkeypatch.setattr(
        backup_module,
        "effective_storage_config",
        lambda: {
            "data_dir": str(data),
            "recordings_dir": str(data),
            "snapshots_dir": str(data),
        },
    )
    archive = backup_module.create_full_backup()
    assert archive.exists()
    try:
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            assert "recordings/clip.mp4" in names
            assert not any("backups" in name for name in names)
            assert not any("daygle-full-" in name for name in names)
    finally:
        archive.unlink(missing_ok=True)
