from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

from app.detection_status import GENERIC_TRIGGER_LABELS
from app.utils import _normalize_iso_to_utc

_SQLITE_BATCH_SIZE = 400


def _batched(values: list[int]) -> list[list[int]]:
    return [values[index:index + _SQLITE_BATCH_SIZE] for index in range(0, len(values), _SQLITE_BATCH_SIZE)]


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
        recording_id: int | None = None,
        thumbnail_path: str | None = None,
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
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO events (created_at, source, snapshot_path, thumbnail_path, alert_triggered, recording_id, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, source, snapshot_path, thumbnail_path, int(alert_triggered), recording_id, json.dumps(metadata or {})),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Failed to create event row")
            event_id = cursor.lastrowid
            for detection in detections:
                box = detection.get("box", {})
                db.execute(
                    """
                    INSERT INTO detections (event_id, label, confidence, x, y, width, height, zone_name, still_alert, still_alert_minutes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        int(bool(detection.get("still_alert"))),
                        detection.get("still_alert_minutes") or None,
                    ),
                )
            return event_id

    def add_event_with_alerts(
        self,
        *,
        created_at: str,
        source: str,
        snapshot_path: str | None,
        detections: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        alert_triggered: bool = False,
        metadata: dict[str, Any] | None = None,
        thumbnail_path: str | None = None,
    ) -> int:
        """Atomically persist an event, its detections, and alert history."""
        created_at = _normalize_iso_to_utc(created_at) or created_at
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                """INSERT INTO events (created_at, source, snapshot_path, thumbnail_path, alert_triggered, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (created_at, source, snapshot_path, thumbnail_path, int(alert_triggered), json.dumps(metadata or {})),
            )
            event_id = int(cursor.lastrowid)
            for detection in detections:
                box = detection.get('box', {})
                db.execute(
                    """INSERT INTO detections (event_id, label, confidence, x, y, width, height, zone_name, still_alert, still_alert_minutes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, detection['label'], float(detection['confidence']), float(box.get('x', 0)), float(box.get('y', 0)), float(box.get('width', 0)), float(box.get('height', 0)), detection.get('zone_name') or None, int(bool(detection.get('still_alert'))), detection.get('still_alert_minutes') or None),
                )
            for alert in alerts:
                db.execute(
                    """INSERT INTO alert_history (created_at, rule_name, event_id, label, confidence, message)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (_normalize_iso_to_utc(str(alert.get('created_at') or created_at)) or created_at, str(alert['rule_name']), event_id, str(alert['label']), float(alert['confidence']), str(alert['message'])),
                )
            return event_id

    def set_event_recording(self, event_id: int, recording_id: int | None) -> bool:
        """Stamp an event with the recording (clip) it belongs to.

        A recording spans many events, so the live-detection path creates the
        event first (with the snapshot) and then, once the clip is resolved by
        ``attach_event_recording``, links it back here. Idempotent.
        """
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                "UPDATE events SET recording_id = ? WHERE id = ?",
                (int(recording_id) if recording_id is not None else None, int(event_id)),
            )
            if recording_id is not None:
                db.execute("UPDATE alert_history SET recording_id = ? WHERE event_id = ?", (int(recording_id), int(event_id)))
            return cursor.rowcount > 0

    def backfill_event_recording_links(self, db: sqlite3.Connection | None = None) -> int:
        """One-shot migration: populate ``events.recording_id`` for installs
        upgrading from the pre-link schema, then re-aggregate each recording's
        object labels from ALL of its linked events.

        Non-destructive: only fills rows where ``recording_id`` is NULL and never
        deletes or merges recording rows/media. Safe to call on every ``init()``
        - it is a no-op once every event that can be linked has been.

        Cross-mixin helper ``_insert_recording_labels`` is reached via MRO
        (EventDatabase inherits both EventsMixin and RecordingsMixin).
        """
        own = db is None
        if own:
            with self.write_slot(), self.connect() as conn:
                return self.backfill_event_recording_links(conn)
        # 1) The recording's declared "primary" event.
        db.execute(
            """
            UPDATE events
            SET recording_id = (
                SELECT r.id FROM recordings r
                WHERE r.event_id = events.id
                ORDER BY r.id ASC LIMIT 1
            )
            WHERE recording_id IS NULL
              AND EXISTS (SELECT 1 FROM recordings r WHERE r.event_id = events.id)
            """
        )
        # 2) An event that fired an alert tied to a recording.
        db.execute(
            """
            UPDATE events
            SET recording_id = (
                SELECT ah.recording_id FROM alert_history ah
                WHERE ah.event_id = events.id AND ah.recording_id IS NOT NULL
                ORDER BY ah.id ASC LIMIT 1
            )
            WHERE recording_id IS NULL
              AND EXISTS (
                SELECT 1 FROM alert_history ah
                WHERE ah.event_id = events.id AND ah.recording_id IS NOT NULL
              )
            """
        )
        # 3) Time-window overlap on the same camera: an event whose created_at
        #    falls inside a recording's [started_at, ended_at] and whose camera
        #    matches is treated as belonging to that clip. Conservative - the
        #    camera predicate rejects a recording that names a camera the event
        #    does not, and only NULL links are filled.
        window_predicate = """
            events.created_at >= r.started_at
            AND events.created_at <= COALESCE(r.ended_at, r.started_at)
            AND (
                r.camera_id IS NULL
                OR r.camera_id = json_extract(events.metadata, '$.camera_id')
            )
        """
        db.execute(
            f"""
            UPDATE events
            SET recording_id = (
                SELECT r.id FROM recordings r
                WHERE {window_predicate}
                ORDER BY r.started_at DESC, r.id DESC LIMIT 1
            )
            WHERE recording_id IS NULL
              AND EXISTS (SELECT 1 FROM recordings r WHERE {window_predicate})
            """
        )
        linked = db.execute(
            "SELECT COUNT(*) AS c FROM events WHERE recording_id IS NOT NULL"
        ).fetchone()["c"]
        # Re-aggregate recording_labels from every linked event's detections so
        # the recordings list shows all objects across the clip, not just those
        # from its primary event. Generic markers (motion/alert/...) are skipped
        # to mirror ``backfill_recording_labels``.
        rows = db.execute(
            """
            SELECT e.recording_id AS recording_id, d.label AS label, MAX(d.confidence) AS confidence
            FROM events e
            JOIN detections d ON d.event_id = e.id
            WHERE e.recording_id IS NOT NULL
            GROUP BY e.recording_id, d.label
            """
        ).fetchall()
        by_rec_labels: dict[int, list[str]] = defaultdict(list)
        by_rec_conf: dict[int, dict[str, float]] = defaultdict(dict)
        for row in rows:
            label = str(row["label"] or "").strip().lower()
            if not label or label in GENERIC_TRIGGER_LABELS:
                continue
            rid = int(row["recording_id"])
            by_rec_labels[rid].append(label)
            if row["confidence"] is not None:
                by_rec_conf[rid][label] = float(row["confidence"])
        for rid, labels in by_rec_labels.items():
            self._insert_recording_labels(db, rid, labels, source="backfill", confidences=by_rec_conf.get(rid))
        return int(linked)

    @staticmethod
    def _purge_event_children(db: sqlite3.Connection, event_ids: list[int]) -> None:
        """Remove rows that reference the given events, mirroring the schema's
        declared ``ON DELETE CASCADE`` (detections, alert_history) and
        ``ON DELETE SET NULL`` (recordings.event_id). SQLite does not enforce
        those actions because ``PRAGMA foreign_keys`` is off per connection, so
        without this a deleted event orphans its detections and leaves its
        alert_history rows -- which still surface in ``/api/alerts``. Call inside
        the same transaction, BEFORE deleting the ``events`` rows."""
        for batch in _batched([int(event_id) for event_id in event_ids]):
            placeholders = ','.join('?' * len(batch))
            db.execute(f"DELETE FROM detections WHERE event_id IN ({placeholders})", batch)
            db.execute(f"DELETE FROM alert_history WHERE event_id IN ({placeholders})", batch)
            db.execute(f"UPDATE recordings SET event_id = NULL WHERE event_id IN ({placeholders})", batch)

    def delete_event(self, event_id: int) -> dict[str, Any] | None:
        with self.write_slot(), self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                return None
            event = dict(row)
            event["metadata"] = json.loads(event.get("metadata") or "{}")
            self._purge_event_children(db, [int(event_id)])
            db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            return event

    def delete_all_events(self) -> int:
        with self.write_slot(), self.connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
            # Mirror the declared CASCADE / SET NULL (foreign_keys is off): every
            # detection and alert_history row references an event, so clear them,
            # and detach any recordings that pointed at a now-deleted event.
            db.execute("DELETE FROM detections")
            db.execute("DELETE FROM alert_history")
            db.execute("UPDATE recordings SET event_id = NULL")
            db.execute("DELETE FROM events")
            return int(count)

    def search_events(
        self,
        label: str | None = None,
        limit: int = 50,
        alerted_only: bool = False,
        with_recording: bool = False,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one event page using the historical list-only contract."""
        items, _next = self.search_events_page(
            label=label,
            limit=limit,
            alerted_only=alerted_only,
            with_recording=with_recording,
            since=since,
        )
        return items

    def search_events_page(
        self,
        label: str | None = None,
        limit: int = 50,
        alerted_only: bool = False,
        with_recording: bool = False,
        since: str | None = None,
        *,
        cursor: tuple[str, int] | None = None,
        owner_user_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], tuple[str, int] | None]:
        """Return ``(events, next_raw_cursor)`` ordered by ``(created_at, id)``.

        Fetching one extra row keeps cursor generation independent of whether the
        requested page happens to end exactly at a filter boundary. ``owner_user_id``
        applies the viewer's recording scope in SQL so a full page is never shortened
        after privacy filtering (which would look like the end of the list).
        """
        since = _normalize_iso_to_utc(since) if since else None
        page_size = max(1, int(limit))
        with self.connect() as db:
            conditions = ['e.dismissed = 0']
            params: list[Any] = []
            if label:
                conditions.append('EXISTS (SELECT 1 FROM detections d WHERE d.event_id = e.id AND d.label = ?)')
                params.append(label)
            if since:
                conditions.append('e.created_at >= ?')
                params.append(since)
            if alerted_only:
                conditions.append('EXISTS (SELECT 1 FROM alert_history ah WHERE ah.event_id = e.id)')
            recording_links = """
                r.id = e.recording_id
                OR r.event_id = e.id
                OR EXISTS (
                    SELECT 1 FROM alert_history ah
                    WHERE ah.event_id = e.id AND ah.recording_id = r.id
                )
            """
            if with_recording:
                conditions.append(f'EXISTS (SELECT 1 FROM recordings r WHERE {recording_links})')
            if owner_user_id is not None:
                conditions.append(
                    'NOT EXISTS (SELECT 1 FROM recordings r '
                    f'WHERE ({recording_links}) AND r.owner_user_id IS NOT NULL '
                    'AND r.owner_user_id != ?)'
                )
                params.append(int(owner_user_id))
            if cursor is not None:
                conditions.append('(e.created_at, e.id) < (?, ?)')
                params.extend((cursor[0], int(cursor[1])))

            rows = db.execute(
                f"""
                SELECT e.* FROM events e
                WHERE {' AND '.join(conditions)}
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                [*params, page_size + 1],
            ).fetchall()
            has_more = len(rows) > page_size
            visible_rows = rows[:page_size]
            next_cursor = (
                (str(visible_rows[-1]['created_at']), int(visible_rows[-1]['id']))
                if has_more and visible_rows
                else None
            )
            return self._events_with_detections(db, visible_rows), next_cursor

    def list_snapshots(self, limit: int = 10000, since: str | None = None) -> list[dict[str, Any]]:
        """Return one snapshot page using the historical list-only contract."""
        items, _next = self.list_snapshots_page(limit=limit, since=since)
        return items

    def list_snapshots_page(
        self,
        limit: int = 10000,
        since: str | None = None,
        *,
        cursor: tuple[str, int] | None = None,
        owner_user_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], tuple[str, int] | None]:
        """Return a stable newest-first snapshot page and its next raw cursor."""
        since = _normalize_iso_to_utc(since) if since else None
        page_size = max(1, int(limit))
        with self.connect() as db:
            conditions = [
                'e.dismissed = 0',
                'e.snapshot_path IS NOT NULL',
                "e.snapshot_path != ''",
            ]
            params: list[Any] = []
            if since:
                conditions.append('e.created_at >= ?')
                params.append(since)
            recording_links = """
                r.id = e.recording_id
                OR r.event_id = e.id
                OR EXISTS (
                    SELECT 1 FROM alert_history ah
                    WHERE ah.event_id = e.id AND ah.recording_id = r.id
                )
            """
            if owner_user_id is not None:
                conditions.append(
                    'NOT EXISTS (SELECT 1 FROM recordings r '
                    f'WHERE ({recording_links}) AND r.owner_user_id IS NOT NULL '
                    'AND r.owner_user_id != ?)'
                )
                params.append(int(owner_user_id))
            if cursor is not None:
                conditions.append('(e.created_at, e.id) < (?, ?)')
                params.extend((cursor[0], int(cursor[1])))

            rows = db.execute(
                f"""
                SELECT e.* FROM events e
                WHERE {' AND '.join(conditions)}
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                [*params, page_size + 1],
            ).fetchall()
            has_more = len(rows) > page_size
            visible_rows = rows[:page_size]
            next_cursor = (
                (str(visible_rows[-1]['created_at']), int(visible_rows[-1]['id']))
                if has_more and visible_rows
                else None
            )
            return self._events_with_detections(db, visible_rows), next_cursor

    def clear_event_snapshot(self, event_id: int) -> bool:
        """Detach a stored snapshot from its event (Snapshots-library delete).

        Only clears the ``snapshot_path`` / ``thumbnail_path`` columns; the
        router removes the image file itself via ``safe_storage_path``. The
        event row (and any linked recording) is left intact, so after this the
        event simply stops advertising ``has_snapshot`` and the snapshot
        endpoint returns 404 - distinct from ``delete_event`` which removes
        the whole event.
        """
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                "UPDATE events SET snapshot_path = NULL, thumbnail_path = NULL WHERE id = ?",
                (int(event_id),),
            )
            return cursor.rowcount > 0

    def purge_events_without_recordings(self, event_ids: list[int]) -> list[dict[str, Any]]:
        """Delete supplied events when no surviving recording still backs them."""
        if not event_ids:
            return []
        with self.write_slot(), self.connect() as db:
            events: list[dict[str, Any]] = []
            for candidate_ids in _batched([int(event_id) for event_id in event_ids]):
                placeholders = ','.join('?' * len(candidate_ids))
                rows = db.execute(
                    f"""
                    SELECT e.*
                    FROM events e
                    WHERE e.id IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM recordings r
                          WHERE r.id = e.recording_id OR r.event_id = e.id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM alert_history ah
                          JOIN recordings r ON r.id = ah.recording_id
                          WHERE ah.event_id = e.id
                      )
                    ORDER BY e.created_at ASC, e.id ASC
                    """,
                    candidate_ids,
                ).fetchall()
                for row in rows:
                    event = dict(row)
                    event['metadata'] = json.loads(event.get('metadata') or '{}')
                    events.append(event)
            purged_ids = [int(event['id']) for event in events]
            self._purge_event_children(db, purged_ids)
            for batch in _batched(purged_ids):
                placeholders = ','.join('?' * len(batch))
                db.execute(f"DELETE FROM events WHERE id IN ({placeholders})", batch)
            return events

    def purge_expired_events_without_recordings(self, older_than: str) -> list[dict[str, Any]]:
        """Delete old events that no longer have any associated recording media.

        This handles frameless sound events and snapshot-only events after the
        corresponding recording rows have already been removed by retention.
        """
        older_than = _normalize_iso_to_utc(older_than) or older_than
        with self.write_slot(), self.connect() as db:
            rows = [dict(row) for row in db.execute(
                """
                SELECT e.*
                FROM events e
                WHERE e.created_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM recordings r
                      WHERE r.id = e.recording_id OR r.event_id = e.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM alert_history ah
                      JOIN recordings r ON r.id = ah.recording_id
                      WHERE ah.event_id = e.id
                  )
                ORDER BY e.created_at ASC, e.id ASC
                """,
                (older_than,),
            ).fetchall()]
            if not rows:
                return []
            events = []
            for row in rows:
                event = dict(row)
                event['metadata'] = json.loads(event.get('metadata') or '{}')
                events.append(event)
            event_ids = [int(event['id']) for event in events]
            self._purge_event_children(db, event_ids)
            for batch in _batched(event_ids):
                placeholders = ','.join('?' * len(batch))
                db.execute(f"DELETE FROM events WHERE id IN ({placeholders})", batch)
            return events

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
        with self.write_slot(), self.connect() as db:
            cursor = db.execute("UPDATE events SET dismissed = 1 WHERE id = ?", (event_id,))
            return cursor.rowcount > 0

    def dismiss_all_events(self) -> int:
        with self.write_slot(), self.connect() as db:
            cursor = db.execute("UPDATE events SET dismissed = 1 WHERE dismissed = 0")
            return cursor.rowcount

    def _event_with_detections(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        """Hydrate one event through the shared batched list hydrator."""
        return self._events_with_detections(db, [row])[0]

    def _events_with_detections(
        self,
        db: sqlite3.Connection,
        rows: list[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        """Hydrate event lists with a fixed number of batched queries.

        The previous per-row implementation issued several queries for every
        event. These IN-clause batches keep list cost constant as pages grow and
        preserve the existing payload shape, including strongest alert and all
        directly/alert-linked recordings.
        """
        if not rows:
            return []
        events = {int(row['id']): dict(row) for row in rows}
        for event in events.values():
            event['metadata'] = json.loads(event.get('metadata') or '{}')
            event['detections'] = []
            event['has_snapshot'] = bool(event.get('snapshot_path'))
            event['alert'] = None
            event['recordings'] = []
            event['recording_status'] = 'none'

        event_ids = list(events)
        recording_ids_by_event: dict[int, set[int]] = defaultdict(set)
        for row in rows:
            if row['recording_id'] is not None:
                recording_ids_by_event[int(row['id'])].add(int(row['recording_id']))

        for event_ids_batch in _batched(event_ids):
            placeholders = ','.join('?' * len(event_ids_batch))
            for detection in db.execute(
                f'SELECT * FROM detections WHERE event_id IN ({placeholders}) ORDER BY confidence DESC',
                event_ids_batch,
            ).fetchall():
                events[int(detection['event_id'])]['detections'].append(dict(detection))

            strongest_alerts: dict[int, sqlite3.Row] = {}
            for alert in db.execute(
                f'SELECT * FROM alert_history WHERE event_id IN ({placeholders}) ORDER BY confidence DESC, id ASC',
                event_ids_batch,
            ).fetchall():
                strongest_alerts.setdefault(int(alert['event_id']), alert)
            for event_id, alert in strongest_alerts.items():
                events[event_id]['alert'] = dict(alert)

            for linked in db.execute(
                f'SELECT id AS recording_id, event_id FROM recordings WHERE event_id IN ({placeholders})',
                event_ids_batch,
            ).fetchall():
                if linked['recording_id'] is not None:
                    recording_ids_by_event[int(linked['event_id'])].add(int(linked['recording_id']))
            for linked in db.execute(
                f'''SELECT DISTINCT event_id, recording_id FROM alert_history
                    WHERE event_id IN ({placeholders}) AND recording_id IS NOT NULL''',
                event_ids_batch,
            ).fetchall():
                recording_ids_by_event[int(linked['event_id'])].add(int(linked['recording_id']))

        all_recording_ids = sorted({
            recording_id
            for linked_ids in recording_ids_by_event.values()
            for recording_id in linked_ids
        })
        recordings_by_id: dict[int, dict[str, Any]] = {}
        for recording_ids in _batched(all_recording_ids):
            placeholders = ','.join('?' * len(recording_ids))
            label_map, confidence_map = self._fetch_labels_for_recordings(db, recording_ids)
            for recording in db.execute(
                f'SELECT * FROM recordings WHERE id IN ({placeholders}) ORDER BY started_at DESC, id DESC',
                recording_ids,
            ).fetchall():
                recording_id = int(recording['id'])
                item = self._recording_row(recording)
                item['labels'] = label_map.get(recording_id, [])
                item['label_confidences'] = confidence_map.get(recording_id, {})
                recordings_by_id[recording_id] = item

        for event_id in event_ids:
            linked = recordings_by_id
            event = events[event_id]
            event['recordings'] = [
                linked[recording_id]
                for recording_id in sorted(
                    recording_ids_by_event.get(event_id, set()),
                    key=lambda rid: (str(linked[rid].get('started_at') or ''), rid),
                    reverse=True,
                )
                if recording_id in linked
            ]
            event['recording_status'] = 'linked' if event['recordings'] else 'none'
        return [events[event_id] for event_id in event_ids]
