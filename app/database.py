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
                    source TEXT NOT NULL CHECK(source IN ('mock', 'camera', 'upload', 'rtsp')),
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

                CREATE TABLE IF NOT EXISTS alert_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    object TEXT NOT NULL,
                    min_confidence REAL NOT NULL DEFAULT 0.5,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    email_enabled INTEGER NOT NULL DEFAULT 0,
                    email_recipients TEXT NOT NULL DEFAULT '[]',
                    active_start TEXT,
                    active_end TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_alert_rules_object ON alert_rules(object);
                """
            )
            self._ensure_column(db, "alert_rules", "email_enabled", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "alert_rules", "email_recipients", "TEXT NOT NULL DEFAULT '[]'")

    def _ensure_column(self, db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO recordings (event_id, camera_id, started_at, ended_at, duration_seconds, file_path, thumbnail_path, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, camera_id, started_at, ended_at, duration_seconds, file_path, thumbnail_path, source, created_at),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Failed to create recording row")
            return cursor.lastrowid

    def list_recordings(self, label: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            if label:
                rows = db.execute(
                    """
                    SELECT DISTINCT r.* FROM recordings r
                    LEFT JOIN detections d ON d.event_id = r.event_id
                    WHERE d.label = ?
                    ORDER BY r.started_at DESC
                    LIMIT ?
                    """,
                    (label, limit),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM recordings ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._recording_with_event(db, row) for row in rows]

    def get_recording(self, recording_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            return self._recording_with_event(db, row) if row else None

    def delete_recording(self, recording_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
            return dict(row)

    def add_alert(self, created_at: str, rule_name: str, event_id: int, label: str, confidence: float, message: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO alert_history (created_at, rule_name, event_id, label, confidence, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (created_at, rule_name, event_id, label, confidence, message),
            )

    def search_events(self, label: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            if label:
                rows = db.execute(
                    """
                    SELECT DISTINCT e.* FROM events e
                    JOIN detections d ON d.event_id = e.id
                    WHERE d.label = ?
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    (label, limit),
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
                "objects": [dict(row) for row in labels],
            }

    def alerts(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM alert_history ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

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

    def list_alert_rules(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM alert_rules ORDER BY name COLLATE NOCASE").fetchall()
            return [self._alert_rule(row) for row in rows]

    def seed_alert_rules(self, rules: list[dict[str, Any]], now: str) -> None:
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM alert_rules").fetchone()["count"]
            if count:
                return
            for rule in rules:
                db.execute(
                    """
                    INSERT INTO alert_rules (name, object, min_confidence, cooldown_seconds, enabled, email_enabled, email_recipients, active_start, active_end, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(rule.get("name") or rule.get("object") or "Alert"),
                        str(rule.get("object") or ""),
                        float(rule.get("min_confidence", 0.5)),
                        int(rule.get("cooldown_seconds", 60)),
                        int(bool(rule.get("enabled", True))),
                        int(bool(rule.get("email_enabled", False))),
                        json.dumps(rule.get("email_recipients", [])),
                        rule.get("active_start"),
                        rule.get("active_end"),
                        now,
                        now,
                    ),
                )

    def create_alert_rule(self, rule: dict[str, Any], now: str) -> dict[str, Any]:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO alert_rules (name, object, min_confidence, cooldown_seconds, enabled, email_enabled, email_recipients, active_start, active_end, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule["name"],
                    rule["object"],
                    float(rule["min_confidence"]),
                    int(rule["cooldown_seconds"]),
                    int(bool(rule["enabled"])),
                    int(bool(rule.get("email_enabled", False))),
                    json.dumps(rule.get("email_recipients", [])),
                    rule.get("active_start"),
                    rule.get("active_end"),
                    now,
                    now,
                ),
            )
            return self._alert_rule(db.execute("SELECT * FROM alert_rules WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def update_alert_rule(self, rule_id: int, rule: dict[str, Any], now: str) -> dict[str, Any] | None:
        updates: list[str] = []
        params: list[Any] = []
        for key in ("name", "object", "min_confidence", "cooldown_seconds", "enabled", "email_enabled", "email_recipients", "active_start", "active_end"):
            if key in rule:
                updates.append(f"{key} = ?")
                value = rule[key]
                if key in {"enabled", "email_enabled"}:
                    value = int(bool(value))
                if key == "email_recipients":
                    value = json.dumps(value)
                params.append(value)
        if not updates:
            with self.connect() as db:
                row = db.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
                return self._alert_rule(row) if row else None
        updates.append("updated_at = ?")
        params.extend([now, rule_id])
        with self.connect() as db:
            cursor = db.execute(f"UPDATE alert_rules SET {', '.join(updates)} WHERE id = ?", params)
            if cursor.rowcount == 0:
                return None
            row = db.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
            return self._alert_rule(row)

    def delete_alert_rule(self, rule_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
            return cursor.rowcount > 0

    def _alert_rule(self, row: sqlite3.Row) -> dict[str, Any]:
        rule = dict(row)
        rule["enabled"] = bool(rule["enabled"])
        rule["email_enabled"] = bool(rule.get("email_enabled"))
        rule["email_recipients"] = json.loads(rule.get("email_recipients") or "[]")
        return rule

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
        return dict(row)

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
