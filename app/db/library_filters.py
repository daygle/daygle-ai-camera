"""SQL fragments shared by the Events, Snapshots and Recordings list queries.

The three library pages share one filter bar (web/library_filters.js), so the
filters that bar sends - a keyword search, a camera, a time window - must mean
the same thing against every list. Keeping the SQL here stops the three
queries from drifting apart.

Every helper returns ``(sql, params)`` with ``?`` placeholders; callers AND the
SQL into their own ``WHERE`` clause.
"""

from __future__ import annotations

from typing import Any

# A keyword query is split on whitespace and every word must match (AND), so
# "red car driveway" narrows rather than widens. The cap bounds the SQL a
# single request can build.
MAX_QUERY_WORDS = 8

# Event metadata fields a keyword can hit: camera, the sound class, the AI
# write-up and its tags, and recognised face names. Matching named JSON paths
# (not the raw metadata blob) keeps a word like "camera" from matching every
# row through the "camera_id" key itself.
_EVENT_METADATA_PATHS = (
    '$.camera_name',
    '$.camera_id',
    '$.label',
    '$.class_label',
    '$.ai_description.text',
    '$.ai_description.tags',
    '$.face_identities',
)


def query_words(query: str | None) -> list[str]:
    """Lower-cased search words from a free-text query, capped."""
    words = [word for word in str(query or '').strip().lower().split() if word]
    return words[:MAX_QUERY_WORDS]


def _like_pattern(word: str) -> str:
    escaped = word.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def event_word_match(alias: str, word: str) -> tuple[str, list[Any]]:
    """SQL true when event ``alias`` mentions ``word`` anywhere searchable:
    a detection label or zone, the event source, or a metadata field above."""
    pattern = _like_pattern(word)
    metadata_clauses = [
        f"lower(COALESCE(json_extract({alias}.metadata, '{path}'), '')) LIKE ? ESCAPE '\\'"
        for path in _EVENT_METADATA_PATHS
    ]
    # CASE (not AND) guarantees json_extract never runs on a malformed blob,
    # which would raise and fail the whole list request.
    clauses = [
        f"(CASE WHEN json_valid({alias}.metadata) THEN ({' OR '.join(metadata_clauses)}) ELSE 0 END)",
        f"lower(COALESCE({alias}.source, '')) LIKE ? ESCAPE '\\'",
        f'EXISTS (SELECT 1 FROM detections dq WHERE dq.event_id = {alias}.id AND '
        "(lower(dq.label) LIKE ? ESCAPE '\\' OR lower(COALESCE(dq.zone_name, '')) LIKE ? ESCAPE '\\'))",
    ]
    return '(' + ' OR '.join(clauses) + ')', [pattern] * (len(metadata_clauses) + 3)


def event_query_condition(alias: str, query: str | None) -> tuple[str, list[Any]] | None:
    """Every word of ``query`` must match event ``alias``; None for no query."""
    words = query_words(query)
    if not words:
        return None
    parts: list[str] = []
    params: list[Any] = []
    for word in words:
        sql, word_params = event_word_match(alias, word)
        parts.append(sql)
        params.extend(word_params)
    return '(' + ' AND '.join(parts) + ')', params


def recording_query_condition(alias: str, query: str | None) -> tuple[str, list[Any]] | None:
    """Every word of ``query`` must match recording ``alias``: its camera,
    trigger, a detection or AI label, or any event linked to the clip."""
    words = query_words(query)
    if not words:
        return None
    parts: list[str] = []
    params: list[Any] = []
    for word in words:
        pattern = _like_pattern(word)
        event_sql, event_params = event_word_match('eq', word)
        parts.append(
            '('
            f"lower(COALESCE({alias}.camera_id, '')) LIKE ? ESCAPE '\\'"
            f" OR lower(COALESCE({alias}.trigger_label, '')) LIKE ? ESCAPE '\\'"
            f" OR EXISTS (SELECT 1 FROM recording_labels rlq WHERE rlq.recording_id = {alias}.id"
            " AND lower(rlq.label) LIKE ? ESCAPE '\\')"
            f' OR EXISTS (SELECT 1 FROM events eq WHERE (eq.id = {alias}.event_id OR eq.recording_id = {alias}.id)'
            f' AND {event_sql})'
            ')'
        )
        params.extend([pattern, pattern, pattern, *event_params])
    return '(' + ' AND '.join(parts) + ')', params


def event_camera_condition(alias: str, camera_id: str | None) -> tuple[str, list[Any]] | None:
    """Events carry their camera in metadata; match the configured id."""
    if not camera_id:
        return None
    return (
        f"(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.camera_id') END) = ?",
        [str(camera_id)],
    )


def event_alerted_condition(alias: str) -> str:
    return f'EXISTS (SELECT 1 FROM alert_history ah WHERE ah.event_id = {alias}.id)'


def _safe_metadata(alias: str) -> str:
    """The event's metadata, or an empty object when the blob is malformed,
    so json_each / json_extract over it can never raise."""
    return f"(CASE WHEN json_valid({alias}.metadata) THEN {alias}.metadata ELSE '{{}}' END)"


def event_face_condition(alias: str, face: str | None) -> tuple[str, list[Any]] | None:
    """Match the Face filter values the UI sends (see matchesFaceFilter in
    web/utils.js): ``any``, ``unknown``, ``id:<person_id>`` or ``name:<lower>``."""
    value = str(face or '').strip()
    if not value:
        return None
    metadata = _safe_metadata(alias)
    people = f"json_each({metadata}, '$.face_identities.people')"
    unknown = f"COALESCE(CAST(json_extract({metadata}, '$.face_identities.unknown') AS INTEGER), 0) > 0"
    if value == 'any':
        return f'({unknown} OR EXISTS (SELECT 1 FROM {people}))', []
    if value == 'unknown':
        return unknown, []
    if value.startswith('id:'):
        return (
            f"EXISTS (SELECT 1 FROM {people} fp WHERE CAST(json_extract(fp.value, '$.person_id') AS TEXT) = ?)",
            [value[3:]],
        )
    if value.startswith('name:'):
        return (
            f"EXISTS (SELECT 1 FROM {people} fp WHERE json_extract(fp.value, '$.person_id') IS NULL"
            " AND lower(trim(COALESCE(json_extract(fp.value, '$.name'), ''))) = ?)",
            [value[5:].strip().lower()],
        )
    # Unknown token: match nothing rather than silently ignoring the filter.
    return '0', []


def recording_face_condition(alias: str, face: str | None) -> tuple[str, list[Any]] | None:
    """A recording matches when any event linked to the clip matches."""
    inner = event_face_condition('ef', face)
    if inner is None:
        return None
    return (
        f'EXISTS (SELECT 1 FROM events ef WHERE (ef.id = {alias}.event_id OR ef.recording_id = {alias}.id)'
        f' AND {inner[0]})',
        inner[1],
    )


def face_facet_rows(db, event_scope_sql: str, params: list[Any]) -> dict[str, Any]:
    """Count recognised people, unknown faces and any-face rows across the
    events selected by ``event_scope_sql`` (a SELECT of event ids)."""
    people_rows = db.execute(
        f'''SELECT json_extract(fp.value, '$.person_id') AS person_id,
                   trim(COALESCE(json_extract(fp.value, '$.name'), '')) AS name,
                   COUNT(DISTINCT fe.id) AS count
            FROM events fe, json_each({_safe_metadata('fe')}, '$.face_identities.people') fp
            WHERE fe.id IN ({event_scope_sql})
            GROUP BY person_id, CASE WHEN person_id IS NULL THEN lower(name) END''',
        params,
    ).fetchall()
    unknown_row = db.execute(
        f'''SELECT COUNT(*) AS count FROM events fe WHERE fe.id IN ({event_scope_sql})
            AND COALESCE(CAST(json_extract({_safe_metadata('fe')}, '$.face_identities.unknown') AS INTEGER), 0) > 0''',
        params,
    ).fetchone()
    people: dict[str, dict[str, Any]] = {}
    for row in people_rows:
        name = str(row['name'] or '').strip()
        if row['person_id'] is not None:
            key = f"id:{row['person_id']}"
        elif name:
            key = f'name:{name.lower()}'
        else:
            continue
        entry = people.setdefault(key, {'value': key, 'name': name or 'Unknown person', 'count': 0})
        entry['count'] += int(row['count'] or 0)
        if name and entry['name'] == 'Unknown person':
            entry['name'] = name
    return {
        'people': sorted(people.values(), key=lambda item: item['name'].lower()),
        'unknown': int(unknown_row['count'] or 0) if unknown_row else 0,
    }
