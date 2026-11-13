"""Tests for full-backup restoration and portable media paths."""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.support import symlink_or_skip

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
    # Keep backup/restore away from the repository's real models/ directory.
    monkeypatch.setattr(backup_module, "MODELS_ROOT", tmp_path / "models")
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
        with closing(sqlite3.connect(str(target_db))) as conn, conn:
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


def test_restore_copy_does_not_overwrite_hardlinked_file(tmp_path):
    source = tmp_path / 'source'
    target = tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    protected = tmp_path / 'protected'
    protected.write_bytes(b'private')
    (source / 'clip.mp4').write_bytes(b'restored')
    (target / 'clip.mp4').hardlink_to(protected)

    assert backup_module._copy_restored_tree(source, target) == 1
    assert protected.read_bytes() == b'private'
    assert (target / 'clip.mp4').read_bytes() == b'restored'


def test_restore_copy_rejects_dangling_symlink(tmp_path):
    source = tmp_path / 'source'
    target = tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    (source / 'clip.mp4').write_bytes(b'restored')
    symlink_or_skip(tmp_path / 'missing', target / 'clip.mp4')

    with pytest.raises(HTTPException, match='symbolic link'):
        backup_module._copy_restored_tree(source, target)
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('section', ['recordings', 'snapshots', 'events'])
@pytest.mark.parametrize('directory', ['.prebuffer', '.frames', '.audio', 'backups'])
def test_full_restore_rejects_runtime_directories(tmp_path, section, directory):
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('manifest.json', json.dumps({'format': backup_module.FULL_BACKUP_FORMAT, 'version': 2}))
        zf.writestr('database/source.sqlite3', b'database')
        zf.writestr(f'{section}/{directory}/camera/segment.mp4', b'untrusted')
    with pytest.raises(HTTPException, match='reserved runtime directory'):
        backup_module.validate_full_backup(archive)


def test_full_restore_rejects_archive_traversal(tmp_path):
    archive = tmp_path / "malicious.zip"
    manifest = {"format": backup_module.FULL_BACKUP_FORMAT, "version": 2}
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("recordings/../outside.txt", b"should not extract")

    with pytest.raises(HTTPException, match="unsafe archive path"):
        backup_module.validate_full_backup(archive)
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize('declared_length', [None, '1', '10000'])
def test_restore_upload_limit_applies_before_multipart_spooling(tmp_path, monkeypatch, declared_length):
    from app.api import settings_system_router as router

    monkeypatch.setattr(router, 'MAX_BACKUP_UPLOAD_BYTES', 256)
    monkeypatch.setattr(router, 'require_admin', lambda request: None)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_database] = lambda: SimpleNamespace(database_path=tmp_path / 'db.sqlite3')
    headers = {'Content-Type': 'multipart/form-data; boundary=boundary'}
    if declared_length is not None:
        headers['Content-Length'] = declared_length
    body = (
        b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="backup.zip"\r\n\r\n'
        + b'x' * 512 + b'\r\n--boundary--\r\n'
    )
    with TestClient(app) as client:
        response = client.post('/api/settings/system/database/restore', content=iter([body[:128], body[128:]]), headers=headers)
    assert response.status_code == 413
    assert not list(tmp_path.glob('.restore-*'))
    assert not router.DATABASE_RESTORE_LOCK.locked()


def test_restore_upload_validates_multipart_file(tmp_path, monkeypatch):
    from app.api import settings_system_router as router

    monkeypatch.setattr(router, 'require_admin', lambda request: None)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_database] = lambda: SimpleNamespace(database_path=tmp_path / 'db.sqlite3')
    with TestClient(app) as client:
        response = client.post('/api/settings/system/database/restore', files={'file': ('empty.sqlite3', b'')})
    assert response.status_code == 400
    assert response.json()['detail'] == 'Uploaded backup is empty.'
    assert not list(tmp_path.glob('.restore-*'))


def test_refresh_after_restore_rebuilds_the_detector(monkeypatch):
    """AI settings must be re-applied like the other settings families.

    A restore can change the backend / model path / confidence, and a full
    restore may have replaced the model files; the live detector otherwise
    keeps the ONNX session it built at startup, so the restored configuration
    only took effect after the next AI-settings save or a restart.
    """
    calls: list[object] = []
    monkeypatch.setattr(state, 'database', SimpleNamespace(init=lambda: calls.append('db')))
    monkeypatch.setattr(
        state, 'auth',
        SimpleNamespace(init=lambda: calls.append('auth'), apply_config=lambda _c: calls.append('auth_config')),
    )
    monkeypatch.setattr(state, 'apply_storage_and_recording_settings', lambda: calls.append('storage'))
    monkeypatch.setattr(state, 'apply_cameras_settings', lambda _c: calls.append('cameras'))
    monkeypatch.setattr(backup_module, 'effective_cameras_config', lambda: [])
    monkeypatch.setattr(backup_module, 'effective_auth_config', lambda: {})
    monkeypatch.setattr(backup_module, 'effective_ai_config', lambda: {'backend': 'onnx'})
    monkeypatch.setattr(backup_module, 'refresh_label_groups', lambda: calls.append('labels'))
    monkeypatch.setattr(state, 'reload_detector', lambda settings: calls.append(('detector', settings)))

    backup_module.refresh_runtime_after_database_restore()

    assert calls[:3] == ['db', 'auth', 'storage']
    assert calls[-1] == ('detector', {'backend': 'onnx'})


def test_refresh_after_restore_survives_a_standalone_host(monkeypatch):
    """``reload_detector`` is wired by camera_lifecycle; a host without it must
    still complete the restore refresh."""
    calls: list[object] = []
    monkeypatch.setattr(state, 'database', SimpleNamespace(init=lambda: calls.append('db')))
    monkeypatch.setattr(
        state, 'auth',
        SimpleNamespace(init=lambda: None, apply_config=lambda _c: None),
    )
    monkeypatch.setattr(state, 'apply_storage_and_recording_settings', lambda: None)
    monkeypatch.setattr(state, 'apply_cameras_settings', lambda _c: None)
    monkeypatch.setattr(backup_module, 'effective_cameras_config', lambda: [])
    monkeypatch.setattr(backup_module, 'effective_auth_config', lambda: {})
    monkeypatch.setattr(backup_module, 'refresh_label_groups', lambda: None)
    monkeypatch.setattr(state, 'reload_detector', None)

    backup_module.refresh_runtime_after_database_restore()

    assert calls == ['db']
