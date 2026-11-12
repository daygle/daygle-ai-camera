"""Small stateless helpers shared across the application.

Exported helpers:

- ``_current_version`` -- version from the latest git tag, with a VERSION-file
  fallback for tarball installs.
- ``utc_now`` / ``_parse_iso_datetime`` / ``_normalize_iso_to_utc`` --
  canonical UTC timestamp formatting, parsing, and normalisation.
- ``local_day_bounds_to_utc`` -- browser-local calendar-day filter window
  converted to half-open UTC bounds.
- ``build_stream_url`` / ``_inject_credentials`` -- RTSP URL construction,
  including IPv6 bracketing and credential escaping.
- ``camera_default_name`` / ``default_camera_detection_settings`` --
  camera record defaults.
- ``normalize_bool_setting`` / ``normalize_ptz_motion_detection`` /
  ``normalize_hhmm`` / ``normalize_email_recipients`` -- setting coercion.
- ``_search_like`` -- escaped ``LIKE`` needle for the diagnostics searches.
- ``parse_json_object`` -- tolerant parser for JSON-object columns (event
  metadata), so one malformed row cannot fail a whole list response.

Everything here depends only on the standard library and its own arguments:
no ``app`` imports and no application singletons, so the module is safe to
import from any layer, including during bootstrap.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import subprocess
from datetime import date, datetime, timedelta, timezone, UTC
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

logger = logging.getLogger('daygle.ai')

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
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug('git describe unavailable for the version lookup: %s', exc)
    # Fallback: legacy VERSION file (for tarball installs without .git)
    version_file = BASE_DIR / 'VERSION'
    if version_file.exists():
        version = version_file.read_text(encoding='utf-8').strip()
        if version:
            _cached_version = version
            return _cached_version
    # Don't cache 'unknown' so a retry is possible if git becomes available
    return 'unknown'


def utc_now() -> str:
    """Return the current UTC time in the application's canonical ISO form."""
    return datetime.now(UTC).isoformat()


def local_day_bounds_to_utc(
    day_from: str | None,
    day_to: str | None,
    tz_offset_minutes: int | None,
) -> tuple[datetime | None, datetime | None]:
    """Convert an inclusive ``[day_from, day_to]`` range of *calendar days* -- as
    the user sees them in their browser timezone -- into a half-open
    ``[start, end)`` pair of timezone-aware UTC datetimes.

    ``day_from`` / ``day_to`` are ``YYYY-MM-DD`` strings (already validated by the
    caller); either may be ``None``. ``tz_offset_minutes`` is the browser's
    ``Date.getTimezoneOffset()`` -- minutes that local time is BEHIND UTC, so
    UTC+10 is ``-600`` and UTC-5 is ``+300``. ``None`` keeps the legacy UTC-day
    boundary. This mirrors the ``/api/recordings/timeline`` endpoint so every
    day-window filter in the app agrees on where a day starts and ends.
    """
    tz = UTC if tz_offset_minutes is None else timezone(timedelta(minutes=-tz_offset_minutes))
    start: datetime | None = None
    end: datetime | None = None
    if day_from:
        d = date.fromisoformat(day_from)
        start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)
    if day_to:
        d = date.fromisoformat(day_to) + timedelta(days=1)
        end = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)
    return start, end


def _non_empty_setting(settings: dict[str, Any], key: str) -> str:
    return str(settings.get(key) or '').strip()


# Restrict camera demuxers and protocols to exclude playlists and local files.
# HTTP redirect control is supported by newer FFmpeg builds; older command-line
# tools reject the unknown option rather than opening the HTTP stream.
# ``-max_redirects`` is an HTTP-protocol option: it has no effect on the RTSP
# demuxer's internal redirect handling, so RTSP inputs get no such option
# here. RTSP redirects are instead constrained by app.rtsp_broker, which
# proxies every live RTSP connection and drops cross-authority redirects.
CAMERA_STREAM_PROTOCOLS = 'rtsp,rtsps,rtmp,rtmps,http,https,tls,tcp,udp,rtp,srt,srtp,crypto,httpproxy'
# An RTSP input must never be redirectable to an HTTP/RTMP service: keep its
# whitelist to RTSP plus the transports RTSP itself uses.
RTSP_STREAM_PROTOCOLS = 'rtsp,rtsps,tcp,udp,rtp,tls,crypto'
CAMERA_STREAM_FORMATS = 'rtsp,rtp,mjpeg,mjpeg_2000,mpjpeg,mpegts,mov,matroska,flv,h264,hevc'


def camera_stream_input_options(url: str) -> tuple[str, ...]:
    scheme = urlsplit(url).scheme.lower()
    if scheme in {'rtsp', 'rtsps'}:
        options = ('-protocol_whitelist', RTSP_STREAM_PROTOCOLS, '-format_whitelist', CAMERA_STREAM_FORMATS)
        return (*options, '-rtsp_transport', 'tcp')
    options = ('-protocol_whitelist', CAMERA_STREAM_PROTOCOLS, '-format_whitelist', CAMERA_STREAM_FORMATS)
    if scheme in {'http', 'https'}:
        return (*options, '-max_redirects', '0')
    return options


def _inject_credentials(url: str, settings: dict[str, Any]) -> str:
    """Inject username/password credentials into an RTSP URL if present."""
    username = _non_empty_setting(settings, 'username')
    password = _non_empty_setting(settings, 'password')
    parsed = urlsplit(url)
    if username and parsed.scheme in {'rtsp', 'rtsps'} and parsed.netloc and ('@' not in parsed.netloc):
        credentials = quote(username, safe='')
        if password:
            credentials += f":{quote(password, safe='')}"
        return urlunsplit((parsed.scheme, f'{credentials}@{parsed.netloc}', parsed.path, parsed.query, parsed.fragment))
    return url


def build_stream_url(settings: dict[str, Any]) -> str:
    stream_url = _non_empty_setting(settings, 'stream_url')
    if stream_url:
        return _inject_credentials(stream_url, settings)
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
    if ':' in host and not host.startswith('['):
        # A bare IPv6 literal must be bracketed in a URL authority, or its
        # colons are read as the port separator.
        try:
            if ipaddress.ip_address(host.split('%', 1)[0]).version == 6:
                host = f'[{host}]'
        except ValueError:
            pass

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
    return {'object_detection_enabled': True, 'ptz_motion_detection': 'auto', 'zones': []}


# Per-camera control for the automatic PTZ / camera-motion (ego-motion)
# suppressor. ``auto`` follows the camera's PTZ-enabled flag (the historical
# behaviour); ``on`` always runs it (a fixed camera on a movable/shared mount);
# ``off`` never runs it (a PTZ camera driven by presets that must not have its
# object alerts suppressed). App-issued PTZ commands are honoured regardless.
_PTZ_MOTION_DETECTION_MODES: frozenset[str] = frozenset({'auto', 'on', 'off'})


def normalize_ptz_motion_detection(value: Any) -> str:
    """Coerce the ``ptz_motion_detection`` setting to ``auto`` / ``on`` / ``off``."""
    mode = str(value if value is not None else 'auto').strip().lower()
    return mode if mode in _PTZ_MOTION_DETECTION_MODES else 'auto'


def normalize_bool_setting(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


# An alert recipient is either a literal email address or a reference to a
# user account, stored as ``user:<id>``. User references are resolved to the
# user's current address when the alert is sent (see
# ``app.alert_dispatch.resolve_email_recipients``), so a user changing their
# email, or being deactivated, takes effect without editing every rule.
_USER_RECIPIENT_RE = re.compile(r'^user:(\d+)$', re.IGNORECASE)


def user_recipient_id(value: Any) -> int | None:
    """Return the user id of a ``user:<id>`` recipient token, else ``None``."""
    match = _USER_RECIPIENT_RE.match(str(value or '').strip())
    return int(match.group(1)) if match else None


def normalize_recipient_entry(value: Any) -> str:
    """Canonical form of one recipient entry, or ``''`` if it is not valid.

    Keeps email addresses (anything with an ``@``) as typed and lower-cases
    ``user:<id>`` tokens.
    """
    recipient = str(value or '').strip()
    user_id = user_recipient_id(recipient)
    if user_id is not None:
        return f'user:{user_id}'
    return recipient if '@' in recipient else ''


def normalize_push_recipients(value: Any) -> list[str]:
    """Push recipients: ``user:<id>`` references only (a list or a
    comma-separated string), canonical and de-duplicated. Each resolves to
    that user's own ntfy topic when the push is sent."""
    return [entry for entry in normalize_email_recipients(value) if user_recipient_id(entry) is not None]


def normalize_email_recipients(value: Any) -> list[str]:
    raw_recipients = value.split(',') if isinstance(value, str) else value
    if not isinstance(raw_recipients, list):
        return []
    recipients: list[str] = []
    seen: set[str] = set()
    for raw_recipient in raw_recipients:
        recipient = normalize_recipient_entry(raw_recipient)
        if recipient and (recipient.lower() not in seen):
            recipients.append(recipient)
            seen.add(recipient.lower())
    return recipients


def normalize_hhmm(value: Any) -> str | None:
    """Return a zero-padded ``HH:MM`` string, or ``None`` for empty/unset values.

    Alert active/notify windows are compared LEXICALLY against the current
    ``HH:MM`` (see ``AlertEngine._is_active_now``, ``_rule_notify_active_now``
    and ``SoundDetector._rule_active_now``). A non-padded ``"9:00"`` sorts after
    ``"10:00"`` and after a padded ``"08:30"`` (``'9' > '1' > '0'``), so a rule
    whose bounds are not zero-padded evaluates its window wrong. Padding here
    keeps the comparison correct regardless of how the value reached storage
    (settings form, direct API call, or a restored/legacy config). Values that
    are already zero-padded or that can't be parsed pass through unchanged so
    existing well-formed data is unaffected.
    """
    s = str(value or '').strip()
    if not s:
        return None
    parts = s.split(':')
    if len(parts) == 2:
        try:
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f'{h:02d}:{m:02d}'
        except ValueError:
            pass
    return s


def parse_json_object(raw: Any) -> dict[str, Any]:
    """Parse a JSON-object column, tolerating corrupt or legacy values.

    Event ``metadata`` blobs are written by this application, but they also
    arrive with a database restored from an uploaded backup (validated for
    schema, not for value contents) or written by older revisions. The search
    layer already guards this with ``json_valid``; the list hydration used to
    call ``json.loads`` directly, so a single unreadable row raised
    ``JSONDecodeError`` and failed the whole ``/api/events`` or
    ``/api/recordings`` request with an HTTP 500. Anything that is not a JSON
    object reads as ``{}`` -- no metadata -- instead.
    """
    try:
        parsed = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _search_like(user_search: str) -> str:
    r"""Turn an operator's free-text search into an escaped SQL LIKE needle.

    Shared by the diagnostics log layers (camera + AI): ``%``/``_`` in the
    operator's text must match literally and ``\`` must escape itself
    (SQLite ``LIKE ... ESCAPE``), never act as wildcards. SQLite ``LIKE``
    is case-insensitive for ASCII, which is what operators expect when
    searching model file names or sha256 prefixes.
    """
    escaped = (
        user_search.replace('\\', '\\\\')
        .replace('%', '\\%')
        .replace('_', '\\_')
    )
    return f"%{escaped}%"


def _parse_iso_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ''))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()
