"""Face recognition engine (Stage 2): embeddings + matching.

This module is the dependency-light core of "recognise *which* person a face
belongs to". It is deliberately decoupled from the detection pipeline and the
web layer:

- :class:`FaceEmbedder` wraps an ArcFace-style ONNX model that turns an aligned
  face crop into a fixed-length, L2-normalised embedding vector.
- :func:`embedding_to_bytes` / :func:`embedding_from_bytes` serialise those
  vectors for the ``person_faces`` BLOB column.
- :class:`FaceMatcher` compares a query embedding against the enrolled vectors
  (loaded from the database) using cosine similarity and returns the best
  matching person, or ``None`` when nothing clears the threshold ("unknown").

Recognising a face is not the same as detecting one: detection (Stage 1) finds
*where* faces are; this module decides *who* they are, and only for people an
operator has explicitly enrolled.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger('daygle.ai')

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised only in minimal installs
    np = None  # type: ignore[assignment]


# ArcFace / InsightFace recognition models take a 112x112 aligned RGB crop and
# emit a 512-d embedding. These are the conventional defaults; a model that
# differs is honoured via the constructor + the real ONNX output shape.
_DEFAULT_INPUT_SIZE = 112
_DEFAULT_EMBEDDING_DIM = 512
# Pixel normalisation used by ArcFace: map [0, 255] -> [-1, 1].
_PIXEL_MEAN = 127.5
_PIXEL_SCALE = 127.5
# Default cosine-similarity acceptance threshold. ArcFace cosine scores for the
# same identity are typically well above this; different identities fall below.
# Exposed as a parameter so a deployment can tune precision/recall.
_DEFAULT_MATCH_THRESHOLD = 0.5

_BASE_DIR = Path(__file__).resolve().parent.parent


class FaceEmbedderUnavailableError(RuntimeError):
    """Raised when the configured embedding model cannot run inference."""


def _require_numpy():
    if np is None:
        raise FaceEmbedderUnavailableError(
            "numpy is not installed. Install requirements.txt or run pip install numpy."
        )
    return np


def _resolve_project_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _BASE_DIR / candidate


def normalize_embedding(vector: Any) -> Any:
    """Return the L2-normalised copy of ``vector`` (float32).

    Cosine similarity between two unit vectors is a plain dot product, so every
    stored and queried embedding is normalised once here. A zero (or numerically
    tiny) vector is returned unchanged rather than divided by ~0.
    """
    npmod = _require_numpy()
    vec = npmod.asarray(vector, dtype=npmod.float32).reshape(-1)
    norm = float(npmod.linalg.norm(vec))
    if norm <= 1e-10:
        return vec
    return (vec / norm).astype(npmod.float32)


def embedding_to_bytes(vector: Any) -> bytes:
    """Serialise an embedding to little-endian float32 bytes for storage.

    The vector is L2-normalised first so every row in ``person_faces`` is stored
    ready-to-compare and the matcher never has to re-normalise enrolled data.
    """
    npmod = _require_numpy()
    vec = normalize_embedding(vector)
    return npmod.ascontiguousarray(vec, dtype='<f4').tobytes()


def embedding_from_bytes(data: bytes, dim: int) -> Any:
    """Rebuild a ``dim``-length float32 embedding from stored bytes.

    Raises ``ValueError`` if the byte length does not match ``dim`` float32s, so
    a truncated/corrupt BLOB fails loudly instead of yielding a mis-shaped
    vector that would silently corrupt the match matrix.
    """
    npmod = _require_numpy()
    vec = npmod.frombuffer(data, dtype='<f4')
    if vec.shape[0] != int(dim):
        raise ValueError(
            f"embedding byte length {vec.shape[0]} does not match declared dim {dim}"
        )
    return npmod.array(vec, dtype=npmod.float32)


class MatchResult:
    """One recognition outcome: the winning person and its cosine score."""

    __slots__ = ('person_id', 'name', 'score', 'face_id')

    def __init__(self, person_id: int, name: str, score: float, face_id: int) -> None:
        self.person_id = person_id
        self.name = name
        self.score = score
        self.face_id = face_id

    def to_dict(self) -> dict[str, Any]:
        return {
            'person_id': self.person_id,
            'name': self.name,
            'score': round(self.score, 4),
            'face_id': self.face_id,
        }


class FaceMatcher:
    """Nearest-neighbour identity matcher over enrolled face embeddings.

    Built from the rows returned by ``PersonsMixin.load_face_embeddings`` (each
    carrying ``person_id``, ``person_name``, ``embedding`` bytes and ``dim``).
    All enrolled vectors are stacked into one normalised matrix so a query is
    matched with a single matrix-vector product; the highest-scoring enrolled
    face wins its owner the match when the cosine score clears the threshold.

    Rows whose ``dim`` disagrees with the matrix are skipped defensively -- the
    caller already filters by model (and model implies a fixed dim), so a
    mismatch means mixed/legacy data that must not corrupt the comparison.
    """

    def __init__(self, rows: list[dict[str, Any]]):
        npmod = _require_numpy()
        vectors: list[Any] = []
        self.person_ids: list[int] = []
        self.names: list[str] = []
        self.face_ids: list[int] = []
        expected_dim: int | None = None
        for row in rows:
            dim = int(row['dim'])
            if expected_dim is None:
                expected_dim = dim
            if dim != expected_dim:
                logger.warning(
                    'FaceMatcher skipping enrolled face %s: dim %s != matrix dim %s',
                    row.get('face_id'), dim, expected_dim,
                )
                continue
            try:
                vec = embedding_from_bytes(row['embedding'], dim)
            except ValueError as exc:
                logger.warning('FaceMatcher skipping corrupt embedding %s: %s', row.get('face_id'), exc)
                continue
            vectors.append(normalize_embedding(vec))
            self.person_ids.append(int(row['person_id']))
            self.names.append(str(row['person_name']))
            self.face_ids.append(int(row.get('face_id', -1)))
        self.dim = expected_dim or 0
        if vectors:
            self.matrix = npmod.vstack(vectors).astype(npmod.float32)
        else:
            self.matrix = npmod.zeros((0, self.dim), dtype=npmod.float32)

    def __len__(self) -> int:
        return int(self.matrix.shape[0])

    @property
    def is_empty(self) -> bool:
        return self.matrix.shape[0] == 0

    def match(self, query: Any, *, threshold: float = _DEFAULT_MATCH_THRESHOLD) -> MatchResult | None:
        """Return the best matching person for ``query``, or ``None`` (unknown).

        ``query`` is any embedding vector (it is normalised here, so callers may
        pass a raw model output). Returns ``None`` when the store is empty, when
        the query dimensionality does not match the enrolled vectors, or when
        the best cosine score is below ``threshold``.
        """
        npmod = _require_numpy()
        if self.is_empty:
            return None
        vec = normalize_embedding(query)
        if vec.shape[0] != self.matrix.shape[1]:
            return None
        scores = self.matrix @ vec
        best_index = int(npmod.argmax(scores))
        best_score = float(scores[best_index])
        if best_score < threshold:
            return None
        return MatchResult(
            person_id=self.person_ids[best_index],
            name=self.names[best_index],
            score=best_score,
            face_id=self.face_ids[best_index],
        )


class FaceEmbedder:
    """ArcFace-style ONNX face embedding model wrapped for the app.

    Mirrors the availability/lazy-import conventions of ``OnnxYoloDetector``:
    construction never raises on a missing model or missing runtime -- it sets
    :attr:`unavailable_reason` and reports :attr:`available` ``False`` so the
    rest of the app can degrade gracefully. Given an aligned face crop (BGR, as
    produced by OpenCV / the detector), :meth:`embed` returns a single
    L2-normalised embedding ready to store or match.
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        input_size: int = _DEFAULT_INPUT_SIZE,
        device: str = 'auto',
        model_id: str = 'arcface',
        max_concurrency: int = 1,
    ) -> None:
        self.model_path = _resolve_project_path(model_path)
        self.model_id = model_id
        try:
            self.input_size = int(input_size)
        except (TypeError, ValueError):
            self.input_size = _DEFAULT_INPUT_SIZE
        if not 16 <= self.input_size <= 1024:
            self.input_size = _DEFAULT_INPUT_SIZE
        self._device = (device or 'auto').lower()
        self.session: Any | None = None
        self.input_name: str | None = None
        self.output_names: list[str] = []
        self.unavailable_reason: str | None = None
        self._embedding_dim: int | None = None
        self._inference_semaphore = threading.Semaphore(max(1, int(max_concurrency)))

        if np is None:
            self.unavailable_reason = "numpy is not installed. Install requirements.txt or run pip install numpy."
            return
        if not self.model_path.exists():
            self.unavailable_reason = f"Face embedding model not found: {self.model_path}"
            return
        try:
            import onnxruntime as ort
        except ImportError:
            self.unavailable_reason = "onnxruntime is not installed. Install requirements.txt or run pip install onnxruntime."
            return
        try:
            providers = self._resolve_providers(ort)
            self.session = ort.InferenceSession(str(self.model_path), providers=providers)
            self.input_name = self.session.get_inputs()[0].name
            self.output_names = [output.name for output in self.session.get_outputs()]
            output_shape = self.session.get_outputs()[0].shape
            # Trailing dim is the embedding width when the model declares it
            # statically (ArcFace exports usually do: [1, 512]).
            if output_shape and isinstance(output_shape[-1], int):
                self._embedding_dim = int(output_shape[-1])
        except Exception as exc:  # pragma: no cover - onnxruntime load failure path
            # Log the underlying error for operators but keep the surfaced
            # reason generic: it flows to the settings API response, and raw
            # exception text can leak internal paths / stack details.
            logger.warning('Failed to load face embedding model %s: %s', self.model_path, exc)
            self.unavailable_reason = "Failed to load the face embedding model."
            self.session = None

    @property
    def available(self) -> bool:
        return self.session is not None and self.unavailable_reason is None

    @property
    def embedding_dim(self) -> int | None:
        return self._embedding_dim

    def _resolve_providers(self, ort: Any) -> list[str]:
        available = set(ort.get_available_providers())
        if self._device in ('auto', 'cuda') and 'CUDAExecutionProvider' in available:
            return ['CUDAExecutionProvider', 'CPUExecutionProvider']
        return ['CPUExecutionProvider']

    def preprocess(self, face_bgr: Any) -> Any:
        """Turn an OpenCV BGR face crop into the model's input tensor.

        Resizes to ``input_size`` square, converts BGR->RGB, scales pixels to
        [-1, 1] the ArcFace way, and returns an ``[1, 3, H, W]`` float32 tensor.
        """
        npmod = _require_numpy()
        import cv2

        if face_bgr is None or getattr(face_bgr, 'size', 0) == 0:
            raise ValueError("face crop is empty")
        resized = cv2.resize(face_bgr, (self.input_size, self.input_size), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        chw = npmod.transpose(rgb, (2, 0, 1)).astype(npmod.float32)
        chw = (chw - _PIXEL_MEAN) / _PIXEL_SCALE
        return npmod.ascontiguousarray(chw[None, ...], dtype=npmod.float32)

    def embed(self, face_bgr: Any) -> Any:
        """Return the L2-normalised embedding for a single BGR face crop."""
        if not self.available:
            raise FaceEmbedderUnavailableError(
                self.unavailable_reason or "Face embedding model is not available"
            )
        tensor = self.preprocess(face_bgr)
        with self._inference_semaphore:
            outputs = self.session.run(self.output_names, {self.input_name: tensor})  # type: ignore[union-attr]
        vector = normalize_embedding(outputs[0])
        if self._embedding_dim is None:
            self._embedding_dim = int(vector.shape[0])
        return vector
