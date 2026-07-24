from __future__ import annotations

import json
from typing import Any

from app.utils import _normalize_iso_to_utc


class SettingsRepoMixin:
    """CRUD helpers for the ``app_settings`` key/value table.

    Lives in app.db.settings_repo so the EventDatabase class in app.database.py
    stays small. Public method names + signatures are unchanged.

    Renamed file from ``settings.py`` to ``settings_repo.py`` to avoid
    shadowing the application-level ``app.settings`` configuration module that
    loads YAML/ENV config - both modules are unrelated but Python would
    otherwise resolve ``settings`` ambiguously inside the package.
    """

    def get_setting(self, key: str) -> Any | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            return json.loads(row["value"]) if row else None

    def has_setting(self, key: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM app_settings WHERE key = ?", (key,)).fetchone()
            return row is not None

    def set_setting(self, key: str, value: Any, updated_at: str) -> Any:
        # Defence-in-depth -- cosmetically identical coverage to the
        # recordings / events / camera_diagnostics lifecycle. Every
        # current caller passes ``utc_now()`` from ``app.auth.utc_now``
        # (which is canonical ``+00:00`` by construction), so the
        # helper is a no-op on the present caller base. The wrap
        # ensures a future caller -- a third-party settings sync, a
        # hand-built timestamp from a CSV ingest, a JSON-LD importer
        # -- still lands canonical on disk so an
        # ``ORDER BY updated_at`` list page or any future age-based
        # purge lex-compare behaves correctly.
        updated_at = _normalize_iso_to_utc(updated_at) or updated_at
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), updated_at),
            )
        return value
