from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.utils import _normalize_iso_to_utc


class EventsMixin:
    """CRUD + query helpers for the ``events`` and ``detections`` tables.

    Lives in app.db.events so the EventDatabase class in app.database.py stays
    small. Public method names + signatures are unchanged; any method already on
    EventDatabase is still callable with the same arguments.
    """

    def add_event(
        self,
        created_at: str,
        source: str,
        snapshot_path: str | None,
        detections: list[dict[str, Any]],
        alert_triggered: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        # Coerce ``created_at`` to canonical UTC ``+00:00`` before binding
        # so the storage form is consistent across every event row. There is
        # no current lexical TIMING compare on ``events.created_at`` (no
        # age-based purge), but the column participates in five
        # ``ORDER BY e.created_at DESC`` sites and any future where-bound
        # search window. Storing the canonical form now means a future
        # query using the same helper normalising its bound value still
        # sorts and compares correctly.
        created_at = _normalize_iso_to_utc(created_at) or created_at
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

    @staticmethod
    def _purge_event_children(db: sqlite3.Connection, event_ids: list[int]) -> None:
        """Remove rows that reference the given events, mirroring the schema's
        declared ``ON DELETE CASCADE`` (detections, alert_history) and
        ``ON DELETE SET NULL`` (recordings.event_id). SQLite does not enforce
        those actions because ``PRAGMA foreign_keys`` is off per connection, so
        without this a deleted event orphans its detections and leaves its
        alert_history rows -- which still surface in ``/api/alerts``. Call inside
        the same transaction, BEFORE deleting the ``events`` rows."""
        if not event_ids:
            return
        placeholders = ','.join('?' * len(event_ids))
        params = [int(eid) for eid in event_ids]
        db.execute(f"DELETE FROM detections WHERE event_id IN ({placeholders})", params)
        db.execute(f"DELETE FROM alert_history WHERE event_id IN ({placeholders})", params)
        db.execute(f"UPDATE recordings SET event_id = NULL WHERE event_id IN ({placeholders})", params)

    def delete_event(self, event_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                return None
            event = dict(row)
            event["metadata"] = json.loads(event.get("metadata") or "{}")
            self._purge_event_children(db, [int(event_id)])
            db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            return event

    def delete_all_events(self) -> int:
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
            # Mirror the declared CASCADE / SET NULL (foreign_keys is off): every
            # detection and alert_history row references an event, so clear them,
            # and detach any recordings that pointed at a now-deleted event.
            db.execute("DELETE FROM detections")
            db.execute("DELETE FROM alert_history")
            db.execute("UPDATE recordings SET event_id = NULL")
            db.execute("DELETE FROM events")
            return int(count)

    def search_events(self, label: str | None = None, limit: int = 50, alerted_only: bool = False, with_recording: bool = False, since: str | None = None) -> list[dict[str, Any]]:
        # Normalise the since bound to canonical UTC ``+00:00`` form (events are
        # stored canonical after ``add_event``; the frontend sends local-day-start
        # bounds built with ``Date.toISOString()`` which carry a ``Z`` suffix).
        # Without this, the lexical ``e.created_at >= ?`` compare mis-sorts at
        # the exact boundary -- ``Z`` (0x5A) sorts AFTER ``+`` (0x2B) -- so an
        # event at local midnight would be dropped for timezones ahead of UTC.
        since = _normalize_iso_to_utc(since) if since else None
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
            since_clause = "AND e.created_at >= ?" if since else ""
            if label:
                params: tuple[Any, ...] = (label,) + ((since,) if since else ()) + (limit,)
                rows = db.execute(
                    f"""
                    SELECT DISTINCT e.* FROM events e
                    JOIN detections d ON d.event_id = e.id
                    WHERE d.label = ?
                    AND e.dismissed = 0
                    {since_clause}
                    {alert_filter if alerted_only else ''}
                    {recording_filter if with_recording else ''}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            elif alerted_only:
                params = ((since,) if since else ()) + (limit,)
                rows = db.execute(
                    f"""
                    SELECT e.* FROM events e
                    WHERE e.dismissed = 0
                    {since_clause}
                    AND EXISTS (
                        SELECT 1
                        FROM alert_history ah
                        WHERE ah.event_id = e.id
                    )
                    {recording_filter if with_recording else ''}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            elif with_recording:
                params = ((since,) if since else ()) + (limit,)
                rows = db.execute(
                    f"""
                    SELECT e.* FROM events e
                    WHERE e.dismissed = 0
                    {since_clause}
                    AND {recording_condition}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            else:
                params = ((since,) if since else ()) + (limit,)
                rows = db.execute(
                    f"SELECT * FROM events e WHERE e.dismissed = 0 {since_clause} ORDER BY e.created_at DESC LIMIT ?",
                    params,
                ).fetchall()

            return [self._event_with_detections(db, row) for row in rows]

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                return None
            return self._event_with_detections(db, row)

    def stats(self, since: str | None = None) -> dict[str, Any]:
        # Same normalisation as ``search_events`` / ``alerts``: the frontend
        # sends local-day-start bounds (``Date.toISOString()`` ``Z`` suffix)
        # that must be canonicalised to ``+00:00`` before the lexical compare
        # against stored rows so the day-boundary counts land on the right side.
        since = _normalize_iso_to_utc(since) if since else None
        with self.connect() as db:
            since_clause = "AND e.created_at >= ?" if since else ""
            since_clause_ah = "AND ah.created_at >= ?" if since else ""
            since_clause_det = "AND e.created_at >= ?" if since else ""

            # Helper to build params tuple for a given since value
            def _params(base: tuple[Any, ...] = ()) -> tuple[Any, ...]:
                return base + ((since,) if since else ())

            total_events = db.execute(
                f"SELECT COUNT(*) AS count FROM events e WHERE e.dismissed = 0 {since_clause}",
                _params(),
            ).fetchone()["count"]
            # ``alert_history`` has no other predicate, so the shared
            # ``AND ...`` since-clause needs a WHERE of its own here (every
            # other query below already opens with WHERE).
            alert_where = "WHERE ah.created_at >= ?" if since else ""
            total_alerts = db.execute(
                f"SELECT COUNT(*) AS count FROM alert_history ah {alert_where}",
                _params(),
            ).fetchone()["count"]
            sound_detection_events = db.execute(
                f"SELECT COUNT(*) AS count FROM events e WHERE e.source = 'sound' AND e.dismissed = 0 {since_clause}",
                _params(),
            ).fetchone()["count"]
            matched_object_events = db.execute(
                f"""
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
                  {since_clause}
                """,
                _params(),
            ).fetchone()["count"]
            object_alerts = db.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM alert_history ah
                WHERE ah.event_id IS NULL
                   OR ah.event_id NOT IN (SELECT id FROM events WHERE source = 'sound')
                {since_clause_ah}
                """,
                _params(),
            ).fetchone()["count"]
            sound_alerts = db.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM alert_history ah
                WHERE ah.event_id IN (SELECT id FROM events WHERE source = 'sound')
                {since_clause_ah}
                """,
                _params(),
            ).fetchone()["count"]
            labels = db.execute(
                f"""
                SELECT d.label, COUNT(*) AS count, MAX(d.confidence) AS max_confidence
                FROM detections d
                JOIN events e ON e.id = d.event_id
                WHERE e.dismissed = 0
                {since_clause_det}
                GROUP BY d.label
                ORDER BY count DESC
                """,
                _params(),
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

    def dismiss_event(self, event_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("UPDATE events SET dismissed = 1 WHERE id = ?", (event_id,))
            return cursor.rowcount > 0

    def dismiss_all_events(self) -> int:
        with self.connect() as db:
            cursor = db.execute("UPDATE events SET dismissed = 1 WHERE dismissed = 0")
            return cursor.rowcount

    def _event_with_detections(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        # Cross-mixin helpers from RecordingsMixin: reached via MRO since
        # EventDatabase inherits both EventsMixin and RecordingsMixin.
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
            label_map, confidence_map = self._fetch_labels_for_recordings(db, [int(rec["id"]) for rec in event["recordings"]])
            for recording in event["recordings"]:
                recording["labels"] = label_map.get(int(recording["id"]), [])
                recording["label_confidences"] = confidence_map.get(int(recording["id"]), {})
        else:
            event["recordings"] = []
        event["recording_status"] = "linked" if recordings else "none"
        return event
