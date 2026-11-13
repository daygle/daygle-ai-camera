from __future__ import annotations

from typing import Any

from app.utils import _normalize_iso_to_utc


class AlertsMixin:
    """CRUD + dismiss helpers for the ``alert_history`` table.

    Lives in app.db.alerts so the EventDatabase class in app.database.py stays
    small. Public method names + signatures are unchanged.
    """

    def add_alert(self, created_at: str, rule_name: str, event_id: int, label: str, confidence: float, message: str, recording_id: int | None = None) -> None:
        with self.write_slot(), self.connect() as db:
            db.execute(
                """
                INSERT INTO alert_history (created_at, rule_name, event_id, label, confidence, message, recording_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, rule_name, event_id, label, confidence, message, recording_id),
            )

    def alerts(self, limit: int = 25, since: str | None = None) -> list[dict[str, Any]]:
        # Normalise the since bound to canonical UTC ``+00:00`` form (rows are
        # stored via ``add_alert`` with ``datetime.now(timezone.utc).isoformat()``
        # which already yields ``+00:00``, but the frontend sends local-day-start
        # bounds built with ``Date.toISOString()`` -- a ``Z`` suffix). Lexically
        # ``Z`` (0x5A) sorts AFTER ``+`` (0x2B) and ``.`` (0x2E) sorts AFTER
        # ``+``, so a raw Z-form bound at the exact local-midnight boundary
        # would exclude the row that represents that same instant -- the same
        # failure mode ``RecordingsMixin.list_recordings_page`` guards against.
        since = _normalize_iso_to_utc(since) if since else None
        with self.connect() as db:
            # Dropping the ``dismissed`` pre-filter left this query with no
            # other predicate, so the since bound must open the WHERE itself.
            # An AND-shaped fragment here would bind into the LEFT JOIN's ON
            # condition and silently stop filtering anything.
            since_where = "WHERE ah.created_at >= ?" if since else ""
            params: tuple[Any, ...] = (since, limit) if since else (limit,)
            rows = db.execute(
                f"""
                SELECT ah.*,
                       r.id AS recording_id,
                       json_extract(e.metadata, '$.camera_name') AS camera_name,
                       json_extract(e.metadata, '$.camera_id') AS camera_id
                FROM alert_history ah
                LEFT JOIN events e ON e.id = ah.event_id
                LEFT JOIN recordings r ON r.id = ah.recording_id
                {since_where}
                ORDER BY ah.created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_all_alerts(self) -> int:
        with self.write_slot(), self.connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM alert_history").fetchone()["count"]
            db.execute("DELETE FROM alert_history")
            return int(count)

    # -- Alert delivery outbox -------------------------------------------
    # Rows are deliveries the mail relay or ntfy server never accepted (see
    # app.alert_outbox). One row is one rule's batch for one channel, so a
    # retry repeats exactly a delivery that delivered nothing.

    def enqueue_alert_delivery(
        self, *, created_at: str, event_id: int, channel: str,
        dedupe_key: str, payload: str, next_attempt_at: float,
    ) -> bool:
        """Insert one failed delivery; False when it is already queued."""
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO alert_delivery_outbox
                    (created_at, event_id, channel, dedupe_key, payload, attempts, next_attempt_at)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (created_at, int(event_id), channel, dedupe_key, payload, float(next_attempt_at)),
            )
            return cursor.rowcount > 0

    def due_alert_deliveries(self, now: float, limit: int = 20) -> list[dict[str, Any]]:
        """Queued deliveries whose next attempt is due, oldest first."""
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM alert_delivery_outbox
                WHERE next_attempt_at <= ?
                ORDER BY next_attempt_at, id
                LIMIT ?
                """,
                (float(now), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_due_alert_deliveries(
        self, now: float, limit: int = 20, *, lease_seconds: float = 120.0,
    ) -> list[dict[str, Any]]:
        """Atomically take the due deliveries this drainer will attempt.

        ``due_alert_deliveries`` is a plain SELECT, so two drainers (a second
        process, or an admin retry racing the background worker) could both pick
        the same row and deliver the alert twice. This statement moves
        ``next_attempt_at`` forward as it selects, so a row belongs to exactly
        one drainer; if that attempt dies, the lease expires and the row is
        retried later (at-least-once delivery, which alerting already was).
        """
        with self.write_slot(), self.connect() as db:
            rows = db.execute(
                """
                UPDATE alert_delivery_outbox
                SET next_attempt_at = ?
                WHERE id IN (
                    SELECT id FROM alert_delivery_outbox
                    WHERE next_attempt_at <= ?
                    ORDER BY next_attempt_at, id
                    LIMIT ?
                )
                RETURNING *
                """,
                (
                    float(now) + max(0.0, float(lease_seconds)),
                    float(now), int(limit),
                ),
            ).fetchall()
            return sorted((dict(row) for row in rows), key=lambda row: int(row['id']))

    def reschedule_alert_delivery(
        self, row_id: int, attempts: int, next_attempt_at: float, last_error: str,
    ) -> None:
        with self.write_slot(), self.connect() as db:
            db.execute(
                """
                UPDATE alert_delivery_outbox
                SET attempts = ?, next_attempt_at = ?, last_error = ?
                WHERE id = ?
                """,
                (int(attempts), float(next_attempt_at), last_error, int(row_id)),
            )

    def delete_alert_delivery(self, row_id: int) -> None:
        with self.write_slot(), self.connect() as db:
            db.execute("DELETE FROM alert_delivery_outbox WHERE id = ?", (int(row_id),))

    def alert_deliveries(self, limit: int = 100) -> list[dict[str, Any]]:
        """Queued deliveries, newest first, for the admin outbox view."""
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM alert_delivery_outbox
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def alert_delivery(self, row_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM alert_delivery_outbox WHERE id = ?", (int(row_id),),
            ).fetchone()
            return dict(row) if row else None

    def count_alert_deliveries(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM alert_delivery_outbox").fetchone()
            return int(row["count"]) if row else 0

    def request_all_alert_delivery_retries(self, now: float) -> int:
        """Make every queued delivery due now; returns how many rows."""
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                "UPDATE alert_delivery_outbox SET next_attempt_at = ?",
                (float(now),),
            )
            return int(cursor.rowcount or 0)

    def prune_alert_deliveries(self, older_than: str) -> int:
        """Drop rows created before the ISO-8601 ``older_than`` bound."""
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                "DELETE FROM alert_delivery_outbox WHERE created_at < ?",
                (older_than,),
            )
            return int(cursor.rowcount or 0)

    def delete_all_objects(self) -> int:
        """Truncate the detections table. Kept alongside alert housekeeping ops
        because it's exposed via the same ``Reset operational data`` admin
        action as ``delete_all_events`` and ``delete_all_alerts``.
        """
        with self.write_slot(), self.connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM detections").fetchone()["count"]
            db.execute("DELETE FROM detections")
            return int(count)
