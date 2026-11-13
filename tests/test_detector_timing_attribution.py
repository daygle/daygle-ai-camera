"""Per-thread attribution of the detector's preprocess/session/postprocess split.

One ``OnnxYoloDetector`` is shared by the live inference scheduler's workers, and
each cycle folds ``detector.last_timing`` into its own pipeline timing right
after its ``detect_frame`` returns. A plain attribute let an inference that
completed on another camera's thread land in that window, so the cycle reported
another camera's breakdown.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

np = pytest.importorskip('numpy')

from app.detector import OnnxYoloDetector  # noqa: E402


def _bare_detector() -> OnnxYoloDetector:
    """A detector carrying only the timing plumbing (no ONNX session)."""
    detector = object.__new__(OnnxYoloDetector)
    detector._timing_local = threading.local()
    detector._last_timing = {}
    return detector


def _runnable_detector() -> OnnxYoloDetector:
    """A detector whose inference path runs without ONNX Runtime."""
    detector = _bare_detector()
    detector.confidence = 0.25
    detector._use_io_binding = False
    detector._input_dtype = None
    detector._inference_semaphore = threading.Semaphore(2)
    detector.session = _FakeSession()
    detector.input_name = 'images'
    detector.output_names = ['output0']
    detector.labels = ['person']
    detector.iou_threshold = 0.45
    detector._nms_free = False
    detector._head_mismatch_logged = False
    detector._confidence_only_nms = True
    detector._preprocess = lambda image: (
        np.zeros((1, 3, 2, 2), dtype=np.float32), 1.0, 0.0, 0.0, 2, 2,
    )
    detector._postprocess = lambda *args, **kwargs: []
    return detector


class _FakeSession:
    def run(self, output_names, feed):
        return [np.zeros((1, 6), dtype=np.float32)]


def test_last_timing_is_empty_before_any_inference():
    assert _bare_detector().last_timing == {}


def test_detect_frame_publishes_its_breakdown():
    detector = _runnable_detector()

    assert detector.detect_frame(np.zeros((8, 8, 3), dtype=np.uint8)) == []
    assert set(detector.last_timing) == {'preprocess_ms', 'inference_ms', 'postprocess_ms'}


def test_last_timing_stays_with_the_inference_thread():
    detector = _bare_detector()
    detector._publish_timing({'inference_ms': 30.0})  # this thread's cycle
    seen = {}

    def other_camera():
        seen['before'] = detector.last_timing
        detector._publish_timing({'inference_ms': 5.0})
        seen['after'] = detector.last_timing

    worker = threading.Thread(target=other_camera)
    worker.start()
    worker.join()

    # The other inference finished while this thread still held its own sample,
    # so this thread's cycle must still report its own cost.
    assert detector.last_timing == {'inference_ms': 30.0}
    assert seen['before'] == {'inference_ms': 30.0}  # last completed anywhere
    assert seen['after'] == {'inference_ms': 5.0}


def test_reader_without_its_own_sample_sees_the_latest():
    detector = _bare_detector()
    detector._publish_timing({'inference_ms': 12.0})
    seen = {}

    thread = threading.Thread(target=lambda: seen.update(sample=detector.last_timing))
    thread.start()
    thread.join()

    assert seen['sample'] == {'inference_ms': 12.0}
