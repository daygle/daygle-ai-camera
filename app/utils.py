"""Pure stateless utility helpers extracted from ``app/main.py`` (Phase A).

This module contains helpers that have zero state dependencies - they only
use stdlib and their own arguments. Because they are pure, any sibling
module can import them at the top level without any circular-import risk.

Cluster membership:

- ``_non_empty_setting`` -- strip a string setting value from a dict.
- ``build_stream_url`` -- construct an RTSP stream URL from camera settings.
- ``camera_default_name`` -- pick the display name for a camera.
- ``default_camera_detection_settings`` -- return the factory-default
  detection settings dict.
- ``normalize_bool_setting`` -- coerce an arbitrary value to bool.
- ``normalize_email_recipients`` -- deduplicate and validate email addresses.
- ``_parse_iso_datetime`` -- parse an ISO timestamp string to a UTC datetime.

**Pool A rebind (in ``app/main.py``):** every helper is re-bound as
``main.<orig_name>`` so tests and callers that reach them as
``main.normalize_bool_setting`` etc. keep working.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

BASE_DIR: Path = Path(__file__).resolve().parent.parent


_cached_version: str | None = None


def _current_version() -> str:
    """Return the current version from the latest git tag.

    Falls back to the legacy VERSION file if git is unavailable
    (e.g. in a release tarball without a .git directory).
    The result is cached after the first successful lookup.
    """
    global _cached_version
    if _cached_version is not None:
        return _cached_version
    try:
        tag = subprocess.run(
            ['git', 'describe', '--tags', '--abbrev=0'],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if tag:
            _cached_version = tag.lstrip('v')
            return _cached_version
    except Exception:
        pass
    # Fallback: legacy VERSION file (for tarball installs without .git)
    version_file = BASE_DIR / 'VERSION'
    if version_file.exists():
        version = version_file.read_text(encoding='utf-8').strip()
        if version:
            _cached_version = version
            return _cached_version
    # Don't cache 'unknown' so a retry is possible if git becomes available
    return 'unknown'


def _non_empty_setting(settings: dict[str, Any], key: str) -> str:
    return str(settings.get(key) or '').strip()


def build_stream_url(settings: dict[str, Any]) -> str:
    stream_url = _non_empty_setting(settings, 'stream_url')
    if stream_url:
        username = _non_empty_setting(settings, 'username')
        password = _non_empty_setting(settings, 'password')
        parsed = urlsplit(stream_url)
        if username and parsed.scheme in {'rtsp', 'rtsps'} and parsed.netloc and ('@' not in parsed.netloc):
            credentials = quote(username, safe='')
            if password:
                credentials += f":{quote(password, safe='')}"
            return urlunsplit((parsed.scheme, f'{credentials}@{parsed.netloc}', parsed.path, parsed.query, parsed.fragment))
        return stream_url
    host = _non_empty_setting(settings, 'host')
    if not host:
        return ''
    username = _non_empty_setting(settings, 'username')
    password = _non_empty_setting(settings, 'password')
    try:
        port = int(settings.get('port') or 554)
    except (TypeError, ValueError):
        port = 554
    path = _non_empty_setting(settings, 'path') or 'stream1'
    path = path.lstrip('/')
    credentials = ''
    if username:
        credentials = quote(username, safe='')
        if password:
            credentials += f":{quote(password, safe='')}"
        credentials += '@'
    return f'rtsp://{credentials}{host}:{port}/{path}'


def camera_default_name(settings: dict[str, Any], fallback: str = 'Primary Camera') -> str:
    return str(settings.get('name') or settings.get('device') or fallback).strip() or fallback


def default_camera_detection_settings() -> dict[str, Any]:
    return {'object_detection_enabled': True, 'zones': []}


def normalize_bool_setting(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


def normalize_email_recipients(value: Any) -> list[str]:
    raw_recipients = value.split(',') if isinstance(value, str) else value
    if not isinstance(raw_recipients, list):
        return []
    recipients: list[str] = []
    seen: set[str] = set()
    for raw_recipient in raw_recipients:
        recipient = str(raw_recipient).strip()
        if recipient and '@' in recipient and (recipient.lower() not in seen):
            recipients.append(recipient)
            seen.add(recipient.lower())
    return recipients


def _parse_iso_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ''))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_iso_to_utc(value: Any, *, raise_on_invalid: bool = False) -> str | None:
    """Coerce an ISO-8601 timestamp to canonical UTC ISO with ``+00:00`` offset.

    Inverse of :func:`_parse_iso_datetime`: takes a string in (works with
    the Python ``isoformat()`` ``+HH:MM`` form, the JavaScript
    ``Date.toISOString()`` ``Z`` suffix, or a naive timestamp with no
    timezone at all) and returns the canonical ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``
    form that every other datetime column in the recordings table is
    stored in. Used at all SQL datetime-binding sites so SQLite's lexical
    string comparison can never sort a timezone-encoded row before a
    UTC-encoded cutoff / filter bound -- a real failure mode that otherwise
    purges recordings near the retention boundary or excludes them from
    the timeline day-window query.

    Naive timestamps (no tzinfo) are assumed UTC; aware timestamps are
    converted to UTC. Returns ``None`` for falsy input; returns the input
    unchanged on parse failure (best-effort -- lets the DB layer surface
    a useful error rather than silently dropping the row).

    Set ``raise_on_invalid=True`` to make unparseable input raise
    ``ValueError`` instead of falling back. Use this from
    migration / audit contexts where silently storing a malformed
    timestamp is worse than aborting the operation. Default ``False``
    keeps backwards compat for every existing call site
    (add / update / list / purge / timeline endpoints) where raising
    would turn one bad request into a 500.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Python <3.11's ``datetime.fromisoformat`` does not accept the trailing
    # ``Z`` UTC marker that ``Date.prototype.toISOString()`` emits in
    # JavaScript (and that JS code may pass into our API as ``started_after``
    # / ``started_before`` filter arguments). Rewrite it to an explicit
    # ``+00:00`` so the same helper is portable across Python versions.
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError) as exc:
        if raise_on_invalid:
            raise ValueError(f"Invalid ISO timestamp {value!r}: {exc}") from exc
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()
