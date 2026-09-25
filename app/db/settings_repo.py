from __future__ import annotations

import json
from typing import Any

from app.utils import _normalize_iso_to_utc

_MISS = object()  # sentinel: distinguishes "not cached" from a cached ``None``


class SettingsRepoMixin:
    """CRUD helpers for the ``app_settings`` key/value table.

    Lives in app.db.settings_repo so the EventDatabase class in app.database.py
    stays small. Public method names + signatures are unchanged.

    Renamed file from ``settings.py`` to ``settings_repo.py`` to avoid
    shadowing the application-level ``app.settings`` configuration module that
    loads YAML/ENV config - both modules are unrelated but Python would
    otherwise resolve ``settings`` ambiguously inside the package.

    ``get_setting`` is on the detection hot path -- ``effective_live_config`` /
    ``effective_object_settings`` / ``effective_ai_config`` each read a key
    every camera cycle, and every read otherwise opens a fresh SQLite
    connection. A per-instance cache of the raw JSON string removes that
    connection churn for config that changes only on operator action. The cache
    stores the STRING (not the parsed object) so every read still ``json.loads``
    a fresh, independently-mutable object -- identical semantics to the
    uncached path. ``set_setting`` (the only writer of these keys) invalidates
    on write, and a generation counter closes the read-then-populate race so a
    concurrent write can never leave a stale value cached. The cache lives on
    the database instance, so a DB restore (which builds a fresh instance)
    starts empty; a direct writer that bypasses ``set_setting`` must call
    ``invalidate_setting_cache``.
    """

    def _settings_cache(self) -> dict[str, Any]:
        cache = self.__dict__.get('_settings_json_cache')
        if cache is None:
            cache = {}
            self.__dict__['_settings_json_cache'] = cache
            self.__dict__.setdefault('_settings_cache_gen', 0)
        return cache

    def invalidate_setting_cache(self, key: str | None = None) -> None:
        """Drop cached setting(s) after a write that bypassed ``set_setting``.

        ``key=None`` clears everything (e.g. after a database restore or bulk
        import); a specific key clears just that entry. Always bumps the
        generation so any in-flight ``get_setting`` refuses to repopulate stale.
        """
        self.__dict__['_settings_cache_gen'] = self.__dict__.get('_settings_cache_gen', 0) + 1
        cache = self.__dict__.get('_settings_json_cache')
        if cache is not None:
            if key is None:
                cache.clear()
            else:
                cache.pop(key, None)

    def get_setting(self, key: str) -> Any | None:
        cache = self._settings_cache()
        cached = cache.get(key, _MISS)
        if cached is not _MISS:
            return json.loads(cached) if cached is not None else None
        gen_before = self.__dict__.get('_settings_cache_gen', 0)
        with self.connect() as db:
            row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        raw = row["value"] if row else None
        # Only cache when no write landed while we were reading; otherwise the
        # value we just read may already be stale and must not be memoised.
        if self.__dict__.get('_settings_cache_gen', 0) == gen_before:
            cache[key] = raw
        return json.loads(raw) if raw is not None else None

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
        with self.write_slot(), self.connect() as db:
            db.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), updated_at),
            )
        # Invalidate the read cache so the next get_setting reflects this write.
        self.invalidate_setting_cache(key)
        return value
