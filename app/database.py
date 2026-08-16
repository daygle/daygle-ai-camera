from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.db.alerts import AlertsMixin
from app.db.audit import AuditLogMixin
from app.db.diagnostics import CameraDiagnosticsMixin
from app.db.events import EventsMixin
from app.db.recordings import RecordingsMixin
from app.db.settings_repo import SettingsRepoMixin


# Immutable audit-log triggers, defined once so BOTH schema creation
# (``EventDatabase.init``) and the backup-restore validator
# (``app.backup.overwrite_database_from_file``) share a single source of
# truth. The restore validator allowlists these -- by exact (normalised)
# body, so a malicious backup cannot smuggle a payload under a trusted
# trigger name -- while still rejecting every other view/trigger. Keep the
# text here byte-for-byte identical to what ships in existing databases so
# the allowlist matches backups produced by any prior release.
AUDIT_LOG_IMMUTABLE_TRIGGERS: dict[str, str] = {
    'trg_audit_log_immutable_delete': """
                CREATE TRIGGER IF NOT EXISTS trg_audit_log_immutable_delete
                BEFORE DELETE ON audit_log
                WHEN OLD.immutable = 1
                BEGIN
                    SELECT RAISE(ABORT, 'Audit log is append-only - entries cannot be deleted.');
                END;
""",
    'trg_audit_log_immutable_update': """
                CREATE TRIGGER IF NOT EXISTS trg_audit_log_immutable_update
                BEFORE UPDATE OF created_at, user_id, username, action, resource,
                                       resource_id, details, ip_address, status
                ON audit_log
                WHEN OLD.immutable = 1
                BEGIN
                    SELECT RAISE(ABORT, 'Audit log is append-only - entries cannot be modified.');
                END;
""",
}


class EventDatabase(
    EventsMixin,
    RecordingsMixin,
    AlertsMixin,
    SettingsRepoMixin,
    AuditLogMixin,
    CameraDiagnosticsMixin,
):
    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init(self) -> None:
        """Create / migrate the schema for every domain table.

        Kept on the host class (rather than split across mixins) because schema
        creation is a one-shot bootstrap step that's easier to reason about in
        one place. The migrations tighten the schema over time; new migrations
        append to the list here.

        ``self.backfill_recording_labels(db)`` is resolved through MRO to
        ``RecordingsMixin.backfill_recording_labels`` so the join-table
        migration runs on every fresh init() just like before.
        """
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL;')
            db.commit()  # flush PRAGMA before executescript issues its own implicit COMMIT
            # Migration: add recording_id to alert_history if upgrading from a
            # pre-video-link schema. New installs get it via the CREATE TABLE
            # block below, so use ALTER TABLE for the upgrade path and swallow
            # the "duplicate column" error that fires when the column exists.
            try:
                db.execute("ALTER TABLE alert_history ADD COLUMN recording_id INTEGER REFERENCES recordings(id) ON DELETE SET NULL")
            except sqlite3.OperationalError:
                pass  # Column already exists on upgrades from older schemas.
            try:
                db.execute("ALTER TABLE events ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists on upgrades from older schemas.
            try:
                db.execute("ALTER TABLE alert_history ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists on upgrades from older schemas.
            try:
                db.execute("ALTER TABLE detections ADD COLUMN zone_name TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists on upgrades from pre-zone-name schema
            # Migration: mark detections that fired a still-dwell alert (the
            # "still for N minutes" Objects page setting) so the UI can badge
            # them. New installs get the column via CREATE TABLE below.
            try:
                db.execute("ALTER TABLE detections ADD COLUMN still_alert INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists on upgrades from pre-still-alert schema
            try:
                db.execute("ALTER TABLE detections ADD COLUMN still_alert_minutes INTEGER")
            except sqlite3.OperationalError:
                pass  # Column already exists on upgrades from pre-still-alert schema
            # Migration: make "which clip this event belongs to" a first-class
            # link. A recording spans many events (a continuous clip accrues a
            # fresh event each time a new object/sound appears), so events carry
            # the recording_id rather than recordings pointing at a single
            # "primary" event. New installs get the column via the CREATE TABLE
            # block below; ALTER covers the upgrade path.
            try:
                db.execute("ALTER TABLE events ADD COLUMN recording_id INTEGER REFERENCES recordings(id) ON DELETE SET NULL")
            except sqlite3.OperationalError:
                pass  # Column already exists on upgrades from older schemas.
            # Migration: track the best confidence seen for each recording label so
            # the recordings list can show a percentage for secondary objects that
            # were only detected after the trigger (their confidence otherwise lives
            # solely in the saved detection track, which the list does not load).
            try:
                db.execute("ALTER TABLE recording_labels ADD COLUMN confidence REAL")
            except sqlite3.OperationalError:
                pass  # Column already exists on upgrades from older schemas.
            # Migration: add the immutable column to existing databases.
            # The CREATE TABLE below already includes it for fresh installs.
            try:
                db.execute("ALTER TABLE audit_log ADD COLUMN immutable INTEGER NOT NULL DEFAULT 1")
            except sqlite3.OperationalError:
                pass  # Column already exists on upgrades from older schemas.
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    snapshot_path TEXT,
                    thumbnail_path TEXT,
                    alert_triggered INTEGER DEFAULT 0,
                    dismissed INTEGER NOT NULL DEFAULT 0,
                    recording_id INTEGER,
                    metadata TEXT DEFAULT '{}',
                    FOREIGN KEY(recording_id) REFERENCES recordings(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    x REAL NOT NULL,
                    y REAL NOT NULL,
                    width REAL NOT NULL,
                    height REAL NOT NULL,
                    zone_name TEXT,
                    still_alert INTEGER NOT NULL DEFAULT 0,
                    still_alert_minutes INTEGER,
                    FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    message TEXT NOT NULL,
                    recording_id INTEGER,
                    dismissed INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE,
                    FOREIGN KEY(recording_id) REFERENCES recordings(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
                CREATE INDEX IF NOT EXISTS idx_events_recording ON events(recording_id);
                CREATE INDEX IF NOT EXISTS idx_detections_label ON detections(label);
                CREATE INDEX IF NOT EXISTS idx_detections_event ON detections(event_id);

                CREATE TABLE IF NOT EXISTS recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER,
                    camera_id TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    file_path TEXT NOT NULL,
                    thumbnail_path TEXT,
                    source TEXT NOT NULL CHECK(source IN ('camera', 'upload', 'rtsp')),
                    trigger_type TEXT NOT NULL DEFAULT 'motion',
                    trigger_label TEXT,
                    created_at TEXT NOT NULL,
                    owner_user_id INTEGER,
                    FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE SET NULL,
                    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recordings_event ON recordings(event_id);
                CREATE INDEX IF NOT EXISTS idx_recordings_started_at ON recordings(started_at);
                CREATE INDEX IF NOT EXISTS idx_recordings_source ON recordings(source);

                CREATE TABLE IF NOT EXISTS recording_labels (
                    recording_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'detection',
                    created_at TEXT NOT NULL,
                    confidence REAL,
                    PRIMARY KEY (recording_id, label),
                    FOREIGN KEY(recording_id) REFERENCES recordings(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_recording_labels_label ON recording_labels(label);
                CREATE INDEX IF NOT EXISTS idx_recording_labels_recording ON recording_labels(recording_id);

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    user_id INTEGER,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    resource_id TEXT,
                    details TEXT NOT NULL DEFAULT '{}',
                    ip_address TEXT,
                    status TEXT NOT NULL DEFAULT 'success',
                    immutable INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_log_username ON audit_log(username);
                CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
                CREATE INDEX IF NOT EXISTS idx_audit_log_resource ON audit_log(resource);

                CREATE TABLE IF NOT EXISTS camera_diagnostics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    camera_id TEXT,
                    camera_name TEXT,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    message TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_camera_diag_created_at ON camera_diagnostics(created_at);
                CREATE INDEX IF NOT EXISTS idx_camera_diag_camera_id ON camera_diagnostics(camera_id);
                CREATE INDEX IF NOT EXISTS idx_camera_diag_event_type ON camera_diagnostics(event_type);
                CREATE INDEX IF NOT EXISTS idx_camera_diag_severity ON camera_diagnostics(severity);
                """
            )

            # Seed recording_labels for installs upgrading from a pre-multi-label schema.
            # Resolved through MRO: this method lives on RecordingsMixin, which
            # the host class inherits.
            self.backfill_recording_labels(db)

            # One-time cleanup of rows orphaned before the delete paths mirrored
            # the schema's referential actions. SQLite never enforced the declared
            # ON DELETE CASCADE / SET NULL (foreign_keys is off per connection),
            # so historical deletes left behind recording_labels / detections /
            # alert_history rows and dangling event/recording references. Each
            # statement only touches rows whose parent no longer exists, so it is
            # a safe no-op once the database is consistent (and on fresh installs).
            # ``id`` columns are PRIMARY KEYs and never NULL, so the ``NOT IN``
            # subqueries have no NULL-elimination pitfall.
            db.executescript(
                """
                DELETE FROM recording_labels
                    WHERE recording_id NOT IN (SELECT id FROM recordings);
                DELETE FROM detections
                    WHERE event_id NOT IN (SELECT id FROM events);
                DELETE FROM alert_history
                    WHERE event_id NOT IN (SELECT id FROM events);
                UPDATE recordings SET event_id = NULL
                    WHERE event_id IS NOT NULL
                      AND event_id NOT IN (SELECT id FROM events);
                UPDATE alert_history SET recording_id = NULL
                    WHERE recording_id IS NOT NULL
                      AND recording_id NOT IN (SELECT id FROM recordings);
                UPDATE events SET recording_id = NULL
                    WHERE recording_id IS NOT NULL
                      AND recording_id NOT IN (SELECT id FROM recordings);
                """
            )

            # Backfill events.recording_id for installs upgrading from the
            # pre-link schema. Runs after the orphan cleanup above so it only
            # links live rows. Resolved through MRO (EventsMixin).
            self.backfill_event_recording_links(db)

            # ── Immutable audit log triggers ────────────────────────────────
            # These SQLite triggers are the last line of defense for the
            # append-only audit log. They must be created AFTER the tables
            # exist (inside the executescript above), so this block runs *after*
            # the schema creation. ``CREATE TRIGGER IF NOT EXISTS`` makes them
            # idempotent on upgrades.
            #
            # The UPDATE trigger lists every content column but deliberately
            # OMITS ``immutable`` itself - this lets a future migration flip
            # immutable to 0 for a narrow exception (e.g. court-ordered
            # expungement) if ever needed. Without this carve-out, the only way
            # to ever delete a row would be to drop the trigger first.
            db.executescript('\n'.join(AUDIT_LOG_IMMUTABLE_TRIGGERS.values()))
