from __future__ import annotations

from typing import Any


class AlertsMixin:
    """CRUD + dismiss helpers for the ``alert_history`` table.

    Lives in app.db.alerts so the EventDatabase class in app.database.py stays
    small. Public method names + signatures are unchanged.
    """

    def add_alert(self, created_at: str, rule_name: str, event_id: int, label: str, confidence: float, message: str, recording_id: int | None = None) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO alert_history (created_at, rule_name, event_id, label, confidence, message, recording_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, rule_name, event_id, label, confidence, message, recording_id),
            )

    def alerts(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT ah.*,
                       r.id AS recording_id,
                       json_extract(e.metadata, '$.camera_name') AS camera_name,
                       json_extract(e.metadata, '$.camera_id') AS camera_id
                FROM alert_history ah
                LEFT JOIN events e ON e.id = ah.event_id
                LEFT JOIN recordings r ON r.id = ah.recording_id
                WHERE ah.dismissed = 0
                ORDER BY ah.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

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
        """Truncate the detections table. Kept alongside alert housekeeping ops
        because it's exposed via the same ``Reset operational data`` admin
        action as ``delete_all_events`` and ``delete_all_alerts``.
        """
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM detections").fetchone()["count"]
            db.execute("DELETE FROM detections")
            return int(count)
