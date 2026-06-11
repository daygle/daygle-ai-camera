from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
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
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    snapshot_path TEXT,
                    thumbnail_path TEXT,
                    alert_triggered INTEGER DEFAULT 0,
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
                    FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
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
                    INSERT INTO detections (event_id, label, confidence, x, y, width, height)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        detection["label"],
                        float(detection["confidence"]),
                        float(box.get("x", 0)),
                        float(box.get("y", 0)),
                        float(box.get("width", 0)),
                        float(box.get("height", 0)),
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
            return cursor.lastrowid

    def list_recordings(self, label: str | None = None, camera_id: str | None = None, limit: int = 50, alerted_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            conditions: list[str] = []
            params: list[Any] = []

            if label:
                conditions.append("d.label = ?")
                params.append(label)
            if camera_id:
                conditions.append("r.camera_id = ?")
                params.append(camera_id)
            if alerted_only:
                conditions.append(
                    "EXISTS (SELECT 1 FROM alert_history ah WHERE ah.event_id = r.event_id)"
                )

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            if label:
                sql = f"SELECT DISTINCT r.* FROM recordings r LEFT JOIN detections d ON d.event_id = r.event_id {where} ORDER BY r.started_at DESC LIMIT ?"
            else:
                sql = f"SELECT r.* FROM recordings r {where} ORDER BY r.started_at DESC LIMIT ?"

            params.append(limit)
            rows = db.execute(sql, params).fetchall()
            return [self._recording_with_event(db, row) for row in rows]

    def list_recordings_for_camera_day(self, camera_id: str, day_start: str, day_end: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT DISTINCT r.*
                FROM recordings r
                LEFT JOIN events e ON e.id = r.event_id
                WHERE (
                    r.camera_id = ?
                    OR (
                        r.camera_id IS NULL
                        AND e.metadata LIKE ?
                    )
                )
                AND r.started_at < ?
                AND COALESCE(r.ended_at, r.started_at) >= ?
                ORDER BY r.started_at ASC, r.id ASC
                """,
                (camera_id, f'%"camera_id": "{camera_id}"%', day_end, day_start),
            ).fetchall()
            return [self._recording_with_event(db, row) for row in rows]

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

    def add_alert(self, created_at: str, rule_name: str, event_id: int, label: str, confidence: float, message: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO alert_history (created_at, rule_name, event_id, label, confidence, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (created_at, rule_name, event_id, label, confidence, message),
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
            recording_filter = "AND EXISTS (SELECT 1 FROM recordings WHERE recordings.event_id = e.id)"
            if label:
                rows = db.execute(
                    f"""
                    SELECT DISTINCT e.* FROM events e
                    JOIN detections d ON d.event_id = e.id
                    WHERE d.label = ?
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
                    WHERE EXISTS (
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
                    WHERE {recording_filter[4:]}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

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
            matched_object_events = db.execute(
                """
                SELECT COUNT(DISTINCT event_id) AS count
                FROM detections
                WHERE label != 'motion' AND confidence >= 0.5
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
                "objects": [dict(row) for row in labels],
            }

    def alerts(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT ah.*, r.id AS recording_id
                FROM alert_history ah
                LEFT JOIN recordings r ON r.event_id = ah.event_id
                ORDER BY ah.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

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

    def list_audit_logs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        username: str | None = None,
        resource: str | None = None,
    ) -> list[dict[str, Any]]:
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
        with self.connect() as db:
            row = db.execute(f"SELECT COUNT(*) AS count FROM audit_log {where}", params).fetchone()
            return int(row['count'])

    def _event_with_detections(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        detections = db.execute("SELECT * FROM detections WHERE event_id = ? ORDER BY confidence DESC", (row["id"],)).fetchall()
        recordings = db.execute("SELECT * FROM recordings WHERE event_id = ? ORDER BY started_at DESC", (row["id"],)).fetchall()
        event = dict(row)
        event["metadata"] = json.loads(event.get("metadata") or "{}")
        event["detections"] = [dict(detection) for detection in detections]
        event["recordings"] = [self._recording_row(recording) for recording in recordings]
        event["recording_status"] = "linked" if recordings else "none"
        return event

    def _recording_row(self, row: sqlite3.Row) -> dict[str, Any]:
        recording = dict(row)
        file_path = Path(str(recording.get("file_path") or ""))
        recording["media_ready"] = file_path.exists() and file_path.is_file() and file_path.stat().st_size > 0
        return recording

    def _recording_with_event(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        recording = self._recording_row(row)
        recording["event"] = None
        recording["detections"] = []
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
