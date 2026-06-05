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
                    active_start TEXT,
                    active_end TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_alert_rules_object ON alert_rules(object);
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
                    INSERT INTO alert_rules (name, object, min_confidence, cooldown_seconds, enabled, active_start, active_end, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(rule.get("name") or rule.get("object") or "Alert"),
                        str(rule.get("object") or ""),
                        float(rule.get("min_confidence", 0.5)),
                        int(rule.get("cooldown_seconds", 60)),
                        int(bool(rule.get("enabled", True))),
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
                INSERT INTO alert_rules (name, object, min_confidence, cooldown_seconds, enabled, active_start, active_end, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule["name"],
                    rule["object"],
                    float(rule["min_confidence"]),
                    int(rule["cooldown_seconds"]),
                    int(bool(rule["enabled"])),
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
        for key in ("name", "object", "min_confidence", "cooldown_seconds", "enabled", "active_start", "active_end"):
            if key in rule:
                updates.append(f"{key} = ?")
                value = rule[key]
                if key == "enabled":
                    value = int(bool(value))
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
        return rule

    def _event_with_detections(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        detections = db.execute("SELECT * FROM detections WHERE event_id = ? ORDER BY confidence DESC", (row["id"],)).fetchall()
        event = dict(row)
        event["metadata"] = json.loads(event.get("metadata") or "{}")
        event["detections"] = [dict(detection) for detection in detections]
        return event
