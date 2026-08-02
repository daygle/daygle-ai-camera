"""Integration test: real INT8 QDQ quantization must load on the installed ORT.

``test_quantization.py`` mocks ``onnxruntime.quantization``; this file runs
the genuine pipeline (``quantize_static`` + the synthetic calibration reader)
against a tiny Conv model and proves the cached artifact:

- contains NO legacy ``ConvInteger`` / ``MatMulInteger`` / ``DynamicQuantizeLinear``
  nodes (the ops modern ONNX Runtime removed from CPU), and
- loads and runs on the locally installed ONNX Runtime.

It also pins the regression this suite was written for: the old
``quantize_dynamic`` output still contains ``ConvInteger`` nodes, which fail
with ``NOT_IMPLEMENTED`` on CPU when ORT no longer ships those kernels.

Skips cleanly when onnxruntime/onnx are absent (the unit tests never require
them).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

onnx = pytest.importorskip('onnx')
ort = pytest.importorskip('onnxruntime')
pytest.importorskip('onnxruntime.quantization')

from onnx import TensorProto, helper  # noqa: E402

import numpy as np  # noqa: E402

import app.quantization as quantization  # noqa: E402
from app.quantization import int8_cache_path, quantize_int8  # noqa: E402


def _tiny_conv_model(path: Path) -> Path:
    """A two-Conv model with a shape-inferable graph and a 4-D float input.

    Weights are seeded random so each tensor spans a real (non-degenerate)
    range -- all-constant weights would collapse to a zero quantization
    scale.
    """
    rng = np.random.default_rng(1234)
    conv1_w = helper.make_tensor(
        'conv1_w', TensorProto.FLOAT, [4, 3, 3, 3],
        rng.normal(0.0, 0.1, 4 * 3 * 3 * 3).astype(np.float32).tolist(),
    )
    conv1_b = helper.make_tensor('conv1_b', TensorProto.FLOAT, [4], [0.0] * 4)
    conv2_w = helper.make_tensor(
        'conv2_w', TensorProto.FLOAT, [2, 4, 3, 3],
        rng.normal(0.0, 0.1, 2 * 4 * 3 * 3).astype(np.float32).tolist(),
    )
    conv2_b = helper.make_tensor('conv2_b', TensorProto.FLOAT, [2], [0.0] * 2)
    graph = helper.make_graph(
        [
            helper.make_node('Conv', ['input', 'conv1_w', 'conv1_b'], ['c1'], name='conv1'),
            helper.make_node('Relu', ['c1'], ['r1'], name='relu1'),
            helper.make_node('Conv', ['r1', 'conv2_w', 'conv2_b'], ['c2'], name='conv2'),
        ],
        'tiny_conv',
        [helper.make_tensor_value_info('input', TensorProto.FLOAT, [1, 3, 64, 64])],
        [helper.make_tensor_value_info('c2', TensorProto.FLOAT, [1, 2, 60, 60])],
        [conv1_w, conv1_b, conv2_w, conv2_b],
    )
    # Newer ``onnx`` releases default to an IR version newer than the
    # ONNX Runtime wheel used by the supported dependency range can read
    # (for example, ORT 1.18 accepts IR <= 11). Keep this synthetic fixture
    # portable so the test reaches quantization/kernel validation instead of
    # failing during model loading with an unrelated IR-version error.
    model = helper.make_model(
        graph,
        opset_imports=[helper.make_opsetid('', 13)],
        ir_version=9,
    )
    onnx.save(model, str(path))
    return path


def test_quantize_int8_produces_loadable_qdq_model(tmp_path: Path):
    """The QDQ quantized cache must load and run on CPU, with no legacy
    Integer-format ops left in the graph."""
    source = _tiny_conv_model(tmp_path / 'tiny.onnx')
    quantized = quantize_int8(source)
    assert quantized is not None
    assert quantized == int8_cache_path(source)
    assert quantized.is_file()

    model = onnx.load(str(quantized))
    # The final graph-output producer is intentionally excluded so YOLO box
    # and confidence values remain FP32 after calibration. This is the
    # regression guard for detections disappearing under INT8.
    assert quantization._model_output_nodes(source) == ['conv2']
    output_elem_type = model.graph.output[0].type.tensor_type.elem_type
    assert output_elem_type == TensorProto.FLOAT
    op_types = {node.op_type for node in model.graph.node}
    # QDQ format only: QuantizeLinear/DequantizeLinear pairs, no legacy ops.
    assert 'ConvInteger' not in op_types
    assert 'MatMulInteger' not in op_types
    assert 'DynamicQuantizeLinear' not in op_types
    assert {'QuantizeLinear', 'DequantizeLinear'} & op_types

    session = ort.InferenceSession(str(quantized), providers=['CPUExecutionProvider'])
    out = session.run(None, {'input': np.zeros((1, 3, 64, 64), dtype=np.float32)})
    assert out and out[0].shape == (1, 2, 60, 60)
    assert out[0].dtype == np.float32


def test_legacy_quantize_dynamic_output_fails_on_modern_cpu_ort(tmp_path: Path):
    """Documents WHY the app moved off ``quantize_dynamic``: its ConvInteger
    output no longer gets a CPU kernel on current ORT, raising the exact
    NOT_IMPLEMENTED error reported by users. Skips on ORT builds that still
    ship the legacy kernel."""
    from onnxruntime.quantization import QuantType, quantize_dynamic

    source = _tiny_conv_model(tmp_path / 'legacy.onnx')
    legacy = tmp_path / 'legacy.int8.onnx'
    quantize_dynamic(str(source), str(legacy), weight_type=QuantType.QInt8)
    assert any(node.op_type == 'ConvInteger' for node in onnx.load(str(legacy)).graph.node)
    try:
        ort.InferenceSession(str(legacy), providers=['CPUExecutionProvider'])
    except Exception as exc:
        error_text = str(exc)
        # Different ORT builds include either the offending node type or a
        # generic kernel-resolution marker. Reject an unrelated load failure
        # (such as unsupported IR/opset) while keeping the test portable across
        # modern CPU wheels.
        assert any(marker in error_text for marker in ('ConvInteger', 'NOT_IMPLEMENTED', 'no implementation'))
    else:
        pytest.skip('installed ONNX Runtime still ships the legacy ConvInteger kernel')
