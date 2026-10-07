"""The face embedder feeds the model the pixel scaling it expects.

The ONNX Model Zoo ArcFace models (mxnet-converted, fp32 and INT8) normalise
pixels inside the graph. Normalising them again before inference squeezed
every face into a near-uniform image, so every face embedded alike and a
stranger "matched" the one enrolled person at ~0.8 -- nothing ever reached
Review. The embedder now measures which scaling the model wants, and stored
embeddings made the old way are redone from their pictures.
"""
from __future__ import annotations

import numpy as np

from app.face_recognition import (
    INPUT_SCALING_NORMALIZED,
    INPUT_SCALING_RAW,
    FaceEmbedder,
    choose_input_scaling,
    embedding_from_bytes,
    embedding_to_bytes,
    encode_face_thumbnail,
)

SIZE = 32


class _ProjectionSession:
    """A stand-in model: a fixed random projection of the input pixels.

    ``normalises_inside`` mimics the mxnet-converted ArcFace graph, which does
    ``(x - 127.5) * 0.0078125`` itself before the network.
    """

    def __init__(self, normalises_inside: bool) -> None:
        self.normalises_inside = normalises_inside
        self.weights = np.random.default_rng(7).standard_normal((64, 3 * SIZE * SIZE)).astype(np.float32)

    def run(self, _output_names, feeds):
        pixels = next(iter(feeds.values())).reshape(-1)
        if self.normalises_inside:
            pixels = (pixels - 127.5) * 0.0078125
        return [(self.weights @ pixels)[None, :]]


def _embedder(tmp_path, normalises_inside: bool) -> FaceEmbedder:
    embedder = FaceEmbedder(tmp_path / 'missing.onnx', input_size=SIZE)
    embedder.session = _ProjectionSession(normalises_inside)
    embedder.unavailable_reason = None
    embedder.input_name = 'data'
    embedder.output_names = ['fc1']
    return embedder


def _face(seed: int) -> np.ndarray:
    """A smooth, distinct test image (survives the JPEG thumbnail)."""
    import cv2

    coarse = np.random.default_rng(seed).integers(0, 256, (4, 4, 3)).astype(np.uint8)
    return cv2.resize(coarse, (SIZE, SIZE), interpolation=cv2.INTER_LINEAR)


def _similarity(embedder: FaceEmbedder, first, second) -> float:
    return float(embedder.embed(first) @ embedder.embed(second))


def test_a_model_that_normalises_itself_gets_raw_pixels(tmp_path):
    embedder = _embedder(tmp_path, normalises_inside=True)
    assert embedder.input_scaling == INPUT_SCALING_NORMALIZED
    assert _similarity(embedder, _face(1), _face(2)) > 0.9, 'normalised twice, two different faces look alike'
    assert embedder.calibrate_input_scaling() == INPUT_SCALING_RAW
    assert embedder.preprocess(_face(1)).max() > 1.0
    assert _similarity(embedder, _face(1), _face(2)) < 0.5, 'raw pixels tell the faces apart'


def test_a_standard_arcface_model_keeps_the_minus_one_to_one_input(tmp_path):
    embedder = _embedder(tmp_path, normalises_inside=False)
    assert embedder.calibrate_input_scaling() == INPUT_SCALING_NORMALIZED
    assert float(embedder.preprocess(_face(1)).max()) <= 1.0


def _with_similarity(similarity: float, count: int = 4) -> list:
    """Unit vectors whose every pair has cosine ``similarity``."""
    basis = np.eye(count + 1, dtype=np.float32)
    return [np.sqrt(similarity) * basis[0] + np.sqrt(1 - similarity) * basis[i + 1] for i in range(count)]


def test_the_catalog_models_measured_gaps_switch_to_raw():
    # Measured on arcfaceresnet100-8 (fp32) and -11-int8: the test images
    # were 89% / 81% alike normalised twice, 29% alike as raw pixels.
    for normalized in (0.886, 0.809):
        scaling, measured = choose_input_scaling({'normalized': _with_similarity(normalized), 'raw': _with_similarity(0.293)})
        assert scaling == INPUT_SCALING_RAW
        assert abs(measured['normalized'] - normalized) < 1e-4
    # And the mirror image, a model that wants [-1, 1], stays put.
    assert choose_input_scaling({'normalized': _with_similarity(0.291), 'raw': _with_similarity(0.768)})[0] == INPUT_SCALING_NORMALIZED
    # A small difference is not evidence either way.
    assert choose_input_scaling({'normalized': _with_similarity(0.5), 'raw': _with_similarity(0.4)})[0] == INPUT_SCALING_NORMALIZED


def test_choice_needs_a_clear_improvement():
    same = [np.ones(4, dtype=np.float32) / 2] * 3
    spread = [np.eye(4, dtype=np.float32)[i] for i in range(3)]
    assert choose_input_scaling({'normalized': same, 'raw': spread})[0] == INPUT_SCALING_RAW
    assert choose_input_scaling({'normalized': spread, 'raw': same})[0] == INPUT_SCALING_NORMALIZED
    # A model that collapses either way gives no evidence: keep the default.
    assert choose_input_scaling({'normalized': same, 'raw': same})[0] == INPUT_SCALING_NORMALIZED


def test_a_failing_probe_keeps_the_default(tmp_path):
    embedder = _embedder(tmp_path, normalises_inside=True)

    class _Broken:
        def run(self, *_args):
            raise RuntimeError('boom')

    embedder.session = _Broken()
    assert embedder.calibrate_input_scaling() == INPUT_SCALING_NORMALIZED


def test_stored_faces_are_redone_when_the_scaling_changes(tmp_path, monkeypatch):
    from app.database import EventDatabase
    import app.face_recognition_service as frs

    db = EventDatabase(str(tmp_path / 'faces.sqlite3'))
    old = _embedder(tmp_path, normalises_inside=True)  # still normalising twice
    glen_photo, stranger = _face(1), _face(2)

    def stored(image):
        return {'embedding': embedding_to_bytes(old.embed(image)), 'dim': 64, 'model': 'arcface-r100'}

    glen = db.add_person('Glen')
    photo_id = db.add_person_face(glen, thumbnail=encode_face_thumbnail(glen_photo), **stored(glen_photo))
    db.add_person_face(glen, source_snapshot='auto-enrich:cam=c,track=4', thumbnail=encode_face_thumbnail(stranger), **stored(stranger))
    blank_id = db.add_person_face(glen, **stored(glen_photo))  # an old row with no picture
    capture = db.store_unknown_face(camera_id='c', thumbnail=encode_face_thumbnail(stranger), **stored(stranger))

    fixed = _embedder(tmp_path, normalises_inside=True)
    fixed.calibrate_input_scaling()
    monkeypatch.setattr(frs, 'FaceEmbedder', lambda *_a, **_k: fixed)
    config = {'enabled': True, 'model_id': 'arcface-r100', 'model_path': 'x.onnx', 'match_threshold': 0.5}
    service = frs.FaceRecognitionService(config, db)

    sources = sorted(face['source'] for face in db.list_person_faces(glen))
    assert sources == ['enrolled', 'enrolled'], 'the auto-learned face is dropped'
    rows = {row['face_id']: row for row in db.load_face_embeddings('arcface-r100')}
    redone = embedding_from_bytes(rows[photo_id]['embedding'], rows[photo_id]['dim'])
    assert float(redone @ fixed.embed(glen_photo)) > 0.99, 'the photo was re-embedded from its picture'
    assert rows[blank_id]['embedding'] == stored(glen_photo)['embedding'], 'nothing to redo it from'
    assert service.recognize(glen_photo).person_id == glen
    assert service.recognize(stranger) is None, 'a stranger no longer matches Glen, so they go to Review'
    assert db.get_setting(frs.INPUT_SCALING_SETTING) == {'arcface-r100': INPUT_SCALING_RAW}
    capture_row = db.stored_face_embeddings_for_refresh('arcface-r100')
    assert any(row['table'] == 'unknown_faces' and row['id'] == capture for row in capture_row)

    # A restart with the same scaling leaves everything alone.
    calls = []
    monkeypatch.setattr(db, 'stored_face_embeddings_for_refresh', lambda model: calls.append(model) or [])
    frs.FaceRecognitionService(config, db)
    assert calls == []
