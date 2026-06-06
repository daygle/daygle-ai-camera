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
                    trigger_type TEXT NOT NULL DEFAULT 'motion',
                    trigger_label TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recordings_event ON recordings(event_id);
                CREATE INDEX IF NOT EXISTS idx_recordings_started_at ON recordings(started_at);
                CREATE INDEX IF NOT EXISTS idx_recordings_source ON recordings(source);

                CREATE TABLE IF NOT EXISTS vehicle_plates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plate_number TEXT NOT NULL UNIQUE,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    sighting_count INTEGER NOT NULL DEFAULT 1,
                    notes TEXT,
                    is_whitelisted INTEGER NOT NULL DEFAULT 0,
                    is_blacklisted INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS plate_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    plate_id INTEGER,
                    plate_number TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    image_path TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE,
                    FOREIGN KEY(plate_id) REFERENCES vehicle_plates(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_vehicle_plates_number ON vehicle_plates(plate_number);
                CREATE INDEX IF NOT EXISTS idx_plate_events_number ON plate_events(plate_number);
                CREATE INDEX IF NOT EXISTS idx_plate_events_created ON plate_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_plate_events_event ON plate_events(event_id);

                CREATE TABLE IF NOT EXISTS plate_alert_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_name TEXT NOT NULL,
                    rule_type TEXT NOT NULL CHECK(rule_type IN ('plate', 'unknown', 'blacklisted')),
                    plate_pattern TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

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
                CREATE INDEX IF NOT EXISTS idx_plate_alert_rules_type ON plate_alert_rules(rule_type);
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

    def list_recordings(self, label: str | None = None, limit: int = 50, alerted_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            alert_filter = """
                AND EXISTS (
                    SELECT 1
                    FROM alert_history ah
                    JOIN alert_rules ar ON ar.name = ah.rule_name
                    WHERE ah.event_id = r.event_id
                    AND ar.enabled = 1
                )
            """
            if label:
                rows = db.execute(
                    f"""
                    SELECT DISTINCT r.* FROM recordings r
                    LEFT JOIN detections d ON d.event_id = r.event_id
                    WHERE d.label = ?
                    {alert_filter if alerted_only else ''}
                    ORDER BY r.started_at DESC
                    LIMIT ?
                    """,
                    (label, limit),
                ).fetchall()
            elif alerted_only:
                rows = db.execute(
                    f"""
                    SELECT r.* FROM recordings r
                    WHERE EXISTS (
                        SELECT 1
                        FROM alert_history ah
                        JOIN alert_rules ar ON ar.name = ah.rule_name
                        WHERE ah.event_id = r.event_id
                        AND ar.enabled = 1
                    )
                    ORDER BY r.started_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM recordings ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._recording_with_event(db, row) for row in rows]

    def get_recording(self, recording_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            return self._recording_with_event(db, row) if row else None

    def delete_event(self, event_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                return None
            event = dict(row)
            event["metadata"] = json.loads(event.get("metadata") or "{}")
            db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            return event

    def delete_plate(self, plate_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM vehicle_plates WHERE id = ?", (plate_id,)).fetchone()
            if row is None:
                return None
            plate = self._plate_row(row)
            db.execute("DELETE FROM plate_events WHERE plate_id = ?", (plate_id,))
            db.execute("DELETE FROM vehicle_plates WHERE id = ?", (plate_id,))
            return plate

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
                existing = [row for row in candidates if Path(str(row["file_path"])).is_file()]
                total = sum(Path(str(row["file_path"])).stat().st_size for row in existing)
                for row in existing:
                    if total <= max_storage_bytes:
                        break
                    purge_ids.add(int(row["id"]))
                    total -= Path(str(row["file_path"])).stat().st_size
            if not purge_ids:
                return []
            rows = [row for row in candidates if int(row["id"]) in purge_ids]
            db.executemany("DELETE FROM recordings WHERE id = ?", [(row["id"],) for row in rows])
            return rows

    def upsert_plate(self, plate_number: str, seen_at: str) -> dict[str, Any]:
        plate_number = plate_number.upper()
        with self.connect() as db:
            row = db.execute("SELECT * FROM vehicle_plates WHERE plate_number = ?", (plate_number,)).fetchone()
            if row:
                db.execute(
                    "UPDATE vehicle_plates SET last_seen = ?, sighting_count = sighting_count + 1 WHERE id = ?",
                    (seen_at, row["id"]),
                )
                updated = db.execute("SELECT * FROM vehicle_plates WHERE id = ?", (row["id"],)).fetchone()
                return self._plate_row(updated)
            cursor = db.execute(
                """
                INSERT INTO vehicle_plates (plate_number, first_seen, last_seen, sighting_count)
                VALUES (?, ?, ?, 1)
                """,
                (plate_number, seen_at, seen_at),
            )
            return self._plate_row(db.execute("SELECT * FROM vehicle_plates WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def add_plate_event(self, *, event_id: int, plate_id: int | None, plate_number: str, confidence: float, image_path: str | None, created_at: str) -> dict[str, Any]:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO plate_events (event_id, plate_id, plate_number, confidence, image_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, plate_id, plate_number.upper(), confidence, image_path, created_at),
            )
            return self._plate_event_with_context(db, db.execute("SELECT * FROM plate_events WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def list_plates(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM vehicle_plates ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
            return [self._plate_row(row) for row in rows]

    def get_plate(self, plate_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM vehicle_plates WHERE id = ?", (plate_id,)).fetchone()
            if row is None:
                return None
            plate = self._plate_row(row)
            events = db.execute("SELECT * FROM plate_events WHERE plate_id = ? ORDER BY created_at DESC", (plate_id,)).fetchall()
            plate["events"] = [self._plate_event_with_context(db, event) for event in events]
            return plate

    def search_plates(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        query = query.upper().strip()
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM plate_events
                WHERE plate_number LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()
            return [self._plate_event_with_context(db, row) for row in rows]

    def update_plate_status(self, plate_number: str, *, notes: str | None = None, is_whitelisted: bool | None = None, is_blacklisted: bool | None = None) -> dict[str, Any]:
        plate = self.upsert_plate(plate_number, datetime.now(timezone.utc).isoformat())
        updates: list[str] = []
        params: list[Any] = []
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)
        if is_whitelisted is not None:
            updates.append("is_whitelisted = ?")
            params.append(int(is_whitelisted))
        if is_blacklisted is not None:
            updates.append("is_blacklisted = ?")
            params.append(int(is_blacklisted))
        if updates:
            params.append(plate["id"])
            with self.connect() as db:
                db.execute(f"UPDATE vehicle_plates SET {', '.join(updates)} WHERE id = ?", params)
        return self.get_plate(int(plate["id"])) or plate

    def list_plate_alert_rules(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM plate_alert_rules ORDER BY rule_name COLLATE NOCASE").fetchall()
            return [self._plate_alert_rule(row) for row in rows]

    def create_plate_alert_rule(self, rule: dict[str, Any], now: str) -> dict[str, Any]:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO plate_alert_rules (rule_name, rule_type, plate_pattern, enabled, cooldown_seconds, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (rule["rule_name"], rule["rule_type"], rule.get("plate_pattern"), int(bool(rule["enabled"])), int(rule["cooldown_seconds"]), now, now),
            )
            return self._plate_alert_rule(db.execute("SELECT * FROM plate_alert_rules WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def update_plate_alert_rule(self, rule_id: int, rule: dict[str, Any], now: str) -> dict[str, Any] | None:
        updates: list[str] = []
        params: list[Any] = []
        for key in ("rule_name", "rule_type", "plate_pattern", "enabled", "cooldown_seconds"):
            if key in rule:
                updates.append(f"{key} = ?")
                params.append(int(bool(rule[key])) if key == "enabled" else rule[key])
        if not updates:
            with self.connect() as db:
                row = db.execute("SELECT * FROM plate_alert_rules WHERE id = ?", (rule_id,)).fetchone()
                return self._plate_alert_rule(row) if row else None
        updates.append("updated_at = ?")
        params.extend([now, rule_id])
        with self.connect() as db:
            cursor = db.execute(f"UPDATE plate_alert_rules SET {', '.join(updates)} WHERE id = ?", params)
            if cursor.rowcount == 0:
                return None
            return self._plate_alert_rule(db.execute("SELECT * FROM plate_alert_rules WHERE id = ?", (rule_id,)).fetchone())

    def delete_plate_alert_rule(self, rule_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM plate_alert_rules WHERE id = ?", (rule_id,))
            return cursor.rowcount > 0

    def add_alert(self, created_at: str, rule_name: str, event_id: int, label: str, confidence: float, message: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO alert_history (created_at, rule_name, event_id, label, confidence, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (created_at, rule_name, event_id, label, confidence, message),
            )

    def search_events(self, label: str | None = None, limit: int = 50, alerted_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            alert_filter = """
                AND EXISTS (
                    SELECT 1
                    FROM alert_history ah
                    JOIN alert_rules ar ON ar.name = ah.rule_name
                    WHERE ah.event_id = e.id
                    AND ar.enabled = 1
                )
            """
            if label:
                rows = db.execute(
                    f"""
                    SELECT DISTINCT e.* FROM events e
                    JOIN detections d ON d.event_id = e.id
                    WHERE d.label = ?
                    {alert_filter if alerted_only else ''}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    (label, limit),
                ).fetchall()
            elif alerted_only:
                rows = db.execute(
                    """
                    SELECT e.* FROM events e
                    WHERE EXISTS (
                        SELECT 1
                        FROM alert_history ah
                        JOIN alert_rules ar ON ar.name = ah.rule_name
                        WHERE ah.event_id = e.id
                        AND ar.enabled = 1
                    )
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
        plate_events = db.execute("SELECT * FROM plate_events WHERE event_id = ? ORDER BY confidence DESC", (row["id"],)).fetchall()
        event["plate_events"] = [self._plate_event_with_context(db, plate_event) for plate_event in plate_events]
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
        recording["plate_events"] = []
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
            plate_events = db.execute("SELECT * FROM plate_events WHERE event_id = ? ORDER BY confidence DESC", (recording["event_id"],)).fetchall()
            recording["plate_events"] = [self._plate_event_with_context(db, plate_event) for plate_event in plate_events]
        return recording

    def _plate_row(self, row: sqlite3.Row) -> dict[str, Any]:
        plate = dict(row)
        plate["is_whitelisted"] = bool(plate["is_whitelisted"])
        plate["is_blacklisted"] = bool(plate["is_blacklisted"])
        return plate

    def _plate_event_with_context(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        plate_event = dict(row)
        plate_event["plate"] = None
        if plate_event.get("plate_id") is not None:
            plate_row = db.execute("SELECT * FROM vehicle_plates WHERE id = ?", (plate_event["plate_id"],)).fetchone()
            if plate_row:
                plate_event["plate"] = self._plate_row(plate_row)
        event_row = db.execute("SELECT * FROM events WHERE id = ?", (plate_event["event_id"],)).fetchone()
        if event_row:
            event = dict(event_row)
            event["metadata"] = json.loads(event.get("metadata") or "{}")
            recordings = db.execute("SELECT * FROM recordings WHERE event_id = ? ORDER BY started_at DESC", (event["id"],)).fetchall()
            event["recordings"] = [self._recording_row(recording) for recording in recordings]
            plate_event["event"] = event
        else:
            plate_event["event"] = None
        return plate_event

    def _plate_alert_rule(self, row: sqlite3.Row) -> dict[str, Any]:
        rule = dict(row)
        rule["enabled"] = bool(rule["enabled"])
        return rule
