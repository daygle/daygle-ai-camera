from __future__ import annotations

import numpy as np

from app.detector import OnnxYoloDetector


def _detector(*, nms_free: bool) -> OnnxYoloDetector:
    detector = OnnxYoloDetector.__new__(OnnxYoloDetector)
    detector.labels = ['person']
    detector._nms_free = nms_free
    detector._confidence_only_nms = True
    detector.iou_threshold = 0.45
    detector.confidence = 0.1
    return detector


def test_nms_free_output_discards_non_finite_rows():
    detector = _detector(nms_free=True)
    output = np.array([
        [10.0, 10.0, 40.0, 40.0, 0.9, 0.0],
        [np.nan, 10.0, 40.0, 40.0, 0.9, 0.0],
    ], dtype=np.float32)[None, ...]

    detections = detector._postprocess_nms_free(output, 1.0, 0.0, 0.0, 100, 100, 0.1)

    assert len(detections) == 1
    assert np.isfinite(detections[0]['confidence'])
    assert all(np.isfinite(value) for value in detections[0]['box'].values())


def test_grid_output_accepts_single_candidate_export():
    detector = _detector(nms_free=False)
    output = np.array([[[50.0], [50.0], [20.0], [20.0], [0.9]]], dtype=np.float32)

    detections = detector._postprocess_nms(output, 1.0, 0.0, 0.0, 100, 100, 0.1)

    assert len(detections) == 1
    assert detections[0]['label'] == 'person'


def test_grid_output_discards_non_finite_rows():
    detector = _detector(nms_free=False)
    # [x, y, w, h, person_score] in the traditional transposed layout.
    output = np.array([
        [50.0, np.inf],
        [50.0, 50.0],
        [20.0, 20.0],
        [20.0, 20.0],
        [0.9, 0.9],
    ], dtype=np.float32)[None, ...]

    detections = detector._postprocess_nms(output, 1.0, 0.0, 0.0, 100, 100, 0.1)

    assert len(detections) == 1
    assert np.isfinite(detections[0]['confidence'])
    assert all(np.isfinite(value) for value in detections[0]['box'].values())
