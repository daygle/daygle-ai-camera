"""Unit tests for the ONNX YOLO detector post-processing paths.

These cover the pure-numpy coordinate transforms and format dispatch that
turn raw model output into normalized detection boxes, for both the
traditional grid/NMS head (YOLOv8 / YOLO11) and the end-to-end NMS-free
head (YOLO26). They do not require ``onnxruntime`` or a model file: the
detector is constructed against a non-existent path (so it marks itself
unavailable) and the post-process helpers are exercised directly.
"""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from app.detector import (
    OnnxYoloDetector,
    _int8_precision_supported_for_detector,
    _resolve_confidence_only_nms,
)

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
    # 2-class grid exports: the 6-feature dim is a MIN class count, not the
    # NMS-free row. At tiny inputs the anchor count can dip below 1000
    # (e.g. 567 at 96px), which must not be mistaken for an end-to-end head.
    ((1, 6, 567), False),
    ((6, 567), False),
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


def test_nms_free_fp16_output_cast_before_nms():
    """FP16 model output must flow through the class-aware NMS dedupe
    without subnormal/NaN IoU: the NMS-free path casts boxes and scores to
    float32 before ``non_max_suppression``, mirroring the grid head."""
    det = _detector(nms_free=True)
    out = _nms_free_overlapping_output().astype(np.float16)
    res = det._postprocess_nms_free(out[0], SCALE, PAD_X, PAD_Y, OW, OH, 0.45)
    assert len(res) == 1  # overlapping pair collapses, as in float32
    assert res[0]["confidence"] == pytest.approx(0.9, abs=1e-3)


# -- INT8 precision support --------------------------------------------------

@pytest.mark.parametrize('nms_free,expected', [
    (False, True),
    (True, False),
])
def test_int8_precision_support_excludes_nms_free_heads(nms_free, expected):
    """The current static QDQ PTQ path must not run on YOLO26-style heads:
    they can load successfully while returning confidence scores too damaged
    to produce object detections. Traditional grid heads retain INT8 support.
    """
    assert _int8_precision_supported_for_detector(nms_free) is expected


def test_nms_free_int8_request_loads_fp32_without_quantizing(monkeypatch, tmp_path):
    """An INT8 request for a YOLO26-style detector must bypass PTQ entirely.

    This exercises the constructor path that previously published a healthy
    QDQ session while returning no real detections. The fallback is observable
    through both the source model path and ``active_precision``.
    """
    model_path = tmp_path / "yolo26l-768.onnx"
    model_path.write_bytes(b"fake model")
    quantize_calls = []

    def _unexpected_quantize(path):
        quantize_calls.append(path)
        raise AssertionError("NMS-free models must not be INT8-quantized")

    import app.quantization as quantization
    monkeypatch.setattr(quantization, "quantize_int8", _unexpected_quantize)

    class _Input:
        name = "images"
        shape = [1, 3, 64, 64]
        type = "tensor(float)"

    class _Output:
        name = "output"

    class _SessionOptions:
        graph_optimization_level = None
        intra_op_num_threads = 0
        inter_op_num_threads = 0
        execution_mode = None

    class _Session:
        last_path = None

        def __init__(self, path, *, sess_options, providers):
            self.last_path = path
            self._providers = ["CPUExecutionProvider"]

        def get_inputs(self):
            return [_Input()]

        def get_outputs(self):
            return [_Output()]

        def get_providers(self):
            return self._providers

        def run(self, output_names, feeds):
            return [np.array([[[8, 8, 56, 56, 0.9, 0]]], dtype=np.float32)]

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.get_available_providers = lambda: ["CPUExecutionProvider"]
    fake_ort.SessionOptions = _SessionOptions
    fake_ort.GraphOptimizationLevel = types.SimpleNamespace(ORT_ENABLE_ALL=1)
    fake_ort.ExecutionMode = types.SimpleNamespace(ORT_PARALLEL=1, ORT_SEQUENTIAL=2)
    fake_ort.InferenceSession = _Session
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    detector = OnnxYoloDetector(
        model_path=model_path,
        categories=LABELS,
        # Omit nms_free deliberately: direct callers must still get the
        # filename-based safety guard used by create_detector().
        precision="int8",
        input_size=64,
    )

    assert quantize_calls == []
    assert detector.available
    assert detector.active_precision == "fp32"
    assert detector.session.last_path == str(model_path)
    detections = detector.detect_frame(np.zeros((64, 64, 3), dtype=np.uint8))
    assert detections and detections[0]["label"] == "person"
    assert detections[0]["confidence"] == pytest.approx(0.9, abs=1e-3)


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


def test_configured_input_size_controls_dynamic_preprocess_geometry():
    """The AI ``input_size`` setting must reach the detector instead of being
    accepted and then silently ignored at the default 640-pixel canvas."""
    pytest.importorskip("cv2")
    from app.detector import create_detector

    det = create_detector({
        "backend": "onnx",
        "model_path": "/does-not-exist.onnx",
        "labels_path": "models/coco.names",
        "input_size": 768,
    })
    assert det.input_width == 768
    assert det.input_height == 768
    frame = np.zeros((OH, OW, 3), dtype=np.uint8)
    tensor, *_ = det._preprocess(frame)
    assert tensor.shape == (1, 3, 768, 768)


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


# -- CUDA io_binding inference path ----------------------------------------

def test_run_inference_io_bound_binds_outputs_by_device(monkeypatch):
    """Regression: the CUDA io_binding path must allocate outputs with
    ``bind_output(name, device_type, device_id)``. The earlier code called
    ``bind_ortvalue_output(name, 'cuda', 0)`` -- that method takes a
    pre-allocated OrtValue, not a device, and raised at runtime with
    'IOBinding.bind_ortvalue_output() takes 3 positional arguments but 4
    were given'. The fake binding below mirrors the real ORT signatures so
    a reintroduced 4-arg call would fail here."""
    calls = {'bind_output': [], 'bind_input': 0, 'ran': False}
    out_arr = np.zeros((1, 84, 8400), dtype=np.float32)

    class _FakeOrtValue:
        def __init__(self, arr):
            self._arr = arr

        @staticmethod
        def ortvalue_from_numpy(arr, device_type, device_id):
            return _FakeOrtValue(arr)

        def numpy(self):
            return self._arr

    class _FakeIOBinding:
        def bind_ortvalue_input(self, name, ortvalue):  # (self, name, ortvalue)
            calls['bind_input'] += 1

        def bind_output(self, name, device_type='cpu', device_id=0):
            calls['bind_output'].append((name, device_type, device_id))

        def bind_ortvalue_output(self, name, ortvalue):  # real ORT signature
            raise AssertionError(
                'device-allocated outputs must use bind_output, not '
                'bind_ortvalue_output'
            )

        def get_outputs(self):
            return [_FakeOrtValue(out_arr)]

    fake_io = _FakeIOBinding()

    class _FakeSession:
        def io_binding(self):
            return fake_io

        def run_with_iobinding(self, io):
            calls['ran'] = True

    fake_ort = types.ModuleType('onnxruntime')
    fake_ort.OrtValue = _FakeOrtValue

    det = _detector(nms_free=False)
    det.session = _FakeSession()
    det.input_name = 'images'
    det.output_names = ['output0', 'output1']

    # ``_run_inference_io_bound`` does ``from onnxruntime import OrtValue`` at
    # call time, so swapping the module in sys.modules is enough.
    monkeypatch.setitem(sys.modules, 'onnxruntime', fake_ort)
    result = det._run_inference_io_bound(np.zeros((1, 3, 640, 640), dtype=np.float32))

    assert calls['ran'] is True
    assert calls['bind_input'] == 1
    assert calls['bind_output'] == [('output0', 'cuda', 0), ('output1', 'cuda', 0)]
    assert [r.shape for r in result] == [(1, 84, 8400)]
