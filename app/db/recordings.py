from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.detection_status import GENERIC_TRIGGER_LABELS
from app.media_utils import safe_storage_path
from app.utils import _normalize_iso_to_utc

_SQLITE_BATCH_SIZE = 400


def _batched(values: list[int]) -> list[list[int]]:
    return [values[index:index + _SQLITE_BATCH_SIZE] for index in range(0, len(values), _SQLITE_BATCH_SIZE)]


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
        with self.write_slot(), self.connect() as db:
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
        # Defence-in-depth -- cosmetically identical coverage to the
        # recordings / events / camera_diagnostics lifecycle.
        # ``datetime.now(timezone.utc).isoformat()`` is canonical ``+00:00``
        # by construction so the helper is a no-op on the present source,
        # but routes any FUTURE change (third-party ingest script, naive
        # datetime assembled with ``.replace(tzinfo=...)``, f-string of local
        # time) through the same canonicaliser so the storage form stays
        # uniform. Idempotent on already-canonical input.
        _now_raw = datetime.now(timezone.utc).isoformat()
        now = _normalize_iso_to_utc(_now_raw) or _now_raw
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
        with self.write_slot(), self.connect() as db:
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
            with self.write_slot(), self.connect() as conn:
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
        for row in rows:
            recording_id = int(row['recording_id'])
            labels: list[str] = []
            if row['detection_labels']:
                for label in str(row['detection_labels']).split(','):
                    normalized = label.strip().lower()
                    if normalized and normalized not in GENERIC_TRIGGER_LABELS:
                        labels.append(normalized)
            trigger_label = str(row['trigger_label'] or '').strip().lower()
            if trigger_label and trigger_label not in GENERIC_TRIGGER_LABELS and trigger_label not in labels:
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
        """Return one recording page using the historical list-only contract."""
        items, _next = self.list_recordings_page(
            label=label,
            labels=labels,
            camera_id=camera_id,
            limit=limit,
            alerted_only=alerted_only,
            started_after=started_after,
            started_before=started_before,
            sort=sort,
            source_type=source_type,
        )
        return items

    def list_recordings_page(
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
        *,
        cursor: tuple[str, int] | None = None,
        owner_user_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], tuple[str, int] | None]:
        """Return ``(recordings, next_raw_cursor)`` with stable keyset ordering."""
        started_after = _normalize_iso_to_utc(started_after) if started_after else None
        started_before = _normalize_iso_to_utc(started_before) if started_before else None
        page_size = max(1, int(limit))
        sort_normalized = (sort or 'newest').strip().lower()
        if sort_normalized not in {'newest', 'oldest'}:
            sort_normalized = 'newest'
        descending = sort_normalized == 'newest'
        order_by = (
            'r.started_at DESC, r.id DESC' if descending
            else 'r.started_at ASC, r.id ASC'
        )
        with self.connect() as db:
            conditions: list[str] = []
            params: list[Any] = []
            resolved_labels = [str(label).strip().lower()] if label else [
                str(item).strip().lower() for item in (labels or []) if str(item).strip()
            ]
            join = ''
            if resolved_labels:
                join = 'LEFT JOIN recording_labels rl ON rl.recording_id = r.id'
                placeholders = ','.join('?' * len(resolved_labels))
                conditions.append(f'rl.label IN ({placeholders})')
                params.extend(resolved_labels)
            if camera_id:
                conditions.append('r.camera_id = ?')
                params.append(camera_id)
            if owner_user_id is not None:
                conditions.append('(r.owner_user_id IS NULL OR r.owner_user_id = ?)')
                params.append(int(owner_user_id))
            if alerted_only:
                conditions.append(
                    'EXISTS (SELECT 1 FROM alert_history ah WHERE ah.recording_id = r.id '
                    'OR (r.event_id IS NOT NULL AND ah.event_id = r.event_id))'
                )
            if started_after:
                conditions.append('r.started_at >= ?')
                params.append(started_after)
            if started_before:
                conditions.append('r.started_at <= ?')
                params.append(started_before)
            if source_type == 'sound':
                conditions.append("EXISTS (SELECT 1 FROM events e WHERE e.id = r.event_id AND e.source = 'sound')")
            elif source_type == 'object':
                conditions.append("(r.event_id IS NULL OR NOT EXISTS (SELECT 1 FROM events e WHERE e.id = r.event_id AND e.source = 'sound'))")
            if cursor is not None:
                comparison = '<' if descending else '>'
                conditions.append(f'(r.started_at, r.id) {comparison} (?, ?)')
                params.extend((cursor[0], int(cursor[1])))

            distinct = 'DISTINCT ' if resolved_labels else ''
            rows = db.execute(
                f'''SELECT {distinct}r.* FROM recordings r {join}
                    {'WHERE ' + ' AND '.join(conditions) if conditions else ''}
                    ORDER BY {order_by} LIMIT ?''',
                [*params, page_size + 1],
            ).fetchall()
            has_more = len(rows) > page_size
            visible_rows = rows[:page_size]
            next_cursor = (
                (str(visible_rows[-1]['started_at']), int(visible_rows[-1]['id']))
                if has_more and visible_rows
                else None
            )
            return self._assemble_recordings(db, visible_rows), next_cursor

    def list_recordings_for_camera_day(self, camera_id: str, day_start: str, day_end: str) -> list[dict[str, Any]]:
        # Normalise the day bounds to canonical UTC +00:00 form so SQLite's
        # lexical comparison against ``started_at`` / ``ended_at`` rows -- which
        # ``add_recording`` already stores in the same form -- doesn't mis-sort
        # a same-instant recording as out-of-window. Defense in depth on top of
        # the timeline endpoint's own ``.astimezone(timezone.utc).isoformat()``
        # so any future caller (script, test, external client) is also covered.
        day_start = _normalize_iso_to_utc(day_start) or day_start
        day_end = _normalize_iso_to_utc(day_end) or day_end
        # Use json_extract over LIKE for metadata camera_id lookup.
        # The previous LIKE-on-text form was brittle to JSON serialisation
        # whitespace, escape characters (``%`` / ``_`` / ``\``), and
        # incidental format changes. json_extract is shape-agnostic and
        # returns NULL when the key is absent, cleanly excluding the row.
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
                        -- prefer json_extract for metadata camera_id
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
        with self.write_slot(), self.connect() as db:
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
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                "UPDATE recordings SET trigger_type = ?, trigger_label = ? WHERE id = ?",
                (str(trigger_type or 'motion'), str(trigger_label).strip().lower() if trigger_label else None, recording_id),
            )
            return cursor.rowcount > 0

    @staticmethod
    def _purge_recording_children(db: sqlite3.Connection, recording_ids: list[int]) -> None:
        """Remove rows that reference the given recordings, mirroring the schema's
        declared ``ON DELETE CASCADE`` (recording_labels) and ``ON DELETE SET
        NULL`` (alert_history.recording_id). SQLite does not enforce those
        referential actions because ``PRAGMA foreign_keys`` is off per
        connection, so without this the labels would orphan and alert rows would
        keep a dangling recording_id. Call inside the same transaction, BEFORE
        deleting the ``recordings`` rows themselves."""
        for batch in _batched([int(recording_id) for recording_id in recording_ids]):
            placeholders = ','.join('?' * len(batch))
            db.execute(f"DELETE FROM recording_labels WHERE recording_id IN ({placeholders})", batch)
            db.execute(f"UPDATE alert_history SET recording_id = NULL WHERE recording_id IN ({placeholders})", batch)
            db.execute(f"UPDATE events SET recording_id = NULL WHERE recording_id IN ({placeholders})", batch)

    def cleanup_incomplete_recordings(self) -> list[dict[str, Any]]:
        """Delete recordings whose files were never written (e.g. service restarted mid-capture)."""
        with self.write_slot(), self.connect() as db:
            rows = db.execute("SELECT * FROM recordings").fetchall()
            incomplete = []
            for row in rows:
                file_path = row["file_path"]
                if not file_path:
                    incomplete.append(dict(row))
                    continue
                path = safe_storage_path(file_path, roots=('recordings_dir',))
                if path is None or not (path.exists() and path.is_file() and path.stat().st_size > 0):
                    incomplete.append(dict(row))
            if incomplete:
                ids = [int(r["id"]) for r in incomplete]
                self._purge_recording_children(db, ids)
                for batch in _batched(ids):
                    placeholders = ','.join('?' * len(batch))
                    db.execute(f"DELETE FROM recordings WHERE id IN ({placeholders})", batch)
            return incomplete

    def delete_all_recordings(self) -> list[dict[str, Any]]:
        with self.write_slot(), self.connect() as db:
            rows = db.execute("SELECT * FROM recordings").fetchall()
            # Mirror the declared CASCADE / SET NULL (foreign_keys is off): clear
            # the label join table and detach any alert_history rows first.
            db.execute("DELETE FROM recording_labels")
            db.execute("UPDATE alert_history SET recording_id = NULL")
            db.execute("UPDATE events SET recording_id = NULL")
            db.execute("DELETE FROM recordings")
            return [dict(row) for row in rows]

    def delete_recording(self, recording_id: int) -> dict[str, Any] | None:
        with self.write_slot(), self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            if row is None:
                return None
            self._purge_recording_children(db, [int(recording_id)])
            db.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
            return dict(row)

    def purge_recordings(
        self,
        *,
        older_than: str | None = None,
        max_storage_bytes: int | None = None,
        _linked_event_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
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
        with self.write_slot(), self.connect() as db:
            purge_ids: set[int] = set()
            if bound_older_than and max_storage_bytes is None:
                # Age-only retention is fully index-filtered and needs no media stats.
                purge_ids.update(
                    int(row["id"])
                    for row in db.execute(
                        "SELECT id FROM recordings WHERE started_at < ? ORDER BY started_at ASC",
                        (bound_older_than,),
                    )
                )
            elif max_storage_bytes is not None:
                # Stream compact rows newest-first. Once retained media exceeds
                # the cap, every older recording must be evicted; no more sizes
                # are needed for those rows. This preserves the same newest-first
                # survivor set as an oldest-first total-and-subtract pass, while
                # avoiding a stat sweep of the cold tail when the cap is reached.
                candidates = db.execute(
                    "SELECT id, started_at, created_at, file_path "
                    "FROM recordings ORDER BY started_at DESC, id DESC"
                )
                retained_bytes = 0
                storage_limit_crossed = False
                for row in candidates:
                    recording_id = int(row["id"])
                    if bound_older_than and str(row["started_at"]) < bound_older_than:
                        purge_ids.add(recording_id)
                        continue
                    created_at = str(row["created_at"] or "")
                    is_within_grace = created_at >= bound_grace_cutoff
                    if storage_limit_crossed:
                        # Older rows are over-cap and should be removed without
                        # stats. Only check recent rows to preserve the existing
                        # protection for a capture whose file is not finalized yet.
                        if is_within_grace:
                            try:
                                path = safe_storage_path(row["file_path"], roots=('recordings_dir',))
                                if path is None:
                                    raise OSError('recording path is outside configured storage')
                                path.stat()
                            except OSError:
                                continue
                        purge_ids.add(recording_id)
                        continue
                    # Grace period: don't treat a recent missing file as an orphan.
                    try:
                        path = safe_storage_path(row["file_path"], roots=('recordings_dir',))
                        if path is None:
                            raise OSError('recording path is outside configured storage')
                        size = path.stat().st_size
                    except OSError:
                        if not is_within_grace:
                            purge_ids.add(recording_id)
                        continue
                    retained_bytes += size
                    if retained_bytes > max_storage_bytes:
                        purge_ids.add(recording_id)
                        storage_limit_crossed = True
            elif bound_older_than:
                # Defensive fallback if the policy caller explicitly disables the
                # size cap while still requesting age-based retention.
                purge_ids.update(
                    int(row["id"])
                    for row in db.execute(
                        "SELECT id FROM recordings WHERE started_at < ? ORDER BY started_at ASC",
                        (bound_older_than,),
                    )
                )
            else:
                # No active retention limits; preserve the previous no-op behavior.
                return []
            if not purge_ids:
                return []
            rows: list[dict[str, Any]] = []
            for batch in _batched(sorted(purge_ids)):
                placeholders = ','.join('?' * len(batch))
                rows.extend(dict(row) for row in db.execute(
                    f"SELECT * FROM recordings WHERE id IN ({placeholders})",
                    batch,
                ))
            rows.sort(key=lambda row: (str(row.get("started_at") or ""), int(row["id"])))
            ordered_purge_ids = sorted(purge_ids)
            if _linked_event_ids is not None:
                for batch in _batched(ordered_purge_ids):
                    placeholders = ','.join('?' * len(batch))
                    linked_rows = db.execute(
                        f"""
                        SELECT DISTINCT id FROM events
                        WHERE recording_id IN ({placeholders})
                           OR id IN (SELECT event_id FROM recordings WHERE id IN ({placeholders}))
                           OR id IN (
                               SELECT event_id FROM alert_history WHERE recording_id IN ({placeholders})
                           )
                        """,
                        batch * 3,
                    ).fetchall()
                    _linked_event_ids.extend(int(row['id']) for row in linked_rows)
            self._purge_recording_children(db, ordered_purge_ids)
            db.executemany("DELETE FROM recordings WHERE id = ?", [(recording_id,) for recording_id in ordered_purge_ids])
            return rows

    def _recording_row(self, row: sqlite3.Row) -> dict[str, Any]:
        recording = dict(row)
        file_path = safe_storage_path(recording.get("file_path"), roots=('recordings_dir',))
        recording["media_ready"] = (
            file_path is not None
            and file_path.exists()
            and file_path.is_file()
            and file_path.stat().st_size > 0
        )
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
        # A recording spans many events: attach every event linked to this clip.
        recording["events"] = self._events_for_recordings(db, [int(recording["id"])]).get(int(recording["id"]), [])
        return recording

    def _assemble_recordings(self, db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Assemble recordings with labels, events, and detections using batch IN-clause queries."""
        if not rows:
            return []
        recordings = [self._recording_row(row) for row in rows]
        recording_ids = [int(r['id']) for r in recordings]

        labels_map, confidences_map = self._fetch_labels_for_recordings(db, recording_ids)
        events_by_recording = self._events_for_recordings(db, recording_ids)

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
            recording['events'] = events_by_recording.get(int(recording['id']), [])
            if recording.get('event_id') is not None:
                eid = int(recording['event_id'])
                recording['event'] = events_map.get(eid)
                recording['detections'] = detections_map.get(eid, [])
        return recordings

    @staticmethod
    def _events_for_recordings(
        db: sqlite3.Connection, recording_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        """Return the events linked to each recording (recording -> many events).

        Events are ordered oldest-first (the order they occurred within the clip)
        and each carries its own detections. Used to expose ``recording['events']``
        so the recordings list can show every object/sound the clip contains.
        """
        if not recording_ids:
            return {}
        placeholders = ','.join('?' * len(recording_ids))
        event_rows = db.execute(
            f"SELECT * FROM events WHERE recording_id IN ({placeholders}) ORDER BY created_at ASC, id ASC",
            [int(rid) for rid in recording_ids],
        ).fetchall()
        if not event_rows:
            return {}
        event_ids = [int(row['id']) for row in event_rows]
        det_placeholders = ','.join('?' * len(event_ids))
        detections_by_event: dict[int, list[dict[str, Any]]] = {}
        for det_row in db.execute(
            f"SELECT * FROM detections WHERE event_id IN ({det_placeholders}) ORDER BY confidence DESC",
            event_ids,
        ).fetchall():
            detections_by_event.setdefault(int(det_row['event_id']), []).append(dict(det_row))
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in event_rows:
            event = dict(row)
            event['metadata'] = json.loads(event.get('metadata') or '{}')
            event['detections'] = detections_by_event.get(int(event['id']), [])
            grouped.setdefault(int(row['recording_id']), []).append(event)
        return grouped

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
