"""``model_export_info`` reads how an installed ONNX file was exported."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

onnx = pytest.importorskip('onnx')
from onnx import TensorProto, helper  # noqa: E402

import app.model_management as mm  # noqa: E402


def _write_model(path: Path, *, metadata: dict[str, str] | None = None, input_type=TensorProto.FLOAT, opset: int = 13) -> Path:
    node = helper.make_node('Identity', ['images'], ['output0'])
    graph = helper.make_graph(
        [node],
        'g',
        [helper.make_tensor_value_info('images', input_type, [1, 3, 32, 32])],
        [helper.make_tensor_value_info('output0', input_type, [1, 3, 32, 32])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', opset)])
    for key, value in (metadata or {}).items():
        prop = model.metadata_props.add()
        prop.key, prop.value = key, value
    onnx.save(model, str(path))
    return path


def test_reads_ultralytics_fp16_export(tmp_path):
    path = _write_model(tmp_path / 'm.onnx', metadata={
        'version': '8.4.176',
        'args': "{'batch': 1, 'quantize': 16, 'nms': None, 'simplify': True, 'opset': 13}",
    })
    assert mm.model_export_info(path) == {
        'precision': 'fp16',
        'simplified': True,
        'opset': 13,
        'ultralytics_version': '8.4.176',
    }


def test_reads_fp32_unsimplified_export(tmp_path):
    path = _write_model(tmp_path / 'm.onnx', metadata={
        'version': '8.4.176',
        'args': "{'batch': 1, 'quantize': None, 'nms': False, 'simplify': False, 'opset': 13}",
    })
    info = mm.model_export_info(path)
    assert info['precision'] == 'fp32'
    assert info['simplified'] is False


def test_legacy_half_arg_and_fp16_input_read_as_fp16(tmp_path):
    legacy = _write_model(tmp_path / 'legacy.onnx', metadata={'args': "{'half': True, 'simplify': True}"})
    assert mm.model_export_info(legacy)['precision'] == 'fp16'
    fp16_io = _write_model(tmp_path / 'io.onnx', input_type=TensorProto.FLOAT16)
    assert mm.model_export_info(fp16_io)['precision'] == 'fp16'


def test_model_without_ultralytics_metadata(tmp_path):
    path = _write_model(tmp_path / 'm.onnx', opset=17)
    assert mm.model_export_info(path) == {
        'precision': 'fp32',
        'simplified': None,
        'opset': 17,
        'ultralytics_version': None,
    }


def test_unreadable_or_missing_file_returns_none(tmp_path):
    assert mm.model_export_info(tmp_path / 'missing.onnx') is None
    junk = tmp_path / 'junk.onnx'
    junk.write_bytes(b'not an onnx file')
    assert mm.model_export_info(junk) is None


def test_reexport_replaces_cached_info(tmp_path):
    path = _write_model(tmp_path / 'm.onnx', metadata={'args': "{'quantize': 16}"})
    assert mm.model_export_info(path)['precision'] == 'fp16'
    _write_model(path, metadata={'args': "{'quantize': None}"})
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert mm.model_export_info(path)['precision'] == 'fp32'
    assert sum(1 for key in mm._export_info_cache if key[0] == str(path)) == 1
