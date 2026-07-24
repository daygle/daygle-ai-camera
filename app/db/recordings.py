from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.utils import _normalize_iso_to_utc


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
        # SQLite compares ISO timestamps as strings. Normalise every
        # datetime column that participates in WHERE-clause comparisons
        # (started_at / ended_at / created_at) to canonical UTC ``+00:00``
        # form so events authored in a different timezone (e.g. EST
        # ``-05:00``) don't lexically sort before the cutoff strings
        # ``purge_recordings`` / ``list_recordings_for_camera_day`` /
        # the size-based grace window bind to -- which would otherwise
        # drop recordings well inside the retention / day boundaries.
        # Best-effort on parse failures: keeps the original string so
        # the DB layer can surface a useful error rather than silently
        # mis-normalising.
        started_at = _normalize_iso_to_utc(started_at) or started_at
        ended_at = _normalize_iso_to_utc(ended_at) or ended_at
        created_at = _normalize_iso_to_utc(created_at) or created_at
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
        # Normalise filter bounds through the same canonical UTC +00:00
        # form rows are stored in. The recordings page constructs these
        # from ``Date.toISOString()`` on the browser side, which yields
        # ``YYYY-MM-DDTHH:MM:SS.sssZ`` (Z suffix); our storage uses
        # ``YYYY-MM-DDTHH:MM:SS+00:00`` (Python ``.isoformat`` against a
        # UTC datetime). Lexically ``+`` (0x2B) sorts BEFORE ``Z`` (0x5A)
        # and ``.`` (0x2E) sorts AFTER ``+`` so a Z-form filter at the
        # exact boundary would otherwise exclude or include rows that
        # represent the same wall-clock instant on the wrong side. The
        # normaliser is idempotent on already-canonical inputs.
        started_after = _normalize_iso_to_utc(started_after) if started_after else None
        started_before = _normalize_iso_to_utc(started_before) if started_before else None
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
        # Normalise the day bounds to canonical UTC +00:00 form so SQLite's
        # lexical comparison against ``started_at`` / ``ended_at`` rows -- which
        # ``add_recording`` already stores in the same form -- doesn't mis-sort
        # a same-instant recording as out-of-window. Defense in depth on top of
        # the timeline endpoint's own ``.astimezone(timezone.utc).isoformat()``
        # so any future caller (script, test, external client) is also covered.
        day_start = _normalize_iso_to_utc(day_start) or day_start
        day_end = _normalize_iso_to_utc(day_end) or day_end
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
                        -- Use ``json_extract`` (JSON1 extension) to read the
                        -- ``camera_id`` field out of ``events.metadata`` by
                        -- key instead of substring-matching a hand-built LIKE
                        -- pattern. The previous LIKE-on-text form was brittle
                        -- to whether ``json.dumps`` separated keys/values with
                        -- ``", "`` (default) vs ``","`` vs ``": "`` vs ``":"``,
                        -- to incidental whitespace inside the JSON for the
                        -- same string id, and to escaping rules for ``%`` /
                        -- ``_`` / ``\``. ``json_extract`` is shape-agnostic and
                        -- returns NULL when the key isn't present so a
                        -- missing camera_id metadata cleanly excludes the row
                        -- (rather than a LIKE wildcard catching one).
                        AND json_extract(e.metadata, '$.camera_id') = ?
                    )
                )
                AND r.started_at < ?
                AND COALESCE(r.ended_at, r.started_at) >= ?
                ORDER BY r.started_at ASC, r.id ASC
                """,
                (camera_id, camera_id, day_end, day_start),
            ).fetchall()
            return self._assemble_recordings(db, rows)

    def get_recording(self, recording_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            return self._recording_with_event(db, row) if row else None

    def update_recording_timing(self, recording_id: int, *, ended_at: str, duration_seconds: float, started_at: str | None = None) -> bool:
        # Normalise timestamps to canonical UTC ``+00:00`` before binding
        # so the size-based purge grace window and timeline day-window
        # queries don't mis-classify an update whose source datetime was
        # in a different timezone.
        ended_at = _normalize_iso_to_utc(ended_at) or ended_at
        started_at_value = _normalize_iso_to_utc(started_at) if started_at is not None else None
        with self.connect() as db:
            if started_at_value is not None:
                cursor = db.execute(
                    "UPDATE recordings SET started_at = ?, ended_at = ?, duration_seconds = ? WHERE id = ?",
                    (started_at_value, ended_at, float(duration_seconds), recording_id),
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
        # Normalise the age cutoff to canonical UTC ``+00:00`` form so the
        # SQLite string comparison against row ``started_at`` values (which
        # are also canonical UTC ``+00:00`` after ``add_recording``'s
        # normalisation) lands on the right side of the boundary. Without
        # this, ``datetime.now(timezone.utc) - timedelta(days=N)`` already
        # yields UTC ``+00:00`` but defensive normalisation also catches
        # any caller passing a non-canonical cutoff (e.g. one carrying a
        # a non-UTC tz suffix from upstream policy code).
        bound_older_than = _normalize_iso_to_utc(older_than) if older_than else None
        bound_grace_cutoff = _normalize_iso_to_utc(
            (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        )
        with self.connect() as db:
            # When only age-based purge is needed, filter in the database.
            # Size-based purge needs all rows to correctly identify oldest recordings.
            if bound_older_than and max_storage_bytes is None:
                candidates = [dict(row) for row in db.execute(
                    "SELECT * FROM recordings WHERE started_at < ? ORDER BY started_at ASC",
                    (bound_older_than,),
                ).fetchall()]
            else:
                candidates = [dict(row) for row in db.execute("SELECT * FROM recordings ORDER BY started_at ASC").fetchall()]
            purge_ids: set[int] = set()
            if bound_older_than:
                purge_ids.update(int(row["id"]) for row in candidates if str(row["started_at"]) < bound_older_than)
            if max_storage_bytes is not None:
                # Grace period: recordings created in the last 10 minutes may still be
                # written by a background capture thread. Don't treat a missing file as
                # an orphan if the record is this new — purging it would leave the file
                # on disk with no database entry once the thread finishes writing.
                # ``bound_grace_cutoff`` is the canonical UTC ``+00:00`` form computed
                # at the top so the ``created_at`` <=> grace_cutoff string compare
                # below lands on the correct side of the boundary too.
                existing_with_sizes: list[tuple[dict[str, Any], int]] = []
                for row in candidates:
                    try:
                        existing_with_sizes.append((row, Path(str(row["file_path"])).stat().st_size))
                    except OSError:
                        if str(row.get("created_at") or "") < bound_grace_cutoff:
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

    def migrate_recording_timestamps_to_utc(self) -> dict[str, int]:
        """One-shot migration: re-encode ``started_at`` / ``ended_at`` /
        ``created_at`` of every recording row to canonical UTC ``+00:00``
        form so SQLite's lexical compares against the retention cutoff
        and timeline day-window land correctly for historical data.

        Idempotent: re-running on an already-canonical database is a
        no-op (every row's normalised value equals its stored value and
        no UPDATE is issued). Malformed timestamps raise ``ValueError``
        inside ``_normalize_iso_to_utc(..., raise_on_invalid=True)``;
        those rows are counted under ``errors`` and skipped so a single
        bad row doesn't abort the whole operation.

        Concurrency: we do NOT hold a single connection through the
        row loop. SQLite grants a write lock to the first UPDATE in a
        transaction; holding it for tens of thousands of rows would
        block every concurrent ``add_recording`` / ``update_recording_timing``
        / ``purge_recordings`` call from live cameras. Instead we
        ``SELECT`` all rows in one short-lived connection so the write
        lock isn't held while we normalise in Python, then commit the
        UPDATEs in chunks of ``commit_chunk_size`` -- each chunk opens
        its own ``self.connect()`` context so SQLite's write lock is
        released between batches and live cameras keep recording
        throughout.
        """
        commit_chunk_size = 500
        counts: dict[str, int] = {
            'rows_scanned': 0,
            'rows_changed': 0,
            'started_at': 0,
            'ended_at': 0,
            'created_at': 0,
            'errors': 0,
        }
        # Phase 1: read all rows in one short-lived connection so the
        # write lock isn't held while we normalise in Python.
        with self.connect() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT id, started_at, ended_at, created_at FROM recordings"
            ).fetchall()]
        counts['rows_scanned'] = len(rows)

        # Phase 2: normalise in memory; collect UPDATEs as a list so we
        # can ``executemany`` them in chunks.
        updates: list[tuple[str, str, str, int]] = []
        for row in rows:
            try:
                new_started = _normalize_iso_to_utc(row['started_at'], raise_on_invalid=True)
                new_ended = _normalize_iso_to_utc(row['ended_at'], raise_on_invalid=True)
                new_created = _normalize_iso_to_utc(row['created_at'], raise_on_invalid=True)
            except ValueError:
                counts['errors'] += 1
                continue
            row_changed = False
            if new_started != row['started_at']:
                counts['started_at'] += 1
                row_changed = True
            if new_ended != row['ended_at']:
                counts['ended_at'] += 1
                row_changed = True
            if new_created != row['created_at']:
                counts['created_at'] += 1
                row_changed = True
            if row_changed:
                counts['rows_changed'] += 1
                updates.append((new_started, new_ended, new_created, int(row['id'])))

        # Phase 3: commit in chunks, with a fresh connection context for
        # each chunk so SQLite's write lock is yielded between batches.
        for chunk_start in range(0, len(updates), commit_chunk_size):
            chunk = updates[chunk_start:chunk_start + commit_chunk_size]
            with self.connect() as db:
                db.executemany(
                    "UPDATE recordings SET started_at = ?, ended_at = ?, created_at = ? WHERE id = ?",
                    chunk,
                )
        return counts

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
