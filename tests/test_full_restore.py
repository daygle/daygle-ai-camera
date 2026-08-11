"""Tests for full-backup restoration and portable media paths."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.backup as backup_module
import app.state as state


def _make_database(path: Path, *, media_root: Path, snapshot_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE users(id INTEGER PRIMARY KEY, role TEXT, is_active INTEGER DEFAULT 1);
            CREATE TABLE events(
                id INTEGER PRIMARY KEY,
                snapshot_path TEXT,
                thumbnail_path TEXT
            );
            CREATE TABLE detections(id INTEGER PRIMARY KEY, event_id INTEGER, label TEXT);
            CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE recordings(
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL,
                thumbnail_path TEXT
            );
            INSERT INTO users(id, role, is_active) VALUES (1, 'admin', 1);
            """
        )
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
            (
                "storage",
                json.dumps({
                    "data_dir": str(media_root.parent),
                    "recordings_dir": str(media_root),
                    "snapshots_dir": str(snapshot_root),
                    "events_dir": str(media_root.parent / "events"),
                    "database": str(path),
                }),
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO recordings(id, file_path, thumbnail_path) VALUES (?, ?, ?)",
            (1, str(media_root / "clip.mp4"), str(snapshot_root / "thumb.jpg")),
        )
        conn.execute(
            "INSERT INTO events(id, snapshot_path, thumbnail_path) VALUES (?, ?, ?)",
            (1, str(snapshot_root / "event.jpg"), str(snapshot_root / "event-thumb.jpg")),
        )
        conn.commit()
    finally:
        conn.close()


def test_full_restore_remaps_database_paths_and_restores_media(tmp_path, monkeypatch):
    source_root = tmp_path / "source" / "recordings"
    source_snapshots = tmp_path / "source" / "snapshots"
    source_events = tmp_path / "source" / "events"
    source_root.mkdir(parents=True)
    source_snapshots.mkdir(parents=True)
    source_events.mkdir(parents=True)
    source_db = tmp_path / "source" / "daygle.sqlite3"
    _make_database(source_db, media_root=source_root, snapshot_root=source_snapshots)
    (source_root / "clip.mp4").write_bytes(b"video")
    (source_snapshots / "event.jpg").write_bytes(b"snapshot")
    (source_snapshots / "event-thumb.jpg").write_bytes(b"thumbnail")
    (source_snapshots / "thumb.jpg").write_bytes(b"recording thumbnail")

    target_root = tmp_path / "target" / "recordings"
    target_snapshots = tmp_path / "target" / "snapshots"
    target_events = tmp_path / "target" / "events"
    target_db = tmp_path / "target" / "daygle.sqlite3"
    _make_database(target_db, media_root=target_root, snapshot_root=target_snapshots)

    monkeypatch.setattr(state, "database", SimpleNamespace(database_path=source_db))
    monkeypatch.setattr(
        backup_module,
        "effective_storage_config",
        lambda: {
            "data_dir": str(source_root.parent),
            "recordings_dir": str(source_root),
            "snapshots_dir": str(source_snapshots),
            "events_dir": str(source_events),
        },
    )
    archive = backup_module.create_full_backup()
    try:
        monkeypatch.setattr(state, "database", SimpleNamespace(database_path=target_db))
        monkeypatch.setattr(
            backup_module,
            "effective_storage_config",
            lambda: {
                "data_dir": str(target_root.parent),
                "recordings_dir": str(target_root),
                "snapshots_dir": str(target_snapshots),
                "events_dir": str(target_events),
            },
        )
        result = backup_module.restore_full_backup(archive)
        assert result["copied"]["recordings"] == 1
        assert result["copied"]["snapshots"] == 3

        assert (target_root / "clip.mp4").read_bytes() == b"video"
        assert (target_snapshots / "event.jpg").read_bytes() == b"snapshot"
        with sqlite3.connect(str(target_db)) as conn:
            recording_path = conn.execute("SELECT file_path FROM recordings WHERE id = 1").fetchone()[0]
            event_path = conn.execute("SELECT snapshot_path FROM events WHERE id = 1").fetchone()[0]
            storage = json.loads(conn.execute("SELECT value FROM app_settings WHERE key = 'storage'").fetchone()[0])
        assert recording_path == str(target_root / "clip.mp4")
        assert event_path == str(target_snapshots / "event.jpg")
        assert storage["recordings_dir"] == str(target_root)
        assert storage["snapshots_dir"] == str(target_snapshots)
        assert storage["database"] == str(target_db.resolve())
    finally:
        archive.unlink(missing_ok=True)


def test_full_restore_rejects_archive_traversal(tmp_path):
    archive = tmp_path / "malicious.zip"
    manifest = {"format": backup_module.FULL_BACKUP_FORMAT, "version": 2}
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("recordings/../outside.txt", b"should not extract")

    with pytest.raises(HTTPException, match="unsafe archive path"):
        backup_module.validate_full_backup(archive)
    assert not (tmp_path / "outside.txt").exists()
