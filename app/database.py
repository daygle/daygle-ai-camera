from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class EventDatabase:
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
        finally:
            connection.close()

    def init(self) -> None:
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL;')
            # Migration: add recording_id to alert_history if upgrading from a
            # pre-video-link schema. New installs get it via the CREATE TABLE
            # block below, so use ALTER TABLE for the upgrade path and swallow
            # the "duplicate column" error that fires when the column exists.
            try:
                db.execute("ALTER TABLE alert_history ADD COLUMN recording_id INTEGER REFERENCES recordings(id) ON DELETE SET NULL")
            except sqlite3.OperationalError:
                pass
            try:
                db.execute("ALTER TABLE events ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                db.execute("ALTER TABLE alert_history ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                db.execute("ALTER TABLE detections ADD COLUMN zone_name TEXT")
            except sqlite3.OperationalError:
                pass
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
                    metadata TEXT DEFAULT '{}'
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
                    FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recordings_event ON recordings(event_id);
                CREATE INDEX IF NOT EXISTS idx_recordings_started_at ON recordings(started_at);
                CREATE INDEX IF NOT EXISTS idx_recordings_source ON recordings(source);

                CREATE TABLE IF NOT EXISTS recording_labels (
                    recording_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'detection',
                    created_at TEXT NOT NULL,
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
                    status TEXT NOT NULL DEFAULT 'success'
                );

                CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_log_username ON audit_log(username);
                CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
                CREATE INDEX IF NOT EXISTS idx_audit_log_resource ON audit_log(resource);
                """
            )

            # Seed recording_labels for installs upgrading from a pre-multi-label schema.
            self.backfill_recording_labels(db)

    def add_event(
        self,
        created_at: str,
        source: str,
        snapshot_path: str | None,
        detections: list[dict[str, Any]],
        alert_triggered: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO events (created_at, source, snapshot_path, alert_triggered, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (created_at, source, snapshot_path, int(alert_triggered), json.dumps(metadata or {})),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Failed to create event row")
            event_id = cursor.lastrowid
            for detection in detections:
                box = detection.get("box", {})
                db.execute(
                    """
                    INSERT INTO detections (event_id, label, confidence, x, y, width, height, zone_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        detection["label"],
                        float(detection["confidence"]),
                        float(box.get("x", 0)),
                        float(box.get("y", 0)),
                        float(box.get("width", 0)),
                        float(box.get("height", 0)),
                        detection.get("zone_name") or None,
                    ),
                )
            return event_id


    def add_recording(
        self,
        *,
        event_id: int | None,
        camera_id: str | None,
        started_at: str,
        ended_at: str,
        duration_seconds: float,
        file_path: str,
        thumbnail_path: str | None,
        source: str,
        created_at: str,
        trigger_type: str = "motion",
        trigger_label: str | None = None,
        labels: list[str] | None = None,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO recordings (event_id, camera_id, started_at, ended_at, duration_seconds, file_path, thumbnail_path, source, trigger_type, trigger_label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, camera_id, started_at, ended_at, duration_seconds, file_path, thumbnail_path, source, trigger_type, trigger_label, created_at),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Failed to create recording row")
            recording_id = cursor.lastrowid
            # When no explicit labels are provided, seed recording_labels from
            # the linked event's detections and the trigger_label so the join
            # table filter is robust for recordings created without labels=[...].
            if labels is None and event_id is not None:
                detection_labels = [
                    str(row['label']).strip().lower()
                    for row in db.execute(
                        "SELECT DISTINCT label FROM detections WHERE event_id = ?",
                        (int(event_id),),
                    ).fetchall()
                ]
                normalized_trigger = str(trigger_label or '').strip().lower()
                labels = list(dict.fromkeys(detection_labels + ([normalized_trigger] if normalized_trigger else [])))
            if labels:
                self._insert_recording_labels(db, recording_id, labels, source='detection')
            return recording_id

    @staticmethod
    def _insert_recording_labels(
        db: sqlite3.Connection,
        recording_id: int,
        labels: list[str],
        *,
        source: str = 'detection',
    ) -> None:
        """Insert unique non-generic labels for a recording.

        Existing rows for the same (recording_id, label) pair are left untouched
        (the primary key is the composite) so callers can call this freely
        from extension / trigger-update paths without duplicating entries.
        """
        if not labels:
            return
        seen: set[str] = set()
        rows: list[tuple[int, str, str, str]] = []
        now = datetime.now(timezone.utc).isoformat()
        for raw in labels:
            label = str(raw or '').strip().lower()
            if not label or label in seen:
                continue
            seen.add(label)
            rows.append((int(recording_id), label, source, now))
        if not rows:
            return
        db.executemany(
            "INSERT OR IGNORE INTO recording_labels (recording_id, label, source, created_at) VALUES (?, ?, ?, ?)",
            rows,
        )

    def add_recording_labels(self, recording_id: int, labels: list[str], *, source: str = 'detection') -> int:
        """Append unique labels to a recording's label set.

        Returns the number of rows newly inserted (existing labels are skipped).
        Safe to call from extension / trigger-update paths.
        """
        with self.connect() as db:
            existing = {
                str(row['label'])
                for row in db.execute(
                    "SELECT label FROM recording_labels WHERE recording_id = ?",
                    (int(recording_id),),
                ).fetchall()
            }
            new_labels = [
                str(raw or '').strip().lower()
                for raw in labels
                if str(raw or '').strip() and str(raw or '').strip().lower() not in existing
            ]
            if not new_labels:
                return 0
            self._insert_recording_labels(db, int(recording_id), new_labels, source=source)
            return len(new_labels)

    def backfill_recording_labels(self, db: sqlite3.Connection | None = None) -> int:
        """One-shot migration: seed recording_labels from existing detections
        and trigger_label columns for installs upgrading from a pre-multi-label
        schema. Safe to call on every init() - does nothing if the join table
        is already populated for a recording.
        """
        own = db is None
        if own:
            with self.connect() as conn:
                return self.backfill_recording_labels(conn)
        rows = db.execute(
            """
            SELECT r.id AS recording_id,
                   r.trigger_label,
                   (SELECT GROUP_CONCAT(DISTINCT lower(d.label))
                      FROM detections d
                     WHERE d.event_id = r.event_id) AS detection_labels
            FROM recordings r
            WHERE NOT EXISTS (
                SELECT 1 FROM recording_labels rl WHERE rl.recording_id = r.id
            )
            """
        ).fetchall()
        total = 0
        generic = {'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous', ''}
        for row in rows:
            recording_id = int(row['recording_id'])
            labels: list[str] = []
            if row['detection_labels']:
                for label in str(row['detection_labels']).split(','):
                    normalized = label.strip().lower()
                    if normalized and normalized not in generic:
                        labels.append(normalized)
            trigger_label = str(row['trigger_label'] or '').strip().lower()
            if trigger_label and trigger_label not in generic and trigger_label not in labels:
                labels.append(trigger_label)
            if labels:
                self._insert_recording_labels(db, recording_id, labels, source='backfill')
                total += len(labels)
        return total

    def list_recordings(
        self,
        label: str | None = None,
        labels: list[str] | None = None,
        camera_id: str | None = None,
        limit: int = 50,
        alerted_only: bool = False,
        started_after: str | None = None,
        started_before: str | None = None,
        sort: str = 'newest',
        source_type: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.connect() as db:
            conditions: list[str] = []
            params: list[Any] = []

            # Normalize: accept either a single label string or a list of labels.
            resolved_labels: list[str] = []
            if label:
                resolved_labels = [str(label).strip().lower()]
            elif labels:
                resolved_labels = [str(l).strip().lower() for l in labels if str(l).strip()]

            if resolved_labels:
                # Join against recording_labels (the authoritative "labels that
                # appeared in this recording" table) rather than detections, so
                # labels added by extension / trigger updates still match.
                placeholders = ','.join('?' * len(resolved_labels))
                conditions.append(f"rl.label IN ({placeholders})")
                params.extend(resolved_labels)
            if camera_id:
                conditions.append("r.camera_id = ?")
                params.append(camera_id)
            if alerted_only:
                conditions.append(
                    "EXISTS (SELECT 1 FROM alert_history ah WHERE ah.recording_id = r.id OR (r.event_id IS NOT NULL AND ah.event_id = r.event_id))"
                )
            if started_after:
                conditions.append("r.started_at >= ?")
                params.append(started_after)
            if started_before:
                conditions.append("r.started_at <= ?")
                params.append(started_before)
            if source_type == 'sound':
                conditions.append("EXISTS (SELECT 1 FROM events e WHERE e.id = r.event_id AND e.source = 'sound')")
            elif source_type == 'object':
                conditions.append("(r.event_id IS NULL OR NOT EXISTS (SELECT 1 FROM events e WHERE e.id = r.event_id AND e.source = 'sound'))")

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            # Sort must be a fixed allowlist - never inject user input into the
            # ORDER BY clause. Whitelisting the column and direction keeps the
            # query safe while still letting callers pick newest/oldest.
            sort_normalized = (sort or 'newest').strip().lower()
            if sort_normalized not in {'newest', 'oldest'}:
                sort_normalized = 'newest'
            order_by = 'r.started_at DESC' if sort_normalized == 'newest' else 'r.started_at ASC'

            if resolved_labels:
                sql = f"SELECT DISTINCT r.* FROM recordings r LEFT JOIN recording_labels rl ON rl.recording_id = r.id {where} ORDER BY {order_by}, r.id DESC LIMIT ?"
            else:
                sql = f"SELECT r.* FROM recordings r {where} ORDER BY {order_by}, r.id DESC LIMIT ?"

            params.append(limit)
            rows = db.execute(sql, params).fetchall()
            return self._assemble_recordings(db, rows)

    def list_recordings_for_camera_day(self, camera_id: str, day_start: str, day_end: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            # Escape LIKE wildcards in camera_id so % and _ in the id do not alter the pattern scope.
            safe_camera_id = str(camera_id).replace('%', '\\%').replace('_', '\\_')
            rows = db.execute(
                """
                SELECT DISTINCT r.*
                FROM recordings r
                LEFT JOIN events e ON e.id = r.event_id
                WHERE (
                    r.camera_id = ?
                    OR (
                        r.camera_id IS NULL
                        AND e.metadata LIKE ? ESCAPE '\\'
                    )
                )
                AND r.started_at < ?
                AND COALESCE(r.ended_at, r.started_at) >= ?
                ORDER BY r.started_at ASC, r.id ASC
                """,
                (camera_id, f'%"camera_id": "{safe_camera_id}"%', day_end, day_start),
            ).fetchall()
            return self._assemble_recordings(db, rows)

    def get_recording(self, recording_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            return self._recording_with_event(db, row) if row else None

    def update_recording_timing(self, recording_id: int, *, ended_at: str, duration_seconds: float, started_at: str | None = None) -> bool:
        with self.connect() as db:
            if started_at is not None:
                cursor = db.execute(
                    "UPDATE recordings SET started_at = ?, ended_at = ?, duration_seconds = ? WHERE id = ?",
                    (started_at, ended_at, float(duration_seconds), recording_id),
                )
            else:
                cursor = db.execute(
                    "UPDATE recordings SET ended_at = ?, duration_seconds = ? WHERE id = ?",
                    (ended_at, float(duration_seconds), recording_id),
                )
            return cursor.rowcount > 0

    def update_recording_trigger(self, recording_id: int, *, trigger_type: str, trigger_label: str | None) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE recordings SET trigger_type = ?, trigger_label = ? WHERE id = ?",
                (str(trigger_type or 'motion'), str(trigger_label).strip().lower() if trigger_label else None, recording_id),
            )
            return cursor.rowcount > 0

    def delete_event(self, event_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                return None
            event = dict(row)
            event["metadata"] = json.loads(event.get("metadata") or "{}")
            db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            return event

    def delete_all_events(self) -> int:
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
            db.execute("DELETE FROM events")
            return int(count)

    def cleanup_incomplete_recordings(self) -> list[dict[str, Any]]:
        """Delete recordings whose files were never written (e.g. service restarted mid-capture)."""
        with self.connect() as db:
            rows = db.execute("SELECT * FROM recordings").fetchall()
            incomplete = []
            for row in rows:
                file_path = row["file_path"]
                if not file_path:
                    incomplete.append(dict(row))
                    continue
                path = Path(str(file_path))
                if not (path.exists() and path.stat().st_size > 0):
                    incomplete.append(dict(row))
            if incomplete:
                ids = [int(r["id"]) for r in incomplete]
                db.execute(f"DELETE FROM recordings WHERE id IN ({','.join('?' * len(ids))})", ids)
            return incomplete

    def delete_all_recordings(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM recordings").fetchall()
            db.execute("DELETE FROM recordings")
            return [dict(row) for row in rows]

    def delete_recording(self, recording_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
            return dict(row)

    def purge_recordings(self, *, older_than: str | None = None, max_storage_bytes: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            # When only age-based purge is needed, filter in the database.
            # Size-based purge needs all rows to correctly identify oldest recordings.
            if older_than and max_storage_bytes is None:
                candidates = [dict(row) for row in db.execute(
                    "SELECT * FROM recordings WHERE started_at < ? ORDER BY started_at ASC",
                    (older_than,),
                ).fetchall()]
            else:
                candidates = [dict(row) for row in db.execute("SELECT * FROM recordings ORDER BY started_at ASC").fetchall()]
            purge_ids: set[int] = set()
            if older_than:
                purge_ids.update(int(row["id"]) for row in candidates if str(row["started_at"]) < older_than)
            if max_storage_bytes is not None:
                existing_with_sizes: list[tuple[dict[str, Any], int]] = []
                for row in candidates:
                    try:
                        existing_with_sizes.append((row, Path(str(row["file_path"])).stat().st_size))
                    except OSError:
                        continue
                total = sum(size for _, size in existing_with_sizes)
                for row, size in existing_with_sizes:
                    if total <= max_storage_bytes:
                        break
                    purge_ids.add(int(row["id"]))
                    total -= size
            if not purge_ids:
                return []
            rows = [row for row in candidates if int(row["id"]) in purge_ids]
            db.executemany("DELETE FROM recordings WHERE id = ?", [(row["id"],) for row in rows])
            return rows

    def add_alert(self, created_at: str, rule_name: str, event_id: int, label: str, confidence: float, message: str, recording_id: int | None = None) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO alert_history (created_at, rule_name, event_id, label, confidence, message, recording_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, rule_name, event_id, label, confidence, message, recording_id),
            )

    def search_events(self, label: str | None = None, limit: int = 50, alerted_only: bool = False, with_recording: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            alert_filter = """
                AND EXISTS (
                    SELECT 1
                    FROM alert_history ah
                    WHERE ah.event_id = e.id
                )
            """
            recording_condition = """
                (
                    EXISTS (SELECT 1 FROM recordings WHERE recordings.event_id = e.id)
                    OR EXISTS (
                        SELECT 1
                        FROM alert_history ah
                        JOIN recordings r ON r.id = ah.recording_id
                        WHERE ah.event_id = e.id
                    )
                )
            """
            recording_filter = f"AND {recording_condition}"
            if label:
                rows = db.execute(
                    f"""
                    SELECT DISTINCT e.* FROM events e
                    JOIN detections d ON d.event_id = e.id
                    WHERE d.label = ?
                    AND e.dismissed = 0
                    {alert_filter if alerted_only else ''}
                    {recording_filter if with_recording else ''}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    (label, limit),
                ).fetchall()
            elif alerted_only:
                rows = db.execute(
                    f"""
                    SELECT e.* FROM events e
                    WHERE e.dismissed = 0
                    AND EXISTS (
                        SELECT 1
                        FROM alert_history ah
                        WHERE ah.event_id = e.id
                    )
                    {recording_filter if with_recording else ''}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            elif with_recording:
                rows = db.execute(
                    f"""
                    SELECT e.* FROM events e
                    WHERE e.dismissed = 0
                    AND {recording_condition}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM events WHERE dismissed = 0 ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

            return [self._event_with_detections(db, row) for row in rows]

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                return None
            return self._event_with_detections(db, row)

    def stats(self) -> dict[str, Any]:
        with self.connect() as db:
            total_events = db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
            total_alerts = db.execute("SELECT COUNT(*) AS count FROM alert_history").fetchone()["count"]
            sound_detection_events = db.execute(
                "SELECT COUNT(*) AS count FROM events WHERE source = 'sound'"
            ).fetchone()["count"]
            matched_object_events = db.execute(
                """
                SELECT COUNT(DISTINCT e.id) AS count
                FROM detections d
                JOIN events e ON e.id = d.event_id
                WHERE d.label != 'motion'
                  AND e.source != 'sound'
                  AND e.dismissed = 0
                  AND (
                      EXISTS (SELECT 1 FROM recordings WHERE recordings.event_id = e.id)
                      OR EXISTS (
                          SELECT 1 FROM alert_history ah
                          JOIN recordings r ON r.id = ah.recording_id
                          WHERE ah.event_id = e.id
                      )
                  )
                """
            ).fetchone()["count"]
            object_alerts = db.execute(
                """
                SELECT COUNT(*) AS count
                FROM alert_history
                WHERE event_id IS NULL
                   OR event_id NOT IN (SELECT id FROM events WHERE source = 'sound')
                """
            ).fetchone()["count"]
            sound_alerts = db.execute(
                """
                SELECT COUNT(*) AS count
                FROM alert_history
                WHERE event_id IN (SELECT id FROM events WHERE source = 'sound')
                """
            ).fetchone()["count"]
            labels = db.execute(
                """
                SELECT label, COUNT(*) AS count, MAX(confidence) AS max_confidence
                FROM detections
                GROUP BY label
                ORDER BY count DESC
                """
            ).fetchall()
            return {
                "total_events": total_events,
                "total_alerts": total_alerts,
                "matched_object_events": matched_object_events,
                "sound_detection_events": sound_detection_events,
                "object_alerts": object_alerts,
                "sound_alerts": sound_alerts,
                "objects": [dict(row) for row in labels],
            }

    def alerts(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT ah.*, r.id AS recording_id
                FROM alert_history ah
                LEFT JOIN recordings r ON r.id = ah.recording_id
                WHERE ah.dismissed = 0
                ORDER BY ah.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def dismiss_event(self, event_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("UPDATE events SET dismissed = 1 WHERE id = ?", (event_id,))
            return cursor.rowcount > 0

    def dismiss_all_events(self) -> int:
        with self.connect() as db:
            cursor = db.execute("UPDATE events SET dismissed = 1 WHERE dismissed = 0")
            return cursor.rowcount

    def dismiss_alert_group(self, group_key: str) -> int:
        parts = group_key.split('-', 1)
        if len(parts) != 2:
            return 0
        kind, raw_id = parts
        try:
            id_val = int(raw_id)
        except ValueError:
            return 0
        with self.connect() as db:
            if kind == 'event':
                cursor = db.execute(
                    "UPDATE alert_history SET dismissed = 1 WHERE event_id = ? AND dismissed = 0",
                    (id_val,),
                )
            elif kind == 'alert':
                cursor = db.execute(
                    "UPDATE alert_history SET dismissed = 1 WHERE id = ? AND dismissed = 0",
                    (id_val,),
                )
            else:
                return 0
            return cursor.rowcount

    def dismiss_all_alerts(self) -> int:
        with self.connect() as db:
            cursor = db.execute("UPDATE alert_history SET dismissed = 1 WHERE dismissed = 0")
            return cursor.rowcount

    def delete_all_alerts(self) -> int:
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM alert_history").fetchone()["count"]
            db.execute("DELETE FROM alert_history")
            return int(count)

    def delete_all_objects(self) -> int:
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM detections").fetchone()["count"]
            db.execute("DELETE FROM detections")
            return int(count)

    def get_setting(self, key: str) -> Any | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            return json.loads(row["value"]) if row else None

    def has_setting(self, key: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM app_settings WHERE key = ?", (key,)).fetchone()
            return row is not None

    def set_setting(self, key: str, value: Any, updated_at: str) -> Any:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), updated_at),
            )
        return value

    def add_audit_log(
        self,
        *,
        created_at: str,
        user_id: int | None,
        username: str,
        action: str,
        resource: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
        status: str = 'success',
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO audit_log (created_at, user_id, username, action, resource, resource_id, details, ip_address, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, user_id, username, action, resource, resource_id, json.dumps(details or {}), ip_address, status),
            )

    @staticmethod
    def _audit_log_filter(
        action: str | None,
        username: str | None,
        resource: str | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if action:
            conditions.append("action = ?")
            params.append(action)
        if username:
            conditions.append("username = ?")
            params.append(username)
        if resource:
            conditions.append("resource LIKE ?")
            params.append(f"{resource}%")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where, params

    def list_audit_logs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        username: str | None = None,
        resource: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._audit_log_filter(action, username, resource)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM audit_log {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry['details'] = json.loads(entry.get('details') or '{}')
            result.append(entry)
        return result

    def count_audit_logs(
        self,
        *,
        action: str | None = None,
        username: str | None = None,
        resource: str | None = None,
    ) -> int:
        where, params = self._audit_log_filter(action, username, resource)
        with self.connect() as db:
            row = db.execute(f"SELECT COUNT(*) AS count FROM audit_log {where}", params).fetchone()
            return int(row['count'])

    def _event_with_detections(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        detections = db.execute("SELECT * FROM detections WHERE event_id = ? ORDER BY confidence DESC", (row["id"],)).fetchall()
        recordings = db.execute(
            """
            SELECT DISTINCT r.*
            FROM recordings r
            WHERE r.event_id = ?
               OR r.id IN (
                    SELECT ah.recording_id
                    FROM alert_history ah
                    WHERE ah.event_id = ?
                      AND ah.recording_id IS NOT NULL
               )
            ORDER BY r.started_at DESC
            """,
            (row["id"], row["id"]),
        ).fetchall()
        event = dict(row)
        event["metadata"] = json.loads(event.get("metadata") or "{}")
        event["detections"] = [dict(detection) for detection in detections]
        event["recordings"] = [self._recording_row(recording) for recording in recordings]
        if event["recordings"]:
            label_map = self._fetch_labels_for_recordings(db, [int(rec["id"]) for rec in event["recordings"]])
            for recording in event["recordings"]:
                recording["labels"] = label_map.get(int(recording["id"]), [])
        else:
            event["recordings"] = []
        event["recording_status"] = "linked" if recordings else "none"
        return event

    def _assemble_recordings(self, db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Assemble recordings with labels, events, and detections using batch IN-clause queries."""
        if not rows:
            return []
        recordings = [self._recording_row(row) for row in rows]
        recording_ids = [int(r['id']) for r in recordings]

        labels_map = self._fetch_labels_for_recordings(db, recording_ids)

        event_ids = [int(r['event_id']) for r in recordings if r.get('event_id') is not None]
        events_map: dict[int, Any] = {}
        detections_map: dict[int, list[dict[str, Any]]] = {}
        if event_ids:
            placeholders = ','.join('?' * len(event_ids))
            event_rows = db.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders})",
                event_ids,
            ).fetchall()
            for event_row in event_rows:
                event = dict(event_row)
                event['metadata'] = json.loads(event.get('metadata') or '{}')
                events_map[int(event['id'])] = event
            det_rows = db.execute(
                f"SELECT * FROM detections WHERE event_id IN ({placeholders}) ORDER BY confidence DESC",
                event_ids,
            ).fetchall()
            for det_row in det_rows:
                eid = int(det_row['event_id'])
                detections_map.setdefault(eid, []).append(dict(det_row))

        for recording in recordings:
            recording['labels'] = labels_map.get(int(recording['id']), [])
            recording['event'] = None
            recording['detections'] = []
            if recording.get('event_id') is not None:
                eid = int(recording['event_id'])
                recording['event'] = events_map.get(eid)
                recording['detections'] = detections_map.get(eid, [])
        return recordings

    @staticmethod
    def _fetch_labels_for_recordings(db: sqlite3.Connection, recording_ids: list[int]) -> dict[int, list[str]]:
        if not recording_ids:
            return {}
        placeholders = ','.join('?' * len(recording_ids))
        rows = db.execute(
            f"SELECT recording_id, label FROM recording_labels WHERE recording_id IN ({placeholders}) ORDER BY label ASC",
            [int(rid) for rid in recording_ids],
        ).fetchall()
        grouped: dict[int, list[str]] = {int(rid): [] for rid in recording_ids}
        for row in rows:
            grouped[int(row['recording_id'])].append(str(row['label']))
        return grouped

    def _recording_row(self, row: sqlite3.Row) -> dict[str, Any]:
        recording = dict(row)
        file_path = Path(str(recording.get("file_path") or ""))
        recording["media_ready"] = file_path.exists() and file_path.is_file() and file_path.stat().st_size > 0
        return recording

    def _recording_with_event(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        recording = self._recording_row(row)
        recording["event"] = None
        recording["detections"] = []
        label_rows = db.execute(
            "SELECT label, source FROM recording_labels WHERE recording_id = ? ORDER BY label ASC",
            (recording["id"],),
        ).fetchall()
        recording["labels"] = [str(label_row["label"]) for label_row in label_rows]
        if recording.get("event_id") is not None:
            event_row = db.execute("SELECT * FROM events WHERE id = ?", (recording["event_id"],)).fetchone()
            detections = db.execute(
                "SELECT * FROM detections WHERE event_id = ? ORDER BY confidence DESC", (recording["event_id"],)
            ).fetchall()
            if event_row:
                event = dict(event_row)
                event["metadata"] = json.loads(event.get("metadata") or "{}")
                recording["event"] = event
            recording["detections"] = [dict(detection) for detection in detections]
        return recording
