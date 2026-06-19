"""Pure stateless utility helpers extracted from ``app/main.py`` (Phase A).

This module contains helpers that have zero state dependencies — they only
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

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit


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
