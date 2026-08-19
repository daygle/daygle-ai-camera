"""Unit tests for Stage 2b: recognition settings + service (no app harness)."""
from __future__ import annotations

import numpy as np
import pytest
from fastapi import HTTPException

from app.face_recognition import embedding_to_bytes
from app.face_recognition_service import FaceRecognitionService
from app.face_recognition_settings import (
    DEFAULT_FACE_RECOGNITION_CONFIG,
    face_recognition_status,
    validate_face_recognition_settings,
)


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------

def test_validate_defaults_when_empty():
    out = validate_face_recognition_settings({})
    assert out == DEFAULT_FACE_RECOGNITION_CONFIG
    assert out['enabled'] is False


def test_validate_drops_unknown_keys_and_coerces_bools():
    out = validate_face_recognition_settings(
        {'enabled': 'yes', 'alert_unknown': 'on', 'model_path': 'models/arcface.onnx', 'evil': 1}
    )
    assert out['enabled'] is True
    assert out['alert_unknown'] is True
    assert 'evil' not in out


def test_validate_rejects_enabled_without_model():
    with pytest.raises(HTTPException) as exc:
        validate_face_recognition_settings({'enabled': True, 'model_path': ''})
    assert exc.value.status_code == 400


def test_validate_threshold_bounds():
    assert validate_face_recognition_settings({'match_threshold': 0.7})['match_threshold'] == 0.7
    for bad in (-0.1, 1.1, 'nope'):
        with pytest.raises(HTTPException):
            validate_face_recognition_settings({'match_threshold': bad})


def test_validate_non_negative_ints():
    out = validate_face_recognition_settings({'retention_days': '30', 'min_face_pixels': 40})
    assert out['retention_days'] == 30
    assert out['min_face_pixels'] == 40
    for field in ('retention_days', 'min_face_pixels'):
        with pytest.raises(HTTPException):
            validate_face_recognition_settings({field: -1})


# ---------------------------------------------------------------------------
# FaceRecognitionService
# ---------------------------------------------------------------------------

class _FakeDB:
    """Minimal enrolment DB stand-in for the service."""

    def __init__(self, rows=None):
        self._rows = rows or []

    def load_face_embeddings(self, model):
        return [r for r in self._rows if r.get('_model', 'arcface') == model]

    def list_persons(self):
        seen = {r['person_id'] for r in self._rows}
        return [{'id': pid} for pid in seen]

    def count_person_faces(self, *, model=None):
        if model is None:
            return len(self._rows)
        return len([r for r in self._rows if r.get('_model', 'arcface') == model])


class _StubEmbedder:
    available = True
    embedding_dim = 4

    def __init__(self, vector):
        self._vector = np.asarray(vector, dtype=np.float32)

    def embed(self, face_bgr):
        return self._vector / np.linalg.norm(self._vector)


def _row(face_id, person_id, name, vec, model='arcface'):
    return {
        'face_id': face_id,
        'person_id': person_id,
        'person_name': name,
        'embedding': embedding_to_bytes(np.asarray(vec, dtype=np.float32)),
        'dim': 4,
        '_model': model,
    }


def _service(config, db, embedder=None):
    """Build a service and (optionally) force a stub embedder into it."""
    svc = FaceRecognitionService(config, db)
    if embedder is not None:
        svc.embedder = embedder
        svc.unavailable_reason = None
    return svc


def test_service_disabled_is_unavailable():
    svc = FaceRecognitionService({'enabled': False}, _FakeDB())
    assert svc.available is False
    assert svc.recognize(np.zeros((112, 112, 3), dtype=np.uint8)) is None


def test_service_enabled_without_loadable_model_is_unavailable(tmp_path):
    svc = FaceRecognitionService(
        {'enabled': True, 'model_path': str(tmp_path / 'missing.onnx')}, _FakeDB()
    )
    assert svc.available is False
    assert 'not found' in (svc.unavailable_reason or '')


def test_service_recognizes_enrolled_person():
    db = _FakeDB([_row(1, 10, 'Alex', [1, 0, 0, 0]), _row(2, 20, 'Sam', [0, 1, 0, 0])])
    svc = _service(
        {'enabled': True, 'model_path': 'x', 'match_threshold': 0.5},
        db,
        embedder=_StubEmbedder([0.9, 0.1, 0, 0]),
    )
    assert svc.available is True
    result = svc.recognize(np.zeros((112, 112, 3), dtype=np.uint8))
    assert result is not None and result.person_id == 10 and result.name == 'Alex'


def test_service_unknown_below_threshold():
    db = _FakeDB([_row(1, 10, 'Alex', [1, 0, 0, 0])])
    svc = _service(
        {'enabled': True, 'model_path': 'x', 'match_threshold': 0.9},
        db,
        embedder=_StubEmbedder([0, 1, 0, 0]),  # orthogonal -> cosine 0
    )
    assert svc.recognize(np.zeros((112, 112, 3), dtype=np.uint8)) is None


def test_service_min_face_pixels_gate():
    db = _FakeDB([_row(1, 10, 'Alex', [1, 0, 0, 0])])
    svc = _service(
        {'enabled': True, 'model_path': 'x', 'min_face_pixels': 80},
        db,
        embedder=_StubEmbedder([1, 0, 0, 0]),
    )
    # 40x40 crop is below the 80px gate -> skipped (None) even though it matches.
    assert svc.recognize(np.zeros((40, 40, 3), dtype=np.uint8)) is None
    # A large enough crop recognises.
    assert svc.recognize(np.zeros((100, 100, 3), dtype=np.uint8)) is not None


def test_service_refresh_picks_up_new_enrollments():
    db = _FakeDB([])
    svc = _service(
        {'enabled': True, 'model_path': 'x'}, db, embedder=_StubEmbedder([1, 0, 0, 0])
    )
    assert svc.recognize(np.zeros((112, 112, 3), dtype=np.uint8)) is None  # empty store
    db._rows.append(_row(1, 10, 'Alex', [1, 0, 0, 0]))
    svc.refresh_matcher()
    result = svc.recognize(np.zeros((112, 112, 3), dtype=np.uint8))
    assert result is not None and result.person_id == 10


def test_service_only_matches_active_model():
    # An embedding enrolled under a different model must not be matched.
    db = _FakeDB([_row(1, 10, 'Alex', [1, 0, 0, 0], model='other')])
    svc = _service(
        {'enabled': True, 'model_path': 'x', 'model_id': 'arcface'},
        db,
        embedder=_StubEmbedder([1, 0, 0, 0]),
    )
    assert svc.recognize(np.zeros((112, 112, 3), dtype=np.uint8)) is None
    assert svc.enrolled_count == 0


def test_status_payload_reports_counts_and_availability():
    db = _FakeDB([_row(1, 10, 'Alex', [1, 0, 0, 0])])
    svc = _service({'enabled': True, 'model_path': 'x'}, db, embedder=_StubEmbedder([1, 0, 0, 0]))
    status = face_recognition_status({'enabled': True, 'model_id': 'arcface'}, svc, db)
    assert status['model_loaded'] is True
    assert status['embedding_dim'] == 4
    assert status['enrolled_people'] == 1
    assert status['enrolled_faces'] == 1
