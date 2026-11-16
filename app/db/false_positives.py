"""False-positive marking for events, snapshots and recordings.

An event is a false positive when what it detected was not really there (a
shadow read as a person, a bush read as a dog). Each event row carries:

* ``false_positive``: NULL (not judged yet), 1 (false positive) or 0 (real);
* ``false_positive_by``: who decided, ``'ai'`` (the background check in
  app.false_positives) or ``'user'`` (marked on the Events, Snapshots or
  Recordings page);
* ``false_positive_at`` and ``false_positive_reason``.

A user's decision always wins: the AI only ever writes rows that are still
unjudged, so un-marking an AI false positive sticks.

Snapshots are events, so they use the event columns directly. A recording
spans many events, so it has no column of its own: it is a false positive
when at least one linked event is a false positive and no linked event says
otherwise (see :func:`recording_false_positive_status`, and
:func:`recording_false_positive_condition` for the same rule in SQL).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.detection_status import GENERIC_TRIGGER_LABELS
from app.utils import _normalize_iso_to_utc, parse_json_object

FALSE_POSITIVE_SOURCES = ('ai', 'user')
MAX_REASON_CHARS = 300
_SQLITE_BATCH_SIZE = 400

# Detection labels that are not objects: an unjudged event with only these
# (a motion blip inside a clip) never stops its recording counting as a
# false positive. Anything else (an object, a face, a sound) has to be judged.
_NON_OBJECT_LABELS = tuple(sorted(GENERIC_TRIGGER_LABELS - {''}))
_NON_OBJECT_SQL = ', '.join(f"'{label}'" for label in _NON_OBJECT_LABELS)

# Recordings an event belongs to (the same two direct links the retention
# purge uses).
_LINK = '(e.recording_id = {r}.id OR {r}.event_id = e.id)'


def _batched(values: list[int]) -> list[list[int]]:
    return [values[index:index + _SQLITE_BATCH_SIZE] for index in range(0, len(values), _SQLITE_BATCH_SIZE)]


def _counts_sql(alias: str, include_ai: bool) -> str:
    """SQL true when event ``alias`` counts as a false positive."""
    sql = f'COALESCE({alias}.false_positive, 0) = 1'
    if not include_ai:
        sql += f" AND {alias}.false_positive_by = 'user'"
    return f'({sql})'


def _needs_judging_sql(alias: str) -> str:
    """SQL true when an unjudged event ``alias`` keeps its recording real:
    it alerted, is a sound, or detected something other than motion."""
    return (
        f"({alias}.alert_triggered = 1 OR {alias}.source = 'sound' OR EXISTS ("
        f'SELECT 1 FROM detections dfp WHERE dfp.event_id = {alias}.id '
        f"AND lower(dfp.label) NOT IN ({_NON_OBJECT_SQL})))"
    )


def event_false_positive_condition(alias: str, mode: str | None) -> tuple[str, list[Any]] | None:
    """List filter for events/snapshots: ``only`` false positives, or ``hide`` them."""
    if mode == 'only':
        return _counts_sql(alias, True), []
    if mode == 'hide':
        return f'COALESCE({alias}.false_positive, 0) != 1', []
    return None


def recording_false_positive_condition(alias: str, *, include_ai: bool = True) -> str:
    """SQL true when recording ``alias`` is a false positive.

    At least one linked event counts as a false positive, and no linked
    event is real, an AI false positive that does not count (``include_ai``
    off), or unjudged but needing a judgement.
    """
    link = _LINK.format(r=alias)
    counts = _counts_sql('e', include_ai)
    blocking = f'(e.false_positive IS NOT NULL OR {_needs_judging_sql("e")})'
    return (
        f'(EXISTS (SELECT 1 FROM events e WHERE {link} AND {counts}) '
        f'AND NOT EXISTS (SELECT 1 FROM events e WHERE {link} AND NOT {counts} AND {blocking}))'
    )


def recording_false_positive_filter(alias: str, mode: str | None) -> tuple[str, list[Any]] | None:
    """List filter for recordings: ``only`` false positives, or ``hide`` them."""
    if mode == 'only':
        return recording_false_positive_condition(alias), []
    if mode == 'hide':
        return f'NOT {recording_false_positive_condition(alias)}', []
    return None


def _event_needs_judging(event: dict[str, Any], detections: list[dict[str, Any]] | None) -> bool:
    if event.get('alert_triggered') or str(event.get('source') or '') == 'sound':
        return True
    return any(
        str(detection.get('label') or '').strip().lower() not in GENERIC_TRIGGER_LABELS
        for detection in detections or []
    )


def recording_false_positive_status(
    events: list[dict[str, Any]], *, include_ai: bool = True,
) -> dict[str, Any] | None:
    """``{'by': 'user'|'ai'|'mixed', 'reason': ...}`` when the recording whose
    linked ``events`` (each with its ``detections``) these are is a false
    positive, else None. Python twin of recording_false_positive_condition."""
    counted: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for event in events or []:
        key = event.get('id')
        if key in seen:
            continue
        seen.add(key)
        value = event.get('false_positive')
        counts = value == 1 and (include_ai or event.get('false_positive_by') == 'user')
        if counts:
            counted.append(event)
        elif value is not None or _event_needs_judging(event, event.get('detections')):
            return None
    if not counted:
        return None
    sources = {str(event.get('false_positive_by') or 'user') for event in counted}
    by = sources.pop() if len(sources) == 1 else 'mixed'
    reason = next((str(event.get('false_positive_reason')) for event in counted
                   if event.get('false_positive_reason')), '')
    return {'by': by, 'reason': reason}


def _clean_reason(reason: str | None) -> str | None:
    text = ' '.join(str(reason or '').split())[:MAX_REASON_CHARS]
    return text or None


def _rows_to_events(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        event = dict(row)
        event['metadata'] = parse_json_object(event.get('metadata'))
        events.append(event)
    return events


class FalsePositivesMixin:
    """Mark, find and purge false positives (see the module docstring)."""

    def set_event_false_positive(
        self,
        event_id: int,
        value: bool | None,
        *,
        by: str,
        at: str | None = None,
        reason: str | None = None,
        only_if_unjudged: bool = False,
    ) -> bool:
        """Mark (True), un-mark (False) or clear (None) one event.

        ``only_if_unjudged`` is how the AI writes: it never overrides a
        decision already made (in particular, a user's).
        """
        if by not in FALSE_POSITIVE_SOURCES:
            raise ValueError(f'by must be one of {FALSE_POSITIVE_SOURCES}')
        condition = 'id = ?' + (' AND false_positive IS NULL' if only_if_unjudged else '')
        if value is None:
            params: list[Any] = [None, None, None, None]
        else:
            params = [int(bool(value)), by, _normalize_iso_to_utc(at) if at else None, _clean_reason(reason)]
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                'UPDATE events SET false_positive = ?, false_positive_by = ?, false_positive_at = ?, '
                f'false_positive_reason = ? WHERE {condition}',
                [*params, int(event_id)],
            )
            return cursor.rowcount > 0

    def set_recording_false_positive(
        self,
        recording_id: int,
        value: bool | None,
        *,
        by: str,
        at: str | None = None,
        reason: str | None = None,
    ) -> int:
        """Apply one decision to every event linked to a recording.

        Returns how many events changed; 0 when the recording has none (a
        recording on its own has nothing to mark).
        """
        if by not in FALSE_POSITIVE_SOURCES:
            raise ValueError(f'by must be one of {FALSE_POSITIVE_SOURCES}')
        if value is None:
            params: list[Any] = [None, None, None, None]
        else:
            params = [int(bool(value)), by, _normalize_iso_to_utc(at) if at else None, _clean_reason(reason)]
        with self.write_slot(), self.connect() as db:
            cursor = db.execute(
                'UPDATE events SET false_positive = ?, false_positive_by = ?, false_positive_at = ?, '
                'false_positive_reason = ? WHERE recording_id = ? '
                'OR id = (SELECT event_id FROM recordings WHERE id = ?)',
                [*params, int(recording_id), int(recording_id)],
            )
            return int(cursor.rowcount)

    def false_positive_candidates(
        self,
        *,
        since: str,
        until: str,
        labels_excluded: list[str] | tuple[str, ...],
        camera_ids: list[str] | None = None,
        before_id: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Unjudged live-camera events with a snapshot and an object to check,
        newest first, created between ``since`` and ``until``."""
        excluded = [str(label).strip().lower() for label in labels_excluded if str(label).strip()]
        conditions = [
            'e.false_positive IS NULL',
            'e.created_at >= ?',
            'e.created_at <= ?',
            "e.snapshot_path IS NOT NULL AND e.snapshot_path != ''",
            "e.source = 'rtsp'",
        ]
        params: list[Any] = [_normalize_iso_to_utc(since) or since, _normalize_iso_to_utc(until) or until]
        label_filter = 'SELECT 1 FROM detections d WHERE d.event_id = e.id'
        if excluded:
            label_filter += f" AND lower(d.label) NOT IN ({','.join('?' * len(excluded))})"
            params.extend(excluded)
        conditions.append(f'EXISTS ({label_filter})')
        if camera_ids:
            conditions.append(
                "(CASE WHEN json_valid(e.metadata) THEN CAST(json_extract(e.metadata, '$.camera_id') AS TEXT) "
                f"ELSE NULL END) IN ({','.join('?' * len(camera_ids))})"
            )
            params.extend(str(camera_id) for camera_id in camera_ids)
        if before_id is not None:
            conditions.append('e.id < ?')
            params.append(int(before_id))
        with self.connect() as db:
            rows = db.execute(
                f"SELECT e.* FROM events e WHERE {' AND '.join(conditions)} ORDER BY e.id DESC LIMIT ?",
                [*params, max(1, int(limit))],
            ).fetchall()
            return self._events_with_detections(db, rows)

    def false_positive_stats(self, since: str, *, include_ai: bool = True,
                             older_than: str | None = None) -> dict[str, Any]:
        """Counts for the AI page: false positives by source since ``since``,
        events judged real, and what a purge with ``older_than`` would remove."""
        since = _normalize_iso_to_utc(since) or since
        with self.connect() as db:
            rows = db.execute(
                'SELECT false_positive, false_positive_by, COUNT(*) AS total FROM events '
                'WHERE created_at >= ? AND false_positive IS NOT NULL GROUP BY false_positive, false_positive_by',
                [since],
            ).fetchall()
            stats: dict[str, Any] = {'ai': 0, 'user': 0, 'real': 0, 'purgeable_recordings': None,
                                     'purgeable_events': None}
            for row in rows:
                if int(row['false_positive']) == 1:
                    key = 'ai' if row['false_positive_by'] == 'ai' else 'user'
                    stats[key] += int(row['total'])
                else:
                    stats['real'] += int(row['total'])
            if older_than is not None:
                recordings, events = self._purgeable_false_positive_ids(
                    db, _normalize_iso_to_utc(older_than) or older_than, include_ai=include_ai, limit=None,
                )
                stats['purgeable_recordings'] = len(recordings)
                stats['purgeable_events'] = len(events)
        return stats

    @staticmethod
    def _purgeable_false_positive_ids(
        db: sqlite3.Connection, older_than: str, *, include_ai: bool, limit: int | None,
    ) -> tuple[list[int], list[int]]:
        """``(recording_ids, standalone_event_ids)`` a purge would remove."""
        limit_sql = '' if limit is None else f' LIMIT {max(1, int(limit))}'
        recording_ids = [int(row['id']) for row in db.execute(
            f"""SELECT r.id FROM recordings r
                WHERE r.ended_at < ? AND r.source != 'upload'
                  AND {recording_false_positive_condition('r', include_ai=include_ai)}
                ORDER BY r.started_at ASC, r.id ASC{limit_sql}""",
            [older_than],
        ).fetchall()]
        event_ids = [int(row['id']) for row in db.execute(
            f"""SELECT e.id FROM events e
                WHERE e.created_at < ? AND {_counts_sql('e', include_ai)}
                  AND NOT EXISTS (SELECT 1 FROM recordings r WHERE {_LINK.format(r='r')})
                ORDER BY e.created_at ASC, e.id ASC{limit_sql}""",
            [older_than],
        ).fetchall()]
        return recording_ids, event_ids

    def purge_false_positives(
        self, *, older_than: str, include_ai: bool = True, limit: int = 500,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Delete false-positive recordings and events older than ``older_than``.

        A false-positive recording goes with every event linked to it (they
        are false positives or motion blips inside it, see
        recording_false_positive_condition). A false-positive event with no
        recording (a snapshot only) goes on its own. Uploaded clips are
        never purged. Returns the deleted ``(recordings, events)`` rows so
        the caller can remove their files; at most ``limit`` of each per call.
        """
        older_than = _normalize_iso_to_utc(older_than) or older_than
        with self.write_slot(), self.connect() as db:
            recording_ids, standalone_ids = self._purgeable_false_positive_ids(
                db, older_than, include_ai=include_ai, limit=limit,
            )
            recordings: list[dict[str, Any]] = []
            event_ids: list[int] = list(standalone_ids)
            for batch in _batched(recording_ids):
                placeholders = ','.join('?' * len(batch))
                recordings.extend(dict(row) for row in db.execute(
                    f'SELECT * FROM recordings WHERE id IN ({placeholders})', batch,
                ).fetchall())
                event_ids.extend(int(row['id']) for row in db.execute(
                    f'SELECT e.id FROM events e WHERE e.recording_id IN ({placeholders}) '
                    f'OR e.id IN (SELECT event_id FROM recordings WHERE id IN ({placeholders}))',
                    [*batch, *batch],
                ).fetchall())
            event_ids = sorted(set(event_ids))
            events: list[dict[str, Any]] = []
            for batch in _batched(event_ids):
                placeholders = ','.join('?' * len(batch))
                events.extend(_rows_to_events(db.execute(
                    f'SELECT * FROM events WHERE id IN ({placeholders})', batch,
                ).fetchall()))
            # Children first, mirroring the declared CASCADE / SET NULL
            # (foreign_keys is off per connection).
            self._purge_recording_children(db, recording_ids)
            for batch in _batched(recording_ids):
                placeholders = ','.join('?' * len(batch))
                db.execute(f'DELETE FROM recordings WHERE id IN ({placeholders})', batch)
            self._purge_event_children(db, event_ids)
            for batch in _batched(event_ids):
                placeholders = ','.join('?' * len(batch))
                db.execute(f'DELETE FROM events WHERE id IN ({placeholders})', batch)
            return recordings, events
