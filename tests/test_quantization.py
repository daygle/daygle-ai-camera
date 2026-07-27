"""Unit tests for ``app.quantization`` -- the INT8 cache helper and the
precision / device dispatch used by ``app.detector`` and
``app.model_management``.

These tests cover pure-Python paths only (mtime invalidation, string
normalization, kwargs assembly); they do NOT require
``onnxruntime.quantization`` to be installed. The runtime quantize /
gpu-availability paths are exercised through public introspection
helpers and skip cleanly when the optional dependency is absent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.quantization import (  # noqa: E402
    int8_cache_path,
    int8_quantization_available,
    normalize_precision,
    onnxruntime_gpu_available,
    precision_export_kwargs,
    quantize_int8_dynamic,
)


# -- int8_cache_path --------------------------------------------------------


def test_int8_cache_path_is_sibling_with_replaced_suffix(tmp_path: Path):
    """The cache lives next to the source as ``<model>.int8.onnx`` so the
    existing ``MODEL MISSING`` and ``MODELS_DIR`` cleanup paths handle
    it identically -- the cache follows the source."""
    source = tmp_path / 'yolo26n.onnx'
    assert int8_cache_path(source) == tmp_path / 'yolo26n.int8.onnx'


def test_int8_cache_path_preserves_model_directory(tmp_path: Path):
    """A nested ``models/`` location still produces a sibling cache --
    no directory traversal, no absolute paths."""
    nested = tmp_path / 'models' / 'sub' / 'yolov8n.onnx'
    cache = int8_cache_path(nested)
    assert cache.parent == nested.parent
    assert cache.name == 'yolov8n.int8.onnx'


# -- normalize_precision ---------------------------------------------------


@pytest.mark.parametrize('value,expected', [
    ('fp32', 'fp32'),
    ('fp16', 'fp16'),
    ('int8', 'int8'),
    ('FP16', 'fp16'),
    (' Int8 ', 'int8'),
    ('', 'fp32'),
    (None, 'fp32'),
    ('unknown', 'fp32'),   # silent fallback preserves current behavior
])
def test_normalize_precision(value, expected):
    """Unknown values silently normalise to ``fp32`` so a stale
    ``precision`` in saved config doesn't break detector loading."""
    assert normalize_precision(value) == expected


# -- precision_export_kwargs ----------------------------------------------


def test_precision_export_kwargs_fp16_cuda_gpu_returns_half_true():
    """``fp16 + cuda + onnxruntime-gpu available`` emits ``half=True``;
    this is the only combination the export honors at export time."""
    assert precision_export_kwargs('fp16', 'cuda', onnxruntime_gpu=True) == 'half=True'


def test_precision_export_kwargs_fp16_cuda_no_gpu_returns_empty():
    """``fp16 + cuda + onnxruntime-gpu MISSING`` returns the empty string so
    the export still succeeds on hosts that carry the setting from a
    prior CUDA deployment but no longer have the wheel installed."""
    assert precision_export_kwargs('fp16', 'cuda', onnxruntime_gpu=False) == ''


def test_precision_export_kwargs_fp16_cpu_returns_empty():
    """``fp16 + cpu`` is the silent no-op path (FP16 CUDA tensors don't
    ship on CPU hosts; the export must still succeed)."""
    assert precision_export_kwargs('fp16', 'cpu', onnxruntime_gpu=True) == ''


def test_precision_export_kwargs_int8_returns_empty():
    """``int8`` is a runtime concern -- the export always emits an FP32
    ONNX; INT8 quantization runs in-process via
    ``quantize_int8_dynamic``. The export kwargs must stay empty."""
    for dev in ('cpu', 'cuda', 'auto'):
        assert precision_export_kwargs('int8', dev, onnxruntime_gpu=True) == ''
        assert precision_export_kwargs('int8', dev, onnxruntime_gpu=False) == ''


@pytest.mark.parametrize('precision', ['fp32'])
def test_precision_export_kwargs_fp32_always_empty(precision):
    """``fp32`` (the default) never injects extra kwargs -- preserves
    current behavior and keeps the export kwargs string stable."""
    assert precision_export_kwargs(precision, 'auto', onnxruntime_gpu=False) == ''


# -- capability detection --------------------------------------------------


def test_int8_quantization_available_returns_bool():
    """The capability check returns a bool -- both branches are valid;
    a True result implies ``onnxruntime.quantization`` is importable."""
    result = int8_quantization_available()
    assert isinstance(result, bool)


def test_onnxruntime_gpu_available_returns_bool():
    """Same contract as ``int8_quantization_available`` -- returns a bool,
    does not raise on missing onnxruntime."""
    assert isinstance(onnxruntime_gpu_available(), bool)


# -- quantize_int8_dynamic -------------------------------------------------


def test_quantize_int8_dynamic_returns_none_for_missing_source(tmp_path: Path):
    """When the source ``.onnx`` doesn't exist (e.g. MODEL MISSING), the
    helper returns ``None`` so the caller falls back to FP32 without
    raising."""
    assert quantize_int8_dynamic(tmp_path / 'nonexistent.onnx') is None


def test_quantize_int8_dynamic_returns_none_when_lib_missing(tmp_path: Path, monkeypatch):
    """When ``onnxruntime.quantization`` cannot be imported (the common
    case on minimal installs) the helper returns ``None`` instead of
    raising -- the detector then logs a warning and falls back to FP32."""
    monkeypatch.setattr('app.quantization.int8_quantization_available', lambda: False)
    source = tmp_path / 'yolo26n.onnx'
    source.write_bytes(b'')
    assert quantize_int8_dynamic(source) is None


def test_quantize_int8_dynamic_returns_existing_cache_when_fresh(tmp_path: Path, monkeypatch):
    """When the cache exists AND its mtime is >= the source's mtime the
    helper short-circuits without re-quantizing. Simulated by exposing
    a fake ``quantize_dynamic`` that would raise if invoked -- proving
    the short-circuit works."""
    monkeypatch.setattr('app.quantization.int8_quantization_available', lambda: True)
    source = tmp_path / 'yolo26n.onnx'
    cache = int8_cache_path(source)
    source.write_bytes(b'fake-source')
    cache.write_bytes(b'fake-cache')
    # Cache newer than source: short-circuit, return cache path.
    os.utime(cache, (2000, 2000))
    os.utime(source, (1000, 1000))
    assert quantize_int8_dynamic(source) == cache


def test_quantize_int8_dynamic_requantizes_when_cache_stale(tmp_path: Path, monkeypatch):
    """When the cache is OLDER than the source (model was re-exported),
    the helper invokes ``quantize_dynamic`` -- simulated here so we
    don't need a real ``onnxruntime`` install."""
    import types

    monkeypatch.setattr('app.quantization.int8_quantization_available', lambda: True)
    source = tmp_path / 'yolo26n.onnx'
    cache = int8_cache_path(source)
    source.write_bytes(b'fake-source')
    cache.write_bytes(b'stale-cache')
    os.utime(source, (3000, 3000))
    os.utime(cache, (1000, 1000))
    captured = {}

    def _fake_quantize_dynamic(model_input, model_output, weight_type):
        captured['called'] = True
        captured['model_input'] = model_input
        captured['model_output'] = model_output
        Path(model_output).write_bytes(b'fresh-cache')

    # Build a stub ``onnxruntime.quantization`` and register it in
    # ``sys.modules`` so the in-function ``from onnxruntime.quantization
    # import ...`` resolves to our fake regardless of whether the wheel is
    # actually installed.
    qmod = types.ModuleType('onnxruntime.quantization')
    qmod.QuantType = type('Q', (), {'QInt8': 1})
    qmod.quantize_dynamic = _fake_quantize_dynamic
    if 'onnxruntime' not in sys.modules:
        # CPython's import machinery also needs the parent package to be
        # importable when resolving ``from X.Y import Z``; register an
        # empty stub package so the resolution chain completes.
        stub_pkg = types.ModuleType('onnxruntime')
        stub_pkg.__path__ = []  # mark as a package
        monkeypatch.setitem(sys.modules, 'onnxruntime', stub_pkg)
    monkeypatch.setitem(sys.modules, 'onnxruntime.quantization', qmod)

    result = quantize_int8_dynamic(source)
    assert result == cache
    assert captured['called'] is True
    assert Path(captured['model_output']).read_bytes() == b'fresh-cache'


# -- delete_model INT8 cache cleanup ---------------------------------------

def test_delete_model_removes_int8_cache_sibling(tmp_path: Path, monkeypatch):
    """Deleting a model must also unlink its ``*.int8.onnx`` quantization
    cache so precision=int8 deployments don't leave orphaned files behind."""
    import app.model_management as mm

    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    onnx = models_dir / 'yolo26n.onnx'
    onnx.write_bytes(b'fake onnx')
    int8 = int8_cache_path(onnx)
    int8.write_bytes(b'fake int8')

    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)
    # A different active model, so the delete isn't blocked by the active guard.
    monkeypatch.setattr(mm, 'effective_ai_config', lambda: {'model_path': 'models/yolo11n.onnx'})

    result = mm.delete_model('yolo26n')

    assert result['ok'] is True
    assert not onnx.exists()
    assert not int8.exists()
