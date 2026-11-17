"""False positives: AI-checked and user-marked detections, and their purge.

Three parts, all off until an admin turns them on (Intelligence → AI):

* **AI check.** A background thread sends every new live object event's
  snapshot to the vision model already used for alert verification
  (app.ai_verification) and asks, for each detected object, whether it is
  really there. When the model says none of them is (a shadow, a bush, a
  reflection), the event is marked a false positive by the AI. It reuses an
  alert verification verdict that already answered for a label, never
  delays an alert (it only runs while the AI queue is idle and yields the
  model to alerts, searches and tests), and never overrides a user.
* **User marking.** Any event, snapshot or recording can be marked (or
  un-marked) from the Events, Snapshots and Recordings pages; see
  app.api.false_positives_router.
* **Purge.** With purging on, false-positive recordings and snapshots older
  than a grace period are deleted by the periodic retention sweep, the same
  way retention deletes old footage.

Storage and the recording rule live in app.db.false_positives.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, UTC
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.runtime_config import cached_snapshot

logger = logging.getLogger('daygle.ai.analysis')

SETTINGS_KEY = 'false_positives'

DEFAULT_FALSE_POSITIVE_SETTINGS: dict[str, Any] = {
    # Ask the AI model about every live object event (needs the model
    # server set up under Intelligence → AI).
    'ai_check': False,
    # Cameras whose events the AI checks; empty = every camera.
    'ai_check_camera_ids': [],
    # Delete false positives once they are older than purge_after_hours.
    'purge_enabled': False,
    'purge_after_hours': 24,
    # Whether the purge deletes AI-marked false positives too, or only the
    # ones a user marked.
    'purge_include_ai': True,
    # Whether the purge deletes false positives a viewer marked, or only
    # those an admin (or the AI) marked.
    'purge_include_viewers': True,
}
MIN_PURGE_AFTER_HOURS = 1
MAX_PURGE_AFTER_HOURS = 24 * 30

# Events from this far back are checked (so turning the check on also covers
# the last day); older ones are left alone.
CHECK_WINDOW_HOURS = 24
# An event is checked once it is this old: its alert verification (if any)
# has had the model first, and its verdict can be reused.
CHECK_MIN_AGE_SECONDS = 20
CHECK_POLL_SECONDS = 15.0
CHECK_PAGE_SIZE = 20
PURGE_BATCH = 500
# Model replies wait this long; background work never holds alerts up (see
# app.ai_verification._ModelGate).
CHECK_TIMEOUT_SECONDS = 90.0

# A false positive is a detection with nothing real there. A detector that
# reads a cat as a dog found something real under the wrong label: that is a
# reclassification, never a false positive (and never purged). So every
# check also asks what a rejected object is instead. A named class
# reclassifies the event's detections to it; the two catch-alls (a real
# animal or vehicle the detector has no class for) keep the label but still
# count as real. The named classes are the detector's own (COCO) classes, so
# a reclassified label is one zones and alert rules can match.
OTHER_ANIMAL = 'another animal'
OTHER_VEHICLE = 'another vehicle'
CATCH_ALL_CANDIDATES = frozenset({OTHER_ANIMAL, OTHER_VEHICLE})
REAL_OBJECT_CANDIDATES: tuple[str, ...] = (
    'person', 'cat', 'dog', 'bird', 'horse', 'sheep', 'cow', 'bear',
    'car', 'truck', 'bus', 'motorcycle', 'bicycle',
    OTHER_ANIMAL, OTHER_VEHICLE,
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def effective_false_positive_settings() -> dict[str, Any]:
    return cached_snapshot(getattr(_state, 'database', None), SETTINGS_KEY, _build_effective_settings)


def _build_effective_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_FALSE_POSITIVE_SETTINGS)
    database = getattr(_state, 'database', None)
    stored = None
    if database is not None:
        try:
            stored = database.get_setting(SETTINGS_KEY)
        except Exception:  # noqa: BLE001 - a settings read must not break the checker or the purge
            stored = None
    if isinstance(stored, dict):
        settings.update({key: value for key, value in stored.items() if key in DEFAULT_FALSE_POSITIVE_SETTINGS})
    return settings


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {'1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'}:
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise _bad(f'{field} must be true or false.')


def validate_false_positive_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a settings payload on top of the current effective settings."""
    unknown = sorted(set(payload) - set(DEFAULT_FALSE_POSITIVE_SETTINGS))
    if unknown:
        raise _bad(f'Unknown false-positive setting: {", ".join(unknown)}.')
    settings = {**effective_false_positive_settings(), **payload}
    settings = {key: settings[key] for key in DEFAULT_FALSE_POSITIVE_SETTINGS}
    for key in ('ai_check', 'purge_enabled', 'purge_include_ai', 'purge_include_viewers'):
        settings[key] = _as_bool(settings[key], key)
    hours = settings['purge_after_hours']
    if isinstance(hours, bool) or not isinstance(hours, int) or not (
            MIN_PURGE_AFTER_HOURS <= hours <= MAX_PURGE_AFTER_HOURS):
        raise _bad(f'purge_after_hours must be a whole number of hours from {MIN_PURGE_AFTER_HOURS} '
                   f'to {MAX_PURGE_AFTER_HOURS}.')
    cameras = settings['ai_check_camera_ids']
    if cameras in (None, ''):
        cameras = []
    if not isinstance(cameras, list) or not all(isinstance(item, str) for item in cameras):
        raise _bad('ai_check_camera_ids must be a list of camera ids.')
    cleaned: list[str] = []
    for item in cameras:
        item = item.strip()
        if item and item not in cleaned:
            cleaned.append(item)
    if len(cleaned) > 200:
        raise _bad('ai_check_camera_ids may contain at most 200 cameras.')
    settings['ai_check_camera_ids'] = cleaned
    return settings


# ---------------------------------------------------------------------------
# AI check
# ---------------------------------------------------------------------------

class _ModelUnavailable(Exception):
    """The model could not be asked; stop this round and try again later."""


def _object_labels(event: dict[str, Any]) -> tuple[list[str], int]:
    """``(labels to ask about, number of distinct object labels)``, strongest first."""
    from app.ai_verification import _UNVERIFIABLE_LABELS, MAX_LABELS_PER_EVENT

    labels: list[str] = []
    detections = sorted(event.get('detections') or [], key=lambda d: -float(d.get('confidence') or 0.0))
    for detection in detections:
        label = str(detection.get('label') or '').strip().lower()
        if label and label not in _UNVERIFIABLE_LABELS and label not in labels:
            labels.append(label)
    return labels[:MAX_LABELS_PER_EVENT], len(labels)


def _reused_verdicts(event: dict[str, Any], labels: list[str]) -> dict[str, dict[str, Any]]:
    """Answers alert verification already got for these labels that settle them.

    A confirmation, or a rejection that named what the object is instead,
    is reused. A bare rejection is reused only when alert verification
    offered every detector class (``asked_instead``, see
    widen_relabel_candidates); otherwise it may have offered no choices (a
    zone with no other rules), so its "no" cannot tell a misread animal from
    an empty scene and is asked again.
    """
    verification = (event.get('metadata') or {}).get('ai_verification') or {}
    answers = verification.get('labels') if isinstance(verification, dict) else None
    asked_instead = isinstance(verification, dict) and verification.get('asked_instead') is True
    reused: dict[str, dict[str, Any]] = {}
    for label in labels:
        answer = (answers or {}).get(label) if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or not isinstance(answer.get('present'), bool):
            continue
        instead = str(answer.get('instead') or '').strip().lower()
        if answer['present'] is False and not instead and not asked_instead:
            continue
        reused[label] = {'present': answer['present'], 'reason': str(answer.get('reason') or ''),
                         'reused': True, **({'instead': instead} if instead else {})}
    return reused


def widen_relabel_candidates(
    candidates: dict[str, list[str]], labels: list[str],
) -> tuple[dict[str, list[str]], bool]:
    """Alert verification's relabel choices (the zone's rule labels) plus
    every detector class, when the AI false-positive check is on.

    Returns ``(candidates, widened)``. The rule labels stay first; a class a
    verified group already covers (``cat`` for an ``animal`` rule) is not
    offered. With the check off the candidates are returned unchanged.
    """
    from app.zone_schema import label_matches

    if not effective_false_positive_settings().get('ai_check'):
        return candidates, False
    widened: dict[str, list[str]] = {}
    for label in labels:
        names = list((candidates or {}).get(label) or [])
        for name in REAL_OBJECT_CANDIDATES:
            if name in names or name == label or label_matches(name, label) or label_matches(label, name):
                continue
            names.append(name)
        widened[label] = names
    return widened, True


def _relabel_candidates(labels: list[str], hints: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Per label, what it may really be instead (never the label itself).
    A class the AI tags already suggested (``hints``) is offered first."""
    candidates: dict[str, list[str]] = {}
    for label in labels:
        names = [name for name in REAL_OBJECT_CANDIDATES if name != label]
        hint = (hints or {}).get(label)
        if hint in names:
            names.remove(hint)
            names.insert(0, hint)
        candidates[label] = names
    return candidates


def tag_contradictions(detections: list[dict[str, Any]], tags: list[str]) -> dict[str, str]:
    """Detected labels the AI tags contradict: ``{'dog': 'cat'}`` when the
    tags name a cat (in any words, see app.label_vocabulary) but no dog.

    Only a class of the same kind counts (an animal for an animal, a vehicle
    for a vehicle): a dog and a car in one picture contradict nothing.
    """
    from app.label_vocabulary import canonical_label, family

    tagged = {canonical_label(tag) for tag in tags or []} - {None}
    found: dict[str, str] = {}
    for detection in detections or []:
        label = str(detection.get('label') or '').strip().lower()
        kind = family(label)
        if not kind or label in tagged or label in found:
            continue
        rival = sorted(name for name in tagged if name != label and family(name) == kind)
        if rival:
            found[label] = rival[0]
    return found


def note_tag_evidence(event_id: int, event: dict[str, Any], tags: list[str]) -> dict[str, str]:
    """Store where an event's AI tags contradict its detections
    (``metadata.ai_tag_evidence``), so the false-positive check looks at it
    first and offers the tagged class first. Returns the contradictions."""
    found = tag_contradictions(event.get('detections') or [], tags)
    if found:
        try:
            _state.database.merge_event_metadata(event_id, {'ai_tag_evidence': {'contradicts': found}})
        except Exception:  # noqa: BLE001 - evidence is a hint; losing it only loses priority
            logger.warning('Could not store AI tag evidence for event %s', event_id, exc_info=True)
    return found


def _relabels(labels: list[str], answers: list[dict[str, Any]]) -> dict[str, str]:
    """``{detected label: what the model says it really is}`` for rejected labels."""
    return {
        label: str(answer['instead'])
        for label, answer in zip(labels, answers, strict=True)
        if answer.get('present') is False and answer.get('instead')
    }


def check_event(event: dict[str, Any], ai_settings: dict[str, Any], *, verifier: Any = None) -> dict[str, Any]:
    """Ask the model whether ``event``'s objects are real (see check_and_describe_event)."""
    return check_and_describe_event(event, ai_settings, verifier=verifier)[0]


def check_and_describe_event(
    event: dict[str, Any], ai_settings: dict[str, Any], *, verifier: Any = None, content: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Ask the model whether ``event``'s objects are real and, with
    ``content``, describe it in the same request.

    Returns ``(check record, description record or None)``; the description
    is None when the request could not carry it (the caller then describes
    the event on its own) or nothing had to be asked.

    Returns the ``ai_false_positive_check`` record; its ``status`` is
    ``false_positive`` (nothing real: the model rejected every object and
    named no real thing instead), ``reclassified`` (a real object under the
    wrong label, e.g. a cat detected as a dog; ``reclassified`` maps each
    label to its class), ``real`` (it confirmed an object, or saw a real
    animal or vehicle the detector has no class for), ``unsure`` or
    ``error`` (left unjudged). Only ``false_positive`` can be purged.
    Raises _ModelUnavailable when the model could not be asked at all.
    """
    from app.ai_verification import VisionVerifier, verify_and_describe_event

    started = time.monotonic()
    labels, total = _object_labels(event)
    record: dict[str, Any] = {
        'model': ai_settings.get('model'),
        'checked_at': datetime.now(UTC).isoformat(),
        'labels': {},
        # Checks that asked what a rejected object is instead (see
        # REAL_OBJECT_CANDIDATES); older verdicts are re-opened on upgrade.
        'asked_instead': True,
    }
    description: dict[str, Any] | None = None
    if not labels:
        record.update(status='error', reason='No object to check.')
        return record, None
    record['labels'].update(_reused_verdicts(event, labels))
    remaining = [label for label in labels if label not in record['labels']]
    if remaining:
        verifier = verifier or VisionVerifier(ai_settings, timeout=CHECK_TIMEOUT_SECONDS, background=True)
        camera_name = str((event.get('metadata') or {}).get('camera_name') or '')
        evidence = ((event.get('metadata') or {}).get('ai_tag_evidence') or {}).get('contradicts') or {}
        result, description = verify_and_describe_event(
            event, remaining, ai_settings, camera_name=camera_name, verifier=verifier,
            candidates=_relabel_candidates(remaining, evidence if isinstance(evidence, dict) else None),
            content=content,
            camera_id=(event.get('metadata') or {}).get('camera_id'),
        )
        if result.get('_model_unavailable'):
            raise _ModelUnavailable(next(
                (str(value.get('error')) for value in result['labels'].values() if value.get('error')),
                'model unavailable'))
        if result.get('status') == 'skipped':
            record.update(status='error', reason=str(result.get('reason') or 'No snapshot to check.'))
            return record, None
        for label, answer in (result.get('labels') or {}).items():
            record['labels'][label] = {key: value for key, value in answer.items()
                                       if key in ('present', 'reason', 'unsure', 'error', 'instead')}
    record['latency_ms'] = int((time.monotonic() - started) * 1000)
    answers = [record['labels'].get(label) or {} for label in labels]
    relabels = _relabels(labels, answers)
    reclassified = {label: name for label, name in relabels.items() if name not in CATCH_ALL_CANDIDATES}
    if reclassified:
        # Something real was there, just not what the detector said: the
        # labels are corrected, and it is never a false positive.
        record['reclassified'] = reclassified
        record['status'] = 'reclassified'
        record['reason'] = reclassified_reason(reclassified)
    elif any(answer.get('present') is True for answer in answers) or relabels:
        record['status'] = 'real'
        if relabels:
            record['reason'] = '; '.join(
                f"AI saw {name}, not {_article(label)} {label.replace('_', ' ')}" for label, name in relabels.items()
            )
    elif total <= len(labels) and all(answer.get('present') is False for answer in answers):
        record['status'] = 'false_positive'
        record['reason'] = '; '.join(
            f"{label.replace('_', ' ')}: {answer.get('reason') or 'not there'}"
            for label, answer in zip(labels, answers, strict=True)
        )
    elif any(answer.get('unsure') for answer in answers) or total > len(labels):
        # Unchecked labels (more objects than one check asks about) cannot
        # be called false positives either.
        record['status'] = 'unsure'
    else:
        record['status'] = 'error'
    return record, description


def reclassified_reason(reclassified: dict[str, str]) -> str:
    """"Reclassified dog as cat." for each corrected label."""
    return 'Reclassified ' + '; '.join(
        f"{label.replace('_', ' ')} as {name}" for label, name in reclassified.items()
    ) + '.'


def _article(label: str) -> str:
    return 'an' if label[:1].lower() in 'aeiou' else 'a'


def store_check(event_id: int, record: dict[str, Any], *, camera_id: Any = None, camera_name: Any = None) -> bool:
    """Save the check on the event and mark it, unless a user got there first.

    ``real``, ``reclassified`` and ``unsure`` are stored as "not a false
    positive" so they are never purged and not asked about again; ``error``
    leaves the event unjudged. A reclassified event's detections (and its
    recording's labels) are relabelled to what the object really is; the
    original labels stay in the check record (``reclassified``).
    """
    database = _state.database
    try:
        database.merge_event_metadata(event_id, {'ai_false_positive_check': record})
    except Exception:  # noqa: BLE001 - the verdict below is what matters; metadata is a detail
        logger.warning('Could not store the AI false-positive check for event %s', event_id, exc_info=True)
    status = record.get('status')
    if status not in ('false_positive', 'real', 'unsure', 'reclassified'):
        return False
    try:
        from app.ai_verification import detection_verdicts

        event = database.get_event(event_id) or {}
        verdicts = detection_verdicts(record.get('labels') or {}, event.get('detections') or [])
        if verdicts:
            database.set_detection_verdicts(event_id, verdicts, source='check')
    except Exception:  # noqa: BLE001 - per-detection verdicts are a detail of the stored check
        logger.warning('Could not store detection verdicts for event %s', event_id, exc_info=True)
    reason = {
        'false_positive': record.get('reason'),
        'reclassified': record.get('reason'),
        'unsure': 'AI could not tell.',
    }.get(status) or record.get('reason') or 'AI confirmed the detection.'

    marked = database.set_event_false_positive(
        event_id, status == 'false_positive', by='ai', at=record.get('checked_at'), reason=reason,
        only_if_unjudged=True,
    )
    if marked and status == 'reclassified':
        # Only an event the AI could still judge is relabelled: a user who
        # already called it a false positive (or real) has the last word.
        try:
            database.reclassify_event_detections(event_id, record.get('reclassified') or {},
                                                 reason=record.get('reason'))
        except Exception:  # noqa: BLE001 - a failed relabel must not undo the verdict already stored
            logger.warning('Could not reclassify event %s', event_id, exc_info=True)
        from app.diagnostics import log_ai_diagnostic

        logger.info('AI reclassified event %s: %s', event_id, reason)
        log_ai_diagnostic(
            'ai_reclassified',
            f'AI reclassified event #{event_id} ({reason})'[:500],
            camera_id=camera_id, camera_name=camera_name,
            details={'event_id': event_id, 'reclassified': record.get('reclassified')},
        )
    if marked and status == 'false_positive':
        from app.diagnostics import log_ai_diagnostic

        logger.info('AI marked event %s a false positive: %s', event_id, reason)
        log_ai_diagnostic(
            'ai_false_positive',
            f'AI marked event #{event_id} a false positive ({reason})'[:500],
            camera_id=camera_id, camera_name=camera_name, details={'event_id': event_id},
        )
    return marked


def combined_check_applies(event: dict[str, Any]) -> bool:
    """Whether a description of ``event`` should also run its false-positive
    check, so both come from one model request (see
    app.ai_verification._describe_and_store_claimed): the check is on, the
    camera is in its scope, and the event is an unjudged live object event."""
    from app.ai_verification import _UNVERIFIABLE_LABELS

    settings = effective_false_positive_settings()
    if not settings.get('ai_check') or event.get('false_positive') is not None:
        return False
    if str(event.get('source') or '') != 'rtsp' or not event.get('snapshot_path'):
        return False
    cameras = settings.get('ai_check_camera_ids') or []
    camera_id = str((event.get('metadata') or {}).get('camera_id') or '')
    if cameras and camera_id not in cameras:
        return False
    return any(
        str(d.get('label') or '').strip().lower() not in _UNVERIFIABLE_LABELS
        for d in event.get('detections') or []
    )


def check_with_description(
    event: dict[str, Any], ai_settings: dict[str, Any], *, content: str, background: bool,
) -> dict[str, Any] | None:
    """Run the false-positive check in the same request as ``event``'s
    description and store its verdict; returns the description record, or
    None when the description must be asked for separately. Never raises:
    a failure leaves the event to the background checker."""
    from app.ai_verification import BACKGROUND_DESCRIBE_TIMEOUT_SECONDS, VisionVerifier

    timeout = None
    if background:
        timeout = max(float(ai_settings.get('timeout_seconds') or 20), BACKGROUND_DESCRIBE_TIMEOUT_SECONDS)
    verifier = VisionVerifier(ai_settings, timeout=timeout, background=background)
    try:
        record, description = check_and_describe_event(event, ai_settings, verifier=verifier, content=content)
    except _ModelUnavailable as exc:
        logger.info('Combined AI check unavailable for event %s: %s', event.get('id'), exc)
        return None
    except Exception:  # noqa: BLE001 - the description path must keep working
        logger.exception('Combined AI check failed for event %s', event.get('id'))
        return None
    event_id = int(event['id'])
    with _attempted_lock:
        _attempted.add(event_id)
    metadata = event.get('metadata') or {}
    store_check(event_id, record, camera_id=metadata.get('camera_id'), camera_name=metadata.get('camera_name'))
    return description


_attempted_lock = threading.Lock()
_attempted: set[int] = set()


def _ai_busy() -> bool:
    from app.ai_verification import _ai_pool_busy, description_backfill_status, model_cooling_down

    return bool(_ai_pool_busy() or model_cooling_down() or description_backfill_status().get('running'))


def _model_configured(ai_settings: dict[str, Any]) -> bool:
    return bool(str(ai_settings.get('server_url') or '').strip() and str(ai_settings.get('model') or '').strip())


def _next_candidate(settings: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """The next unjudged event to check: first those whose AI tags
    contradict a detection (most likely misread), then the rest, newest first."""
    from app.ai_verification import _UNVERIFIABLE_LABELS

    since = (now - timedelta(hours=CHECK_WINDOW_HOURS)).isoformat()
    until = (now - timedelta(seconds=CHECK_MIN_AGE_SECONDS)).isoformat()
    for flagged_only in (True, False):
        before_id = None
        while True:
            page = _state.database.false_positive_candidates(
                since=since, until=until, labels_excluded=sorted(_UNVERIFIABLE_LABELS),
                camera_ids=settings.get('ai_check_camera_ids') or None,
                before_id=before_id, limit=CHECK_PAGE_SIZE, flagged_only=flagged_only,
            )
            with _attempted_lock:
                for event in page:
                    event_id = int(event['id'])
                    if event_id not in _attempted:
                        if len(_attempted) > 20000:
                            _attempted.clear()
                        _attempted.add(event_id)
                        return event
            if len(page) < CHECK_PAGE_SIZE:
                break
            before_id = int(page[-1]['id'])
    return None


def run_checks_once(*, max_events: int = 50, verifier: Any = None, ignore_busy: bool = False,
                    stop: threading.Event | None = None) -> int:
    """Check up to ``max_events`` unjudged events while the AI is idle.

    Returns how many were checked. Each event is tried once per process, so
    one that cannot be checked (an unreadable reply) is not retried forever;
    when the model cannot be reached the event is released for a later round.
    ``stop`` (the worker's shutdown flag) ends the round between events.
    """
    from app.ai_verification import effective_ai_verification_settings

    settings = effective_false_positive_settings()
    if not settings.get('ai_check') or getattr(_state, 'database', None) is None:
        return 0
    ai_settings = effective_ai_verification_settings()
    if not _model_configured(ai_settings):
        return 0
    checked = 0
    while checked < max_events and not (stop is not None and stop.is_set()):
        if not ignore_busy and _ai_busy():
            break
        event = _next_candidate(settings, datetime.now(UTC))
        if event is None:
            break
        event_id = int(event['id'])
        try:
            record = check_event(event, ai_settings, verifier=verifier)
        except _ModelUnavailable as exc:
            with _attempted_lock:
                _attempted.discard(event_id)
            logger.info('AI false-positive check paused: %s', exc)
            break
        except Exception:  # noqa: BLE001 - one bad event must not stop the checker
            logger.exception('AI false-positive check failed for event %s', event_id)
            continue
        metadata = event.get('metadata') or {}
        store_check(event_id, record, camera_id=metadata.get('camera_id'), camera_name=metadata.get('camera_name'))
        checked += 1
    return checked


_stop = threading.Event()
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def _run_worker() -> None:
    while not _stop.wait(CHECK_POLL_SECONDS):
        try:
            run_checks_once(stop=_stop)
        except Exception:  # noqa: BLE001 - a background helper must never crash the app
            logger.exception('AI false-positive checker failed')


def start_checker() -> bool:
    """Start the background checker (idle unless the AI check is turned on)."""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return False
        _stop.clear()
        _worker = threading.Thread(target=_run_worker, name='ai-false-positive-check', daemon=True)
        _worker.start()
        return True


def stop_checker(timeout: float = 2.0) -> None:
    global _worker
    _stop.set()
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is not None and worker.is_alive() and worker is not threading.current_thread():
        worker.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

def _purge_cutoff(settings: dict[str, Any]) -> str:
    hours = settings.get('purge_after_hours')
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < MIN_PURGE_AFTER_HOURS:
        # A hand-edited or restored value never shortens the grace period.
        hours = DEFAULT_FALSE_POSITIVE_SETTINGS['purge_after_hours']
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def purge_false_positives_by_policy(*, force: bool = False) -> dict[str, Any]:
    """Delete false positives older than the grace period.

    Runs from the periodic retention sweep when purging is on; ``force``
    (the Purge Now button) runs it whatever the switch says. The grace
    period always applies, so a fresh false positive can still be reviewed.
    """
    from app.recording_files import delete_recording_files, delete_snapshot_files

    result = {'recordings_purged': 0, 'events_purged': 0, 'recording_bytes_deleted': 0,
              'snapshot_files_deleted': 0, 'snapshot_bytes_deleted': 0}
    settings = effective_false_positive_settings()
    database = getattr(_state, 'database', None)
    if database is None or (not force and not settings.get('purge_enabled')):
        return result
    from app.media_utils import safe_storage_path

    cutoff = _purge_cutoff(settings)
    include_ai = bool(settings.get('purge_include_ai', True))
    include_viewers = bool(settings.get('purge_include_viewers', True))
    while True:
        recordings, events = database.purge_false_positives(
            older_than=cutoff, include_ai=include_ai, include_viewers=include_viewers, limit=PURGE_BATCH,
        )
        for recording in recordings:
            path = safe_storage_path(recording.get('file_path'), roots=('recordings_dir',))
            if path is not None and path.is_file():
                result['recording_bytes_deleted'] += path.stat().st_size
        delete_recording_files(recordings)
        files, size = delete_snapshot_files(events)
        result['recordings_purged'] += len(recordings)
        result['events_purged'] += len(events)
        result['snapshot_files_deleted'] += files
        result['snapshot_bytes_deleted'] += size
        if len(recordings) < PURGE_BATCH and len(events) < PURGE_BATCH:
            break
    if result['recordings_purged'] or result['events_purged']:
        logger.info('Purged %d false-positive recording(s) and %d event(s)',
                    result['recordings_purged'], result['events_purged'])
    return result


def false_positive_stats(days: int = 7) -> dict[str, Any]:
    """Counts for the AI page: marked in the last ``days`` and waiting to purge."""
    settings = effective_false_positive_settings()
    since = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).isoformat()
    stats = _state.database.false_positive_stats(
        since, include_ai=bool(settings.get('purge_include_ai', True)),
        include_viewers=bool(settings.get('purge_include_viewers', True)), older_than=_purge_cutoff(settings),
    )
    return {'days': int(days), **stats}
