"""Face-recognition settings: defaults, validation, and the status payload.

Mirrors ``app/ai_settings.py``'s ``validate_ai_settings`` contract: an
allow-list of persisted keys, tolerant coercion of form/API values, and an
``HTTPException(400)`` for anything invalid. The settings are admin-only (the
router gates every read/write with ``require_admin``).

Recognition is OFF by default: a fresh install ships with no embedding model
and ``enabled = False``, so nothing runs until an operator deliberately turns
it on and points it at a model.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

# The persisted recognition settings and their defaults. ``model_id`` tags every
# embedding a run produces so the matcher only ever compares vectors from the
# same model (see app/db/persons.py); changing the model means re-enrolling.
DEFAULT_FACE_RECOGNITION_CONFIG: dict[str, Any] = {
    'enabled': False,
    'model_path': '',
    'model_id': 'arcface',
    # Cosine-similarity acceptance threshold. Higher = stricter (fewer false
    # matches, more "unknown"); lower = looser.
    'match_threshold': 0.5,
    # Days to retain recognised-identity data on events. 0 = keep indefinitely.
    # Enforcement lands with the live-recognition wiring; the setting is stored
    # and validated here so the policy is configurable up front.
    'retention_days': 0,
    # Ignore faces smaller than this many pixels on their shorter side before
    # embedding -- tiny/distant faces embed poorly and cause false matches.
    # 0 disables the gate.
    'min_face_pixels': 0,
    # Automatically enrol a fresh embedding from a high-confidence live match to
    # improve accuracy over time. OFF by default: unsupervised enrolment can
    # self-poison an identity if a confident match is wrong, so it is opt-in and,
    # when on, gated on a high score plus a clear margin over the runner-up
    # person (see app/face_identity.py::_maybe_enrich_person).
    'auto_enrich_enabled': False,
}

_ALLOWED_KEYS = frozenset(DEFAULT_FACE_RECOGNITION_CONFIG)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def validate_face_recognition_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalise a face-recognition settings payload.

    Only keys in :data:`DEFAULT_FACE_RECOGNITION_CONFIG` are kept; unknown keys
    are dropped. Missing keys fall back to their default. Raises
    ``HTTPException(400)`` for out-of-range or non-numeric values.
    """
    updated: dict[str, Any] = dict(DEFAULT_FACE_RECOGNITION_CONFIG)
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _ALLOWED_KEYS:
                updated[key] = value

    updated['enabled'] = _coerce_bool(updated.get('enabled', False))
    updated['auto_enrich_enabled'] = _coerce_bool(updated.get('auto_enrich_enabled', False))

    updated['model_path'] = str(updated.get('model_path') or '').strip()
    updated['model_id'] = str(updated.get('model_id') or 'arcface').strip() or 'arcface'

    try:
        threshold = float(updated.get('match_threshold', 0.5))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='match_threshold must be a number.') from exc
    if not 0.0 <= threshold <= 1.0:
        raise HTTPException(status_code=400, detail='match_threshold must be between 0 and 1.')
    updated['match_threshold'] = threshold

    updated['retention_days'] = _validate_non_negative_int(
        updated.get('retention_days', 0), 'retention_days'
    )
    updated['min_face_pixels'] = _validate_non_negative_int(
        updated.get('min_face_pixels', 0), 'min_face_pixels'
    )

    # A configuration that turns recognition on without a model would report a
    # confusing "enabled but nothing happens" state; reject it so the error is
    # visible at save time rather than silently at inference time.
    if updated['enabled'] and not updated['model_path']:
        raise HTTPException(
            status_code=400,
            detail='Enable face recognition only after selecting an embedding model.',
        )
    return updated


def _validate_non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f'{field} must be a non-negative integer.')
    try:
        number = int(value) if value not in (None, '') else 0
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f'{field} must be a non-negative integer.') from exc
    if number < 0:
        raise HTTPException(status_code=400, detail=f'{field} must be a non-negative integer.')
    return number


def face_recognition_status(config: dict[str, Any], service: Any = None, database: Any = None) -> dict[str, Any]:
    """Build the settings-page status payload for face recognition.

    Combines the persisted settings with live service availability (is a model
    loaded? what dimensionality?) and enrolment counts, so the UI can show one
    coherent state without extra round-trips.
    """
    status: dict[str, Any] = dict(config)
    model_loaded = bool(getattr(service, 'available', False)) if service is not None else False
    status['model_loaded'] = model_loaded
    status['embedding_dim'] = getattr(service, 'embedding_dim', None) if service is not None else None
    status['unavailable_reason'] = getattr(service, 'unavailable_reason', None) if service is not None else None

    enrolled_people = 0
    enrolled_faces = 0
    if database is not None:
        try:
            enrolled_people = len(database.list_persons())
            enrolled_faces = database.count_person_faces(model=config.get('model_id'))
        except Exception:  # pragma: no cover - defensive: status must never 500
            pass
    status['enrolled_people'] = enrolled_people
    status['enrolled_faces'] = enrolled_faces
    return status
