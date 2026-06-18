from __future__ import annotations

import json
from typing import Any


class AuditLogMixin:
    """CRUD + pagination helpers for the ``audit_log`` table.

    Lives in app.db.audit so the EventDatabase class in app.database.py stays
    small. Public method names + signatures are unchanged.
    """

    def add_audit_log(
        self,
        *,
        created_at: str,
        user_id: int | None,
        username: str,
        action: str,
        resource: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
        status: str = 'success',
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO audit_log (created_at, user_id, username, action, resource, resource_id, details, ip_address, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, user_id, username, action, resource, resource_id, json.dumps(details or {}), ip_address, status),
            )

    @staticmethod
    def _audit_log_filter(
        action: str | None,
        username: str | None,
        resource: str | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if action:
            conditions.append("action = ?")
            params.append(action)
        if username:
            conditions.append("username = ?")
            params.append(username)
        if resource:
            conditions.append("resource LIKE ?")
            params.append(f"{resource}%")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where, params

    def list_audit_logs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        username: str | None = None,
        resource: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._audit_log_filter(action, username, resource)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM audit_log {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry['details'] = json.loads(entry.get('details') or '{}')
            result.append(entry)
        return result

    def count_audit_logs(
        self,
        *,
        action: str | None = None,
        username: str | None = None,
        resource: str | None = None,
    ) -> int:
        where, params = self._audit_log_filter(action, username, resource)
        with self.connect() as db:
            row = db.execute(f"SELECT COUNT(*) AS count FROM audit_log {where}", params).fetchone()
            return int(row['count'])
