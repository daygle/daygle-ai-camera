from __future__ import annotations

import json
from typing import Any

from app.utils import _normalize_iso_to_utc, _search_like


class AiDiagnosticsMixin:
    """CRUD + retention helpers for the ``ai_diagnostics`` table.

    Lives in app.db.ai_diagnostics so the EventDatabase class in
    app.database.py stays small. Mirrors ``CameraDiagnosticsMixin``: this is
    the dedicated AI activity log (verification verdicts, descriptions/tag
    checks, relabels, review captures), kept separate from both the camera
    diagnostics log and the high-volume application logger. ``camera_id`` is
    nullable because some AI jobs (search, catch-up descriptions) run without
    a camera context.
    """

    AI_DIAGNOSTICS_MAX_ROWS = 10000

    def add_ai_diagnostic(
        self,
        *,
        created_at: str,
        event_type: str,
        severity: str = 'info',
        message: str = '',
        details: dict[str, Any] | None = None,
        camera_id: str | None = None,
        camera_name: str | None = None,
    ) -> None:
        # Same lexical-compare concern as the camera diagnostics log: the
        # purge policy lex-compares ``created_at`` against a canonical
        # ``+00:00`` cutoff, so every row is stored in the canonical form of
        # the bound it will be compared against.
        created_at = _normalize_iso_to_utc(created_at) or created_at
        with self.write_slot(), self.connect() as db:
            db.execute(
                """
                INSERT INTO ai_diagnostics (created_at, camera_id, camera_name, event_type, severity, message, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, camera_id, camera_name, event_type, severity, message, json.dumps(details or {})),
            )
            # Same bounded-oldest trim as the camera diagnostics log.
            db.execute(
                """
                DELETE FROM ai_diagnostics
                WHERE id <= (
                    SELECT id FROM ai_diagnostics ORDER BY id DESC LIMIT 1 OFFSET ?
                )
                """,
                (self.AI_DIAGNOSTICS_MAX_ROWS,),
            )

    @staticmethod
    def _ai_diagnostics_filter(
        camera_id: str | None,
        event_type: str | None,
        severity: str | None,
        created_after: str | None = None,
        created_before: str | None = None,
        search: str | None = None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if camera_id:
            conditions.append("camera_id = ?")
            params.append(camera_id)
        if search:
            needle = _search_like(search)
            # ``details`` is stored as a JSON blob, so a LIKE across message
            # and details matches filenames, sha256 prefixes, backend names..
            # as written text — good enough for a diagnostics log.
            conditions.append("(message LIKE ? ESCAPE '\\' OR details LIKE ? ESCAPE '\\')")
            params.extend([needle, needle])
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        if created_after:
            conditions.append("created_at >= ?")
            params.append(_normalize_iso_to_utc(created_after) or created_after)
        if created_before:
            conditions.append("created_at < ?")
            params.append(_normalize_iso_to_utc(created_before) or created_before)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where, params

    def list_ai_diagnostics(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        camera_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._ai_diagnostics_filter(
            camera_id, event_type, severity, created_after, created_before,
            search=search,
        )
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM ai_diagnostics {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry['details'] = json.loads(entry.get('details') or '{}')
            result.append(entry)
        return result

    def count_ai_diagnostics(
        self,
        *,
        camera_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        search: str | None = None,
    ) -> int:
        where, params = self._ai_diagnostics_filter(
            camera_id, event_type, severity, created_after, created_before,
            search=search,
        )
        with self.connect() as db:
            row = db.execute(f"SELECT COUNT(*) AS count FROM ai_diagnostics {where}", params).fetchone()
            return int(row['count'])

    def delete_all_ai_diagnostics(self) -> int:
        with self.write_slot(), self.connect() as db:
            cursor = db.execute("DELETE FROM ai_diagnostics")
            return int(cursor.rowcount or 0)

    def purge_ai_diagnostics_older_than(self, older_than: str) -> int:
        """Delete AI diagnostics created before the given ISO timestamp.

        ``older_than`` is normalised here too, mirroring
        ``purge_camera_diagnostics_older_than``.
        """
        older_than_bound = _normalize_iso_to_utc(older_than) or older_than
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                "DELETE FROM ai_diagnostics WHERE created_at < ?",
                (older_than_bound,),
            )
            return int(cursor.rowcount or 0)
