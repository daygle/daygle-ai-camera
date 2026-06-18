from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class RecordingsMixin:
    """CRUD + query helpers for the ``recordings`` and ``recording_labels`` tables.

    Also exposes ``_recording_row``, ``_recording_with_event``,
    ``_assemble_recordings`` and ``_fetch_labels_for_recordings`` helpers.
    ``EventsMixin._event_with_detections`` (app.db.events) reaches these via
    Python's MRO when the host class inherits both mixins, so the public API is
    preserved.
    """

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
        label_confidences: dict[str, float] | None = None,
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
                detection_rows = db.execute(
                    "SELECT label, MAX(confidence) AS confidence FROM detections WHERE event_id = ? GROUP BY label",
                    (int(event_id),),
                ).fetchall()
                detection_labels = [str(row['label']).strip().lower() for row in detection_rows]
                if label_confidences is None:
                    label_confidences = {
                        str(row['label']).strip().lower(): float(row['confidence'])
                        for row in detection_rows
                        if row['confidence'] is not None
                    }
                normalized_trigger = str(trigger_label or '').strip().lower()
                labels = list(dict.fromkeys(detection_labels + ([normalized_trigger] if normalized_trigger else [])))
            if labels:
                self._insert_recording_labels(db, recording_id, labels, source='detection', confidences=label_confidences)
            return recording_id

    @staticmethod
    def _insert_recording_labels(
        db: sqlite3.Connection,
        recording_id: int,
        labels: list[str],
        *,
        source: str = 'detection',
        confidences: dict[str, float] | None = None,
    ) -> None:
        """Insert unique non-generic labels for a recording.

        Rows are keyed on the composite (recording_id, label). New labels are
        inserted; for labels that already exist the row's source is preserved but
        the stored confidence is raised to the best value ever seen, so callers
        can call this freely from extension / trigger-update paths without
        duplicating entries or losing a higher confidence captured later.
        """
        if not labels:
            return
        conf_map = {
            str(k or '').strip().lower(): float(v)
            for k, v in (confidences or {}).items()
            if v is not None
        }
        seen: set[str] = set()
        rows: list[tuple[int, str, str, str, float | None]] = []
        now = datetime.now(timezone.utc).isoformat()
        for raw in labels:
            label = str(raw or '').strip().lower()
            if not label or label in seen:
                continue
            seen.add(label)
            rows.append((int(recording_id), label, source, now, conf_map.get(label)))
        if not rows:
            return
        db.executemany(
            """
            INSERT INTO recording_labels (recording_id, label, source, created_at, confidence)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(recording_id, label) DO UPDATE SET
                confidence = CASE
                    WHEN excluded.confidence IS NOT NULL
                         AND (recording_labels.confidence IS NULL OR excluded.confidence > recording_labels.confidence)
                    THEN excluded.confidence
                    ELSE recording_labels.confidence
                END
            """,
            rows,
        )

    def add_recording_labels(
        self,
        recording_id: int,
        labels: list[str],
        *,
        source: str = 'detection',
        confidences: dict[str, float] | None = None,
    ) -> int:
        """Append unique labels to a recording's label set.

        Returns the number of rows newly inserted. Labels that already exist are
        not duplicated, but their stored confidence is still raised to any higher
        value supplied here. Safe to call from extension / trigger-update paths.
        """
        with self.connect() as db:
            existing = {
                str(row['label'])
                for row in db.execute(
                    "SELECT label FROM recording_labels WHERE recording_id = ?",
                    (int(recording_id),),
                ).fetchall()
            }
            normalized = [
                str(raw or '').strip().lower()
                for raw in labels
                if str(raw or '').strip()
            ]
            if not normalized:
                return 0
            # Upsert every supplied label so existing rows can pick up a higher
            # confidence, but only count the genuinely new ones.
            self._insert_recording_labels(db, int(recording_id), normalized, source=source, confidences=confidences)
            return len([label for label in dict.fromkeys(normalized) if label not in existing])

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
                   r.event_id,
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
                confidences: dict[str, float] = {}
                if row['event_id'] is not None:
                    confidences = {
                        str(crow['label']).strip().lower(): float(crow['confidence'])
                        for crow in db.execute(
                            "SELECT label, MAX(confidence) AS confidence FROM detections WHERE event_id = ? GROUP BY label",
                            (int(row['event_id']),),
                        ).fetchall()
                        if crow['confidence'] is not None
                    }
                self._insert_recording_labels(db, recording_id, labels, source='backfill', confidences=confidences)
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
                (camera_id, f'%\"camera_id\": \"{safe_camera_id}\"%', day_end, day_start),
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
                # Grace period: recordings created in the last 10 minutes may still be
                # written by a background capture thread. Don't treat a missing file as
                # an orphan if the record is this new — purging it would leave the file
                # on disk with no database entry once the thread finishes writing.
                grace_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
                existing_with_sizes: list[tuple[dict[str, Any], int]] = []
                for row in candidates:
                    try:
                        existing_with_sizes.append((row, Path(str(row["file_path"])).stat().st_size))
                    except OSError:
                        if str(row.get("created_at") or "") < grace_cutoff:
                            purge_ids.add(int(row["id"]))
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
            "SELECT label, source, confidence FROM recording_labels WHERE recording_id = ? ORDER BY label ASC",
            (recording["id"],),
        ).fetchall()
        recording["labels"] = [str(label_row["label"]) for label_row in label_rows]
        recording["label_confidences"] = {
            str(label_row["label"]): float(label_row["confidence"])
            for label_row in label_rows
            if label_row["confidence"] is not None
        }
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

    def _assemble_recordings(self, db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Assemble recordings with labels, events, and detections using batch IN-clause queries."""
        if not rows:
            return []
        recordings = [self._recording_row(row) for row in rows]
        recording_ids = [int(r['id']) for r in recordings]

        labels_map, confidences_map = self._fetch_labels_for_recordings(db, recording_ids)

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
            recording['label_confidences'] = confidences_map.get(int(recording['id']), {})
            recording['event'] = None
            recording['detections'] = []
            if recording.get('event_id') is not None:
                eid = int(recording['event_id'])
                recording['event'] = events_map.get(eid)
                recording['detections'] = detections_map.get(eid, [])
        return recordings

    @staticmethod
    def _fetch_labels_for_recordings(
        db: sqlite3.Connection, recording_ids: list[int]
    ) -> tuple[dict[int, list[str]], dict[int, dict[str, float]]]:
        if not recording_ids:
            return {}, {}
        placeholders = ','.join('?' * len(recording_ids))
        rows = db.execute(
            f"SELECT recording_id, label, confidence FROM recording_labels WHERE recording_id IN ({placeholders}) ORDER BY label ASC",
            [int(rid) for rid in recording_ids],
        ).fetchall()
        grouped: dict[int, list[str]] = {int(rid): [] for rid in recording_ids}
        confidences: dict[int, dict[str, float]] = {int(rid): {} for rid in recording_ids}
        for row in rows:
            rid = int(row['recording_id'])
            grouped[rid].append(str(row['label']))
            if row['confidence'] is not None:
                confidences[rid][str(row['label'])] = float(row['confidence'])
        return grouped, confidences
