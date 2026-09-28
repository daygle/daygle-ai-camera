from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.utils import _normalize_iso_to_utc

# Full-text index over the AI event descriptions (app.ai_descriptions). The
# FTS5 rowid IS the event id, so no side table is needed; a trigger removes the
# row when its event is deleted (foreign keys are off on these connections, so
# a declared cascade would never fire). ``porter`` stemming lets "carrying"
# match "carry" and "cars" match "car".
_FTS_TABLE = 'event_description_fts'
_DESCRIPTION_JSON = "json_extract(e.metadata, '$.ai_description.text')"

# Exact DDL, also allowlisted by app.backup: a restored backup may carry this
# virtual table and trigger, but no other virtual table or trigger.
EVENT_DESCRIPTION_FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} "
    "USING fts5(description, tokenize='porter unicode61')"
)
EVENT_DESCRIPTION_TRIGGERS: dict[str, str] = {
    'trg_events_delete_description': (
        "CREATE TRIGGER IF NOT EXISTS trg_events_delete_description AFTER DELETE ON events "
        f"BEGIN DELETE FROM {_FTS_TABLE} WHERE rowid = old.id; END;"
    ),
}


class EventDescriptionsMixin:
    """Store and search the AI-written event descriptions."""

    _description_fts: bool = False

    def ensure_event_description_index(self, db: sqlite3.Connection) -> None:
        try:
            db.execute(EVENT_DESCRIPTION_FTS_DDL)
            for ddl in EVENT_DESCRIPTION_TRIGGERS.values():
                db.execute(ddl)
            self._description_fts = True
        except sqlite3.OperationalError:
            # SQLite built without FTS5: search falls back to LIKE over the
            # description stored in the event metadata.
            self._description_fts = False

    def set_event_description(self, event_id: int, record: dict[str, Any]) -> bool:
        """Store the description record in the event metadata and index its text."""
        text = str(record.get('text') or '').strip()
        # Tags are indexed with the sentence so "ladder" finds an event whose
        # caption only said "tools" but whose tags named the ladder.
        tags = [str(tag) for tag in record.get('tags') or [] if str(tag).strip()]
        indexed = ' '.join([text, *tags]).strip()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE events SET metadata = json_patch(COALESCE(NULLIF(metadata, ''), '{}'), ?) WHERE id = ?",
                (json.dumps({'ai_description': record}), int(event_id)),
            )
            if cursor.rowcount == 0:
                return False
            if self._description_fts:
                db.execute(f"DELETE FROM {_FTS_TABLE} WHERE rowid = ?", (int(event_id),))
                if indexed:
                    db.execute(f"INSERT INTO {_FTS_TABLE}(rowid, description) VALUES (?, ?)", (int(event_id), indexed))
            return True

    def add_ai_recording_tags(self, event_id: int, tags: list[str]) -> int:
        """Attach AI tags to the event's recordings as ``source='ai'`` labels.

        They show in the recordings list (marked as AI) and match the label
        filter. A real detection of the same label always wins: see
        RecordingsMixin._insert_recording_labels. Returns recordings touched.
        """
        tags = [str(tag).strip().lower() for tag in tags if str(tag).strip()]
        if not tags:
            return 0
        with self.write_slot(), self.connect() as db:
            rows = db.execute(
                """
                SELECT r.id FROM recordings r
                WHERE r.event_id = ?
                   OR r.id = (SELECT recording_id FROM events WHERE id = ?)
                   OR EXISTS (SELECT 1 FROM alert_history ah WHERE ah.event_id = ? AND ah.recording_id = r.id)
                """,
                (int(event_id), int(event_id), int(event_id)),
            ).fetchall()
            for row in rows:
                self._insert_recording_labels(db, int(row['id']), tags, source='ai')
            return len(rows)

    def events_without_description(self, *, since: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Newest object events with a snapshot and no description yet (for backfill)."""
        clauses = ["e.snapshot_path IS NOT NULL", "e.snapshot_path != ''",
                   f"{_DESCRIPTION_JSON} IS NULL",
                   "COALESCE(json_extract(e.metadata, '$.source'), '') != 'sound-detection'"]
        params: list[Any] = []
        if since:
            clauses.append("e.created_at >= ?")
            params.append(_normalize_iso_to_utc(since))
        with self.connect() as db:
            rows = db.execute(
                f"SELECT e.* FROM events e WHERE {' AND '.join(clauses)} ORDER BY e.id DESC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
            return self._events_with_detections(db, rows) if rows else []

    def search_event_descriptions(
        self,
        *,
        groups: list[list[str]] | None,
        camera_ids: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        owner_user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Events whose description matches every group (any term within one).

        ``groups`` is a list of synonym lists, e.g. ``[["red"], ["car", "ute"]]``
        means red AND (car OR ute). Results are ranked by relevance, newest
        first among equals. ``owner_user_id`` hides events linked to another
        user's private recording, as the events list does.
        """
        clauses: list[str] = []
        params: list[Any] = []
        order = 'e.created_at DESC, e.id DESC'
        join = ''
        if groups is None:
            # Any described event (a camera- or time-only search).
            if self._description_fts:
                join = f'JOIN {_FTS_TABLE} f ON f.rowid = e.id'
            else:
                clauses.append(f'{_DESCRIPTION_JSON} IS NOT NULL')
            groups = []
        else:
            groups = [[term for term in group if term] for group in groups]
            groups = [group for group in groups if group]
            if not groups:
                return []
        expression = fts_match_expression(groups) if groups and self._description_fts else ''
        if groups and self._description_fts:
            if not expression:
                return []
            join = f'JOIN {_FTS_TABLE} f ON f.rowid = e.id'
            clauses.append(f'{_FTS_TABLE} MATCH ?')
            params.append(expression)
            order = f'bm25({_FTS_TABLE}) ASC, e.created_at DESC, e.id DESC'
        elif groups:
            for group in groups:
                clauses.append('(' + ' OR '.join(f"LOWER({_DESCRIPTION_JSON}) LIKE ? ESCAPE '\\'" for _ in group) + ')')
                params.extend(f'%{_like_escape(term.lower())}%' for term in group)
        if camera_ids:
            clauses.append(
                "json_extract(e.metadata, '$.camera_id') IN (" + ','.join('?' * len(camera_ids)) + ')'
            )
            params.extend(camera_ids)
        if since:
            clauses.append('e.created_at >= ?')
            params.append(_normalize_iso_to_utc(since))
        if until:
            clauses.append('e.created_at <= ?')
            params.append(_normalize_iso_to_utc(until))
        if owner_user_id is not None:
            # Same scoping as EventsMixin.search_events_page.
            clauses.append(
                'NOT EXISTS (SELECT 1 FROM recordings r WHERE (r.id = e.recording_id OR r.event_id = e.id '
                'OR EXISTS (SELECT 1 FROM alert_history ah WHERE ah.event_id = e.id AND ah.recording_id = r.id)) '
                'AND r.owner_user_id IS NOT NULL AND r.owner_user_id != ?)'
            )
            params.append(int(owner_user_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        sql = f"SELECT e.* FROM events e {join} {where} ORDER BY {order} LIMIT ?"
        with self.connect() as db:
            try:
                rows = db.execute(sql, (*params, int(limit))).fetchall()
            except sqlite3.OperationalError:
                return []  # malformed FTS expression: treat as no match
            return self._events_with_detections(db, rows) if rows else []


def _like_escape(value: str) -> str:
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def fts_match_expression(groups: list[list[str]]) -> str:
    """Build an FTS5 query: groups ANDed, terms within a group ORed.

    Each word is double-quoted, so user text can never inject FTS operators,
    and multi-word terms become phrases. No prefix wildcard: the porter
    tokenizer already matches plurals and verb forms, while ``car*`` would
    also match "carrying" and "careful".
    """
    parts = []
    for group in groups:
        alternatives = []
        for term in group:
            words = [word for word in ''.join(ch if ch.isalnum() else ' ' for ch in term.lower()).split() if word]
            if not words:
                continue
            if len(words) == 1:
                alternatives.append(f'"{words[0]}"')
            else:
                alternatives.append('"' + ' '.join(words) + '"')
        if alternatives:
            parts.append('(' + ' OR '.join(alternatives) + ')')
    return ' AND '.join(parts)
