from __future__ import annotations

import json
from typing import Any

from app.utils import _normalize_iso_to_utc


class CameraDiagnosticsMixin:
    """CRUD + retention helpers for the ``camera_diagnostics`` table.

    Lives in app.db.diagnostics so the EventDatabase class in app.database.py
    stays small. ``CAMERA_DIAGNOSTICS_MAX_ROWS`` is the ring-buffer cap used
    by ``add_camera_diagnostic`` to prevent high-volume camera noise from
    unbounded table growth.
    """

    # ── Camera diagnostics log ────────────────────────────────────────
    # System-generated operational events (capture fallbacks, RTSP reconnects,
    # detection backoff, etc.). Kept separate from audit_log so the security
    # trail stays free of high-volume camera noise.
    CAMERA_DIAGNOSTICS_MAX_ROWS = 10000

    def add_camera_diagnostic(
        self,
        *,
        created_at: str,
        camera_id: str | None,
        camera_name: str | None,
        event_type: str,
        severity: str = 'info',
        message: str = '',
        details: dict[str, Any] | None = None,
    ) -> None:
        # Same lexical-compare concern as the recordings lifecycle: the
        # ``purge_camera_diagnostics_older_than`` retention policy
        # (driven by ``app/backup.py::purge_camera_diagnostics_by_policy``)
        # lex-compares the row's ``created_at`` against a canonical
        # ``+00:00`` cutoff. A source-tz-bearing string would lex-sort
        # before the cutoff and a recording captured at the same wall-clock
        # instant as the cutoff would be wrongly purged inside the
        # retention window. Same fix shape: route through the same
        # canonical UTC normaliser so every row stored here matches the
        # canonical form of the bound it will be lex-compared against.
        created_at = _normalize_iso_to_utc(created_at) or created_at
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO camera_diagnostics (created_at, camera_id, camera_name, event_type, severity, message, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, camera_id, camera_name, event_type, severity, message, json.dumps(details or {})),
            )
            # Trim oldest rows so a flapping camera can't grow the table without
            # bound. Cheap because the table stays small and id is the PK.
            db.execute(
                """
                DELETE FROM camera_diagnostics
                WHERE id NOT IN (
                    SELECT id FROM camera_diagnostics ORDER BY id DESC LIMIT ?
                )
                """,
                (self.CAMERA_DIAGNOSTICS_MAX_ROWS,),
            )

    @staticmethod
    def _camera_diagnostics_filter(
        camera_id: str | None,
        event_type: str | None,
        severity: str | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if camera_id:
            conditions.append("camera_id = ?")
            params.append(camera_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where, params

    def list_camera_diagnostics(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        camera_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._camera_diagnostics_filter(camera_id, event_type, severity)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM camera_diagnostics {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry['details'] = json.loads(entry.get('details') or '{}')
            result.append(entry)
        return result

    def count_camera_diagnostics(
        self,
        *,
        camera_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
    ) -> int:
        where, params = self._camera_diagnostics_filter(camera_id, event_type, severity)
        with self.connect() as db:
            row = db.execute(f"SELECT COUNT(*) AS count FROM camera_diagnostics {where}", params).fetchone()
            return int(row['count'])

    def delete_all_camera_diagnostics(self) -> int:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM camera_diagnostics")
            return int(cursor.rowcount or 0)

    def purge_camera_diagnostics_older_than(self, older_than: str) -> int:
        """Delete diagnostics created before the given ISO timestamp.

        ``older_than`` is already canonical ``+00:00`` from
        ``datetime.now(timezone.utc) - timedelta(...)`` above the
        call stack; we normalise it here too so any future caller
        passing a non-canonical cutoff gets the same lexical-compare
        alignment as the current ``utc_now()`` policy source.
        """
        older_than_bound = _normalize_iso_to_utc(older_than) or older_than
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM camera_diagnostics WHERE created_at < ?",
                (older_than_bound,),
            )
            return int(cursor.rowcount or 0)
