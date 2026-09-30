"""Plain-English search over AI event descriptions.

A question such as "red car in the driveway yesterday afternoon" becomes a
structured query - concept groups ("red" AND "car"/"vehicle"/"ute"), a camera
and a time window - which runs against the full-text index of event
descriptions (app.db.descriptions).

The local model (the one configured for AI verification/descriptions) does the
interpretation because it understands synonyms, camera names and relative
times. Without it, or when it fails, a keyword fallback handles the common
cases: content words, camera names mentioned in the question, and "today",
"yesterday", "this morning", "last night", "this afternoon"/"tonight".
Searches never fail because the model is down.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import app.state as _state
from app.ai_verification import (
    VerificationError,
    VisionVerifier,
    describe_mode,
    effective_ai_verification_settings,
)

logger = logging.getLogger('daygle.ai')

MAX_QUERY_CHARS = 300
MAX_GROUPS = 6
MAX_TERMS_PER_GROUP = 8
_TERM_RE = re.compile(r"^[a-z0-9][a-z0-9 '\-]{0,39}$")
_WORD_RE = re.compile(r"[a-z0-9']+")

# Words that never help match a caption. Time and camera words are handled
# separately; these are grammar and search-request filler.
STOPWORDS = frozenset("""
a an the and or of in on at to for from by with without into onto near by over under
is are was were be been being has have had do does did any anyone anybody anything someone
somebody something some all show find me my our us i we you it its this that these those there
here who what when where which while please events event footage video videos clip clips
camera cameras recording recordings search look looking see seen saw last past ago around
between during about just only ever every with them they their he she his her him
""".split())
_TIME_WORDS = frozenset("""
today yesterday tonight morning afternoon evening night midnight noon week weekend hour hours
day days monday tuesday wednesday thursday friday saturday sunday am pm
""".split())

SEARCH_SYSTEM_PROMPT = (
    'You turn a search request about home security camera events into a JSON query. '
    'Answer only with one JSON object and nothing else.'
)


def admin_timezone() -> ZoneInfo | timezone:
    try:
        from app.alerts import _alert_datetime_prefs

        name = _alert_datetime_prefs()[0]
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc
    except Exception:  # noqa: BLE001 - a missing preference must not break search
        return timezone.utc


def _cameras() -> list[dict[str, str]]:
    try:
        from app.config_facades import effective_cameras_config

        return [
            {'id': str(camera.get('id') or ''), 'name': str(camera.get('name') or camera.get('id') or '')}
            for camera in effective_cameras_config()
            if camera.get('id')
        ]
    except Exception:  # noqa: BLE001
        return []


def build_search_prompt(query: str, now_local: datetime, cameras: list[dict[str, str]]) -> str:
    names = [camera['name'] for camera in cameras]
    return (
        f'Current local date and time: {now_local:%A %Y-%m-%d %H:%M}.\n'
        f'Camera names: {json.dumps(names)}.\n'
        'Return JSON exactly like '
        '{"terms": [["red"], ["car", "vehicle", "ute", "sedan"]], "camera": "Driveway", '
        '"start": "2026-01-31 12:00", "end": "2026-01-31 18:00"}.\n'
        '- "terms": what the event description must mention, one list per concept, each list holding '
        'the lowercase word and up to 5 close synonyms. Leave out words about time, cameras, and '
        'filler such as "show me" or "anyone".\n'
        '- "camera": one of the camera names if the request names or clearly refers to one, else null.\n'
        '- "start"/"end": local times "YYYY-MM-DD HH:MM" if the request mentions a time, else null. '
        'Morning is 05:00-12:00, afternoon 12:00-18:00, evening 18:00-22:00, night 22:00-06:00 '
        '(last night means yesterday 18:00 to today 06:00), "today" is today 00:00 until now.\n'
        f'Request: {json.dumps(query)}'
    )


def _json_object(text: str) -> dict[str, Any]:
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise VerificationError('model reply had no JSON object')
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise VerificationError('model reply was not valid JSON') from exc
    if not isinstance(data, dict):
        raise VerificationError('model reply was not a JSON object')
    return data


def _clean_groups(raw: Any) -> list[list[str]]:
    groups: list[list[str]] = []
    if not isinstance(raw, list):
        return groups
    for group in raw[:MAX_GROUPS]:
        if isinstance(group, str):
            group = [group]
        if not isinstance(group, list):
            continue
        terms: list[str] = []
        for term in group[:MAX_TERMS_PER_GROUP]:
            if not isinstance(term, str):
                continue
            term = ' '.join(term.lower().split())
            if term and _TERM_RE.match(term) and term not in STOPWORDS and term not in terms:
                terms.append(term)
        if terms:
            groups.append(terms)
    return groups


def _match_camera(value: Any, cameras: list[dict[str, str]]) -> dict[str, str] | None:
    wanted = ' '.join(str(value or '').lower().split())
    if not wanted or wanted in {'null', 'none'}:
        return None
    for camera in cameras:
        if wanted in {camera['name'].lower(), camera['id'].lower()}:
            return camera
    return None


def _parse_local(value: Any, tz) -> datetime | None:
    text = str(value or '').strip()
    if not text or text.lower() in {'null', 'none'}:
        return None
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def plan_with_model(query: str, settings: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    tz = admin_timezone()
    cameras = _cameras()
    reply = VisionVerifier(settings).chat([
        {'role': 'system', 'content': SEARCH_SYSTEM_PROMPT},
        {'role': 'user', 'content': build_search_prompt(query, now_utc.astimezone(tz), cameras)},
    ], max_tokens=200)
    data = _json_object(reply)
    groups = _clean_groups(data.get('terms'))
    camera = _match_camera(data.get('camera'), cameras)
    start = _parse_local(data.get('start'), tz)
    end = _parse_local(data.get('end'), tz)
    if start and end and end < start:
        start, end = end, start
    if not groups and camera is None and start is None and end is None:
        raise VerificationError('model produced an empty query')
    return {
        'groups': groups, 'camera': camera,
        'since': start.astimezone(timezone.utc).isoformat() if start else None,
        'until': end.astimezone(timezone.utc).isoformat() if end else None,
        'interpreted_by': 'model',
    }


def plan_with_keywords(query: str, now_utc: datetime) -> dict[str, Any]:
    tz = admin_timezone()
    cameras = _cameras()
    lowered = ' '.join(query.lower().split())
    camera = None
    camera_words: set[str] = set()
    for candidate in sorted(cameras, key=lambda item: -len(item['name'])):
        name = candidate['name'].lower()
        if name and re.search(rf'\b{re.escape(name)}\b', lowered):
            camera = candidate
            camera_words = set(_WORD_RE.findall(name))
            break
    now_local = now_utc.astimezone(tz)
    today = now_local.date()

    def at(day, hour):
        return datetime.combine(day, dtime(hour % 24), tzinfo=tz)

    start = end = None
    yesterday = today - timedelta(days=1)
    if 'last night' in lowered:
        start, end = at(yesterday, 18), at(today, 6)
    elif 'yesterday' in lowered:
        day = yesterday
        start, end = at(day, 0), at(day + timedelta(days=1), 0)
        for word, (h0, h1) in {'morning': (5, 12), 'afternoon': (12, 18), 'evening': (18, 22)}.items():
            if word in lowered:
                start, end = at(day, h0), at(day, h1)
    elif 'this morning' in lowered:
        start, end = at(today, 5), at(today, 12)
    elif 'this afternoon' in lowered:
        start, end = at(today, 12), at(today, 18)
    elif 'tonight' in lowered or 'this evening' in lowered:
        start, end = at(today, 18), None
    elif 'today' in lowered:
        start, end = at(today, 0), None
    words = [
        word for word in _WORD_RE.findall(lowered)
        if word not in STOPWORDS and word not in _TIME_WORDS and word not in camera_words and len(word) > 1
    ]
    groups = [[word] for word in dict.fromkeys(words)][:MAX_GROUPS]
    return {
        'groups': groups, 'camera': camera,
        'since': start.astimezone(timezone.utc).isoformat() if start else None,
        'until': end.astimezone(timezone.utc).isoformat() if end else None,
        'interpreted_by': 'keywords',
    }


def model_available(settings: dict[str, Any]) -> bool:
    return describe_mode(settings) != 'off'


def search_events(
    query: str,
    *,
    limit: int = 50,
    owner_user_id: int | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Interpret ``query`` and return matching events plus the interpretation."""
    query = ' '.join(str(query or '').split())[:MAX_QUERY_CHARS]
    now_utc = now_utc or datetime.now(timezone.utc)
    if not query:
        return {'items': [], 'interpretation': None}
    settings = effective_ai_verification_settings()
    plan = None
    if model_available(settings):
        try:
            plan = plan_with_model(query, settings, now_utc)
        except VerificationError as exc:
            logger.info('AI search fell back to keywords for %r: %s', query, exc)
        except Exception:  # noqa: BLE001 - search must not fail on the model
            logger.exception('AI search interpretation failed; using keywords')
    if plan is None:
        plan = plan_with_keywords(query, now_utc)

    database = _state.database
    camera_ids = [plan['camera']['id']] if plan['camera'] else None
    common = dict(camera_ids=camera_ids, since=plan['since'], until=plan['until'],
                  limit=limit, owner_user_id=owner_user_id)
    relaxed = False
    if plan['groups']:
        items = database.search_event_descriptions(groups=plan['groups'], **common)
        if not items and len(plan['groups']) > 1:
            # Nothing mentions every concept: show events mentioning any.
            merged = list(dict.fromkeys(term for group in plan['groups'] for term in group))
            items = database.search_event_descriptions(groups=[merged], **common)
            relaxed = bool(items)
    else:
        # Only a camera and/or time: every described event in that window.
        items = database.search_event_descriptions(groups=None, **common)
    return {
        'items': items,
        'interpretation': {
            'terms': plan['groups'],
            'camera': plan['camera']['name'] if plan['camera'] else None,
            'since': plan['since'],
            'until': plan['until'],
            'interpreted_by': plan['interpreted_by'],
            'relaxed': relaxed,
        },
    }
