"""Unit tests for the Stage 2 face-recognition engine (app/face_recognition.py).

Covers the pure-numpy pieces that need neither onnxruntime nor a model file:
embedding serialisation, L2 normalisation, and the cosine-similarity matcher.
The ONNX embedder is exercised for its preprocess maths and its unavailable /
stubbed-session paths.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.face_recognition import (
    FaceEmbedder,
    FaceEmbedderUnavailableError,
    FaceMatcher,
    embedding_from_bytes,
    embedding_to_bytes,
    normalize_embedding,
)


def _unit(vec) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    return arr / np.linalg.norm(arr)


# ---------------------------------------------------------------------------
# Serialisation + normalisation
# ---------------------------------------------------------------------------

def test_embedding_round_trip_is_normalised():
    raw = np.array([3.0, 4.0, 0.0, 0.0], dtype=np.float32)  # norm 5
    data = embedding_to_bytes(raw)
    restored = embedding_from_bytes(data, dim=4)
    # Stored form is L2-normalised (norm 5 -> unit vector).
    assert restored[0] == pytest.approx(0.6, abs=1e-6)
    assert restored[1] == pytest.approx(0.8, abs=1e-6)
    assert float(np.linalg.norm(restored)) == pytest.approx(1.0, abs=1e-6)


def test_embedding_from_bytes_rejects_wrong_dim():
    data = embedding_to_bytes(np.ones(4, dtype=np.float32))
    with pytest.raises(ValueError):
        embedding_from_bytes(data, dim=8)


def test_normalize_zero_vector_is_safe():
    out = normalize_embedding(np.zeros(4, dtype=np.float32))
    assert np.all(out == 0)  # no divide-by-zero blow-up


# ---------------------------------------------------------------------------
# FaceMatcher
# ---------------------------------------------------------------------------

def _row(face_id, person_id, name, vec, dim=4):
    return {
        'face_id': face_id,
        'person_id': person_id,
        'person_name': name,
        'embedding': embedding_to_bytes(np.asarray(vec, dtype=np.float32)),
        'dim': dim,
    }


def test_matcher_finds_enrolled_person():
    rows = [
        _row(1, 10, 'Alex', [1, 0, 0, 0]),
        _row(2, 20, 'Sam', [0, 1, 0, 0]),
    ]
    matcher = FaceMatcher(rows)
    result = matcher.match(np.array([0.9, 0.1, 0, 0], dtype=np.float32), threshold=0.5)
    assert result is not None
    assert result.person_id == 10
    assert result.name == 'Alex'
    assert result.score == pytest.approx(float(_unit([0.9, 0.1, 0, 0])[0]), abs=1e-4)


def test_matcher_returns_none_below_threshold():
    rows = [_row(1, 10, 'Alex', [1, 0, 0, 0])]
    matcher = FaceMatcher(rows)
    # Orthogonal query -> cosine 0 -> unknown.
    assert matcher.match(np.array([0, 1, 0, 0], dtype=np.float32), threshold=0.5) is None


def test_matcher_empty_store_is_unknown():
    matcher = FaceMatcher([])
    assert matcher.is_empty
    assert len(matcher) == 0
    assert matcher.match(np.array([1, 0, 0, 0], dtype=np.float32)) is None


def test_matcher_uses_best_of_multiple_faces_per_person():
    # One person enrolled with two different-looking faces; a query near the
    # second face must still resolve to that person.
    rows = [
        _row(1, 10, 'Alex', [1, 0, 0, 0]),
        _row(2, 10, 'Alex', [0, 0, 1, 0]),
        _row(3, 20, 'Sam', [0, 1, 0, 0]),
    ]
    matcher = FaceMatcher(rows)
    result = matcher.match(np.array([0.1, 0, 0.95, 0], dtype=np.float32), threshold=0.5)
    assert result is not None
    assert result.person_id == 10
    assert result.face_id == 2


def test_matcher_query_dim_mismatch_is_unknown():
    matcher = FaceMatcher([_row(1, 10, 'Alex', [1, 0, 0, 0])])
    # 8-d query against 4-d store -> no match rather than a shape error.
    assert matcher.match(np.ones(8, dtype=np.float32)) is None


def test_matcher_skips_incompatible_dim_rows():
    rows = [
        _row(1, 10, 'Alex', [1, 0, 0, 0], dim=4),
        _row(2, 20, 'Sam', np.ones(8, dtype=np.float32), dim=8),  # different model/dim
    ]
    matcher = FaceMatcher(rows)
    # Matrix is built on the first row's dim (4); the 8-d row is dropped.
    assert matcher.dim == 4
    assert len(matcher) == 1


# ---------------------------------------------------------------------------
# FaceEmbedder
# ---------------------------------------------------------------------------

def test_embedder_unavailable_on_missing_model(tmp_path):
    embedder = FaceEmbedder(tmp_path / 'nope.onnx')
    assert embedder.available is False
    assert 'not found' in (embedder.unavailable_reason or '')
    with pytest.raises(FaceEmbedderUnavailableError):
        embedder.embed(np.zeros((112, 112, 3), dtype=np.uint8))


def test_embedder_preprocess_shape_and_normalisation(tmp_path):
    embedder = FaceEmbedder(tmp_path / 'nope.onnx', input_size=112)
    # A mid-grey 64x64 crop resizes to 112x112 and maps 127->~0 in [-1, 1].
    crop = np.full((64, 64, 3), 127, dtype=np.uint8)
    tensor = embedder.preprocess(crop)
    assert tensor.shape == (1, 3, 112, 112)
    assert tensor.dtype == np.float32
    assert float(tensor.max()) <= 1.0 and float(tensor.min()) >= -1.0
    assert abs(float(tensor.mean())) < 0.02  # centred near zero


def test_embedder_embed_with_stubbed_session(tmp_path, monkeypatch):
    """A stubbed ONNX session drives embed() end to end: preprocess ->
    session.run -> L2-normalised output."""
    embedder = FaceEmbedder(tmp_path / 'nope.onnx')

    class _StubSession:
        def run(self, output_names, feeds):
            # Return an un-normalised 512-d vector; embed() must normalise it.
            vec = np.zeros((1, 512), dtype=np.float32)
            vec[0, 0] = 3.0
            vec[0, 1] = 4.0
            return [vec]

    # Force the instance into an "available" state with the stub.
    embedder.session = _StubSession()
    embedder.unavailable_reason = None
    embedder.input_name = 'input'
    embedder.output_names = ['embedding']

    out = embedder.embed(np.full((112, 112, 3), 127, dtype=np.uint8))
    assert out.shape == (512,)
    assert float(np.linalg.norm(out)) == pytest.approx(1.0, abs=1e-6)
    assert out[0] == pytest.approx(0.6, abs=1e-6)
    assert out[1] == pytest.approx(0.8, abs=1e-6)
