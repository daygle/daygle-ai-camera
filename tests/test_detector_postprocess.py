"""Unit tests for the ONNX YOLO detector post-processing paths.

These cover the pure-numpy coordinate transforms and format dispatch that
turn raw model output into normalized detection boxes, for both the
traditional grid/NMS head (YOLOv8 / YOLO11) and the end-to-end NMS-free
head (YOLO26). They do not require ``onnxruntime`` or a model file: the
detector is constructed against a non-existent path (so it marks itself
unavailable) and the post-process helpers are exercised directly.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.detector import OnnxYoloDetector, _resolve_confidence_only_nms

# Frame + letterbox geometry shared by the tests: a 1280x720 frame scaled
# into a 640x640 square input. scale = min(640/1280, 640/720) = 0.5;
# horizontal padding is 0, vertical padding is (640 - 360) / 2 = 140.
OW, OH = 1280, 720
SCALE = min(640 / OW, 640 / OH)
PAD_X = (640 - OW * SCALE) / 2
PAD_Y = (640 - OH * SCALE) / 2

LABELS = [f"c{i}" for i in range(80)]
LABELS[0] = "person"


def _detector(nms_free: bool) -> OnnxYoloDetector:
    det = OnnxYoloDetector(
        model_path="/does-not-exist.onnx",
        categories=LABELS,
        confidence=0.45,
        iou_threshold=0.45,
        nms_free=nms_free,
    )
    det.input_width = 640
    det.input_height = 640
    return det


def _grid_output(cx, cy, w, h, cls, score, extra=None):
    """Build a YOLOv8-style ``[1, 84, 8400]`` output with one strong box."""
    out = np.zeros((1, 84, 8400), dtype=np.float32)
    out[0, 0, 0], out[0, 1, 0], out[0, 2, 0], out[0, 3, 0] = cx, cy, w, h
    out[0, 4 + cls, 0] = score
    if extra is not None:
        ecx, ecy, ew, eh, ecls, escore = extra
        out[0, 0, 1], out[0, 1, 1], out[0, 2, 1], out[0, 3, 1] = ecx, ecy, ew, eh
        out[0, 4 + ecls, 1] = escore
    return out


def test_grid_nms_coordinate_transform():
    det = _detector(nms_free=False)
    # Box centred at input (320, 320), 100x100 px -> input xyxy (270,270,370,370)
    out = _grid_output(320, 320, 100, 100, cls=0, score=0.9)
    res = det._postprocess_nms(out[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    assert len(res) == 1
    box = res[0]["box"]
    assert res[0]["label"] == "person"
    # x1 = (270 - pad_x)/scale = 540; y1 = (270 - 140)/scale = 260
    assert box["x"] == pytest.approx(540 / OW, abs=0.01)
    assert box["y"] == pytest.approx(260 / OH, abs=0.01)
    assert box["width"] == pytest.approx(200 / OW, abs=0.01)
    assert box["height"] == pytest.approx(200 / OH, abs=0.01)


def test_grid_nms_deduplicates_overlapping_boxes():
    det = _detector(nms_free=False)
    out = _grid_output(320, 320, 100, 100, cls=0, score=0.9,
                       extra=(325, 325, 100, 100, 0, 0.8))
    res = det._postprocess_nms(out[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    # Two heavily overlapping same-class boxes collapse to one after NMS.
    assert len(res) == 1
    assert res[0]["confidence"] == pytest.approx(0.9, abs=1e-3)


def test_grid_nms_confidence_filter():
    det = _detector(nms_free=False)
    out = _grid_output(320, 320, 100, 100, cls=0, score=0.2)
    res = det._postprocess_nms(out[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    assert res == []


def _nms_free_output(rows):
    out = np.zeros((1, 300, 6), dtype=np.float32)
    for i, row in enumerate(rows):
        out[0, i] = row
    return out


def test_nms_free_coordinate_transform():
    det = _detector(nms_free=True)
    # xyxy in input space (270,180,370,280), conf 0.9, class 0
    out = _nms_free_output([[270, 180, 370, 280, 0.9, 0],
                            [0, 0, 10, 10, 0.01, 0]])  # low-conf filler
    res = det._postprocess_nms_free(out[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    assert len(res) == 1
    box = res[0]["box"]
    assert res[0]["label"] == "person"
    assert box["x"] == pytest.approx(540 / OW, abs=0.01)
    assert box["y"] == pytest.approx(80 / OH, abs=0.01)   # (180-140)/0.5 = 80
    assert box["width"] == pytest.approx(200 / OW, abs=0.01)
    assert box["height"] == pytest.approx(200 / OH, abs=0.01)


def test_nms_free_single_detection_does_not_crash():
    """A single surviving detection squeezes to 1-D; must not raise."""
    det = _detector(nms_free=True)
    out = np.zeros((1, 1, 6), dtype=np.float32)
    out[0, 0] = [270, 180, 370, 280, 0.9, 0]
    res = det._postprocess_nms_free(out[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    assert len(res) == 1
    assert res[0]["label"] == "person"


@pytest.mark.parametrize("shape,expected", [
    ((1, 300, 6), True),      # end-to-end / NMS-free
    ((300, 6), True),
    ((1, 6), True),           # single end-to-end detection
    ((6,), True),
    ((1, 84, 8400), False),   # YOLOv8/11 COCO-80 grid head
    ((84, 8400), False),
    ((1, 84, 2100), False),   # grid head at 320px input
    ((2, 300, 6), None),      # >2 real dims: can't classify, fall back to flag
])
def test_looks_nms_free_shape_dispatch(shape, expected):
    assert OnnxYoloDetector._looks_nms_free(np.zeros(shape, dtype=np.float32)) is expected


def test_dispatch_prefers_shape_over_flag():
    """A grid model mislabelled nms_free=True is still decoded correctly."""
    det = _detector(nms_free=True)  # WRONG flag on purpose
    out = _grid_output(320, 320, 100, 100, cls=0, score=0.9)
    res = det._postprocess(out[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    assert len(res) == 1
    assert res[0]["label"] == "person"
    assert res[0]["box"]["x"] == pytest.approx(540 / OW, abs=0.01)


def test_dispatch_handles_nms_free_when_flag_false():
    """An end-to-end model mislabelled nms_free=False is still decoded."""
    det = _detector(nms_free=False)  # WRONG flag on purpose
    out = _nms_free_output([[270, 180, 370, 280, 0.9, 0]])
    res = det._postprocess(out[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    assert len(res) == 1
    assert res[0]["label"] == "person"
    assert res[0]["box"]["y"] == pytest.approx(80 / OH, abs=0.01)


# -- confidence_only_nms tri-state resolution ------------------------------

@pytest.mark.parametrize("value,nms_free,expected", [
    (None, True, True),        # auto + NMS-free model -> skip NMS
    (None, False, False),      # auto + grid model    -> run NMS
    ("auto", True, True),
    ("auto", False, False),
    ("on", False, True),       # explicit on overrides regardless of model
    ("off", True, False),      # explicit off overrides regardless of model
    (True, False, True),       # legacy persisted bool
    (False, True, False),      # legacy persisted bool
    ("garbage", True, True),   # unknown -> follow the model (auto)
])
def test_resolve_confidence_only_nms(value, nms_free, expected):
    assert _resolve_confidence_only_nms(value, nms_free) is expected


# -- confidence_only_nms actually gates the NMS-free dedupe ----------------

def _nms_free_overlapping_output():
    """Two heavily-overlapping same-class boxes (IoU ~0.68)."""
    out = np.zeros((1, 300, 6), dtype=np.float32)
    out[0, 0] = [270, 180, 370, 280, 0.9, 0]
    out[0, 1] = [280, 190, 380, 290, 0.8, 0]
    return out


def test_nms_free_runs_dedupe_when_disabled():
    """confidence_only_nms=False -> the class-aware NMS collapses the pair."""
    det = OnnxYoloDetector(model_path="/does-not-exist.onnx", categories=LABELS,
                           iou_threshold=0.45, nms_free=True, confidence_only_nms=False)
    det.input_width = det.input_height = 640
    res = det._postprocess_nms_free(_nms_free_overlapping_output()[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    assert len(res) == 1


def test_nms_free_skips_dedupe_when_enabled():
    """confidence_only_nms=True -> both overlapping detections are kept
    (the modern NMS-free path trusts the head's assignment)."""
    det = OnnxYoloDetector(model_path="/does-not-exist.onnx", categories=LABELS,
                           iou_threshold=0.45, nms_free=True, confidence_only_nms=True)
    det.input_width = det.input_height = 640
    res = det._postprocess_nms_free(_nms_free_overlapping_output()[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    assert len(res) == 2


# -- input dtype default ---------------------------------------------------

def test_preprocess_defaults_to_float32_when_dtype_unresolved():
    """With no loaded session the input dtype is unresolved (None); the
    preprocess step must still produce a float32 tensor."""
    pytest.importorskip("cv2")  # _preprocess needs opencv; skip on minimal installs
    det = _detector(nms_free=False)  # unavailable (no model) -> _input_dtype None
    assert det._input_dtype is None
    frame = np.zeros((OH, OW, 3), dtype=np.uint8)
    tensor, *_ = det._preprocess(frame)
    assert tensor.dtype == np.float32


# -- active_precision reflects the running precision after fallback ---------

def test_active_precision_reports_int8_when_quantized():
    det = _detector(nms_free=False)
    det._precision = 'int8'  # __init__ only keeps 'int8' when quantization worked
    assert det.active_precision == 'int8'


def test_active_precision_reports_fp16_from_model_input_dtype():
    det = _detector(nms_free=False)
    det._precision = 'fp16'
    det._input_dtype = np.float16
    assert det.active_precision == 'fp16'


def test_active_precision_fp16_request_but_fp32_model_reads_fp32():
    """FP16 requested but the loaded model has a float32 input (a CPU export
    that dropped half=True) reports fp32 -- the truth, not the request."""
    det = _detector(nms_free=False)
    det._precision = 'fp16'
    det._input_dtype = np.float32
    assert det.active_precision == 'fp32'


def test_active_precision_defaults_fp32():
    det = _detector(nms_free=False)
    assert det.active_precision == 'fp32'
