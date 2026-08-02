"""Unit tests for ``app.quantization`` -- the INT8 QDQ cache helper and the
precision / device dispatch used by ``app.detector`` and
``app.model_management``.

These tests cover pure-Python paths only (mtime invalidation, cache-format
markers, string normalization, kwargs assembly, synthetic calibration
shapes); they do NOT require ``onnxruntime.quantization`` to be installed.
The runtime quantize paths are exercised through public introspection
helpers and skip cleanly when the optional dependency is absent. The real
``quantize_static`` pipeline is covered by ``test_quantization_integration.py``
when ONNX Runtime is available.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.quantization as quantization  # noqa: E402
from app.quantization import (  # noqa: E402
    _INT8_CACHE_FORMAT,
    _int8_cache_metadata_path,
    _source_signature,
    int8_cache_path,
    int8_quantization_available,
    invalidate_int8_cache,
    normalize_precision,
    onnxruntime_gpu_available,
    precision_export_kwargs,
    quantize_int8,
)


def _install_fake_quantization_module(monkeypatch, quantize_static, model_input_contract=('images', (1, 3, 64, 64))):
    """Register a stub ``onnxruntime.quantization`` and a fake input contract.

    The in-function ``from onnxruntime.quantization import ...`` and
    ``_model_input_contract(source)`` calls resolve to the stubs regardless
    of whether the real wheel is installed, so the cache logic can be tested
    without a real model or ONNX Runtime.
    """
    qmod = types.ModuleType('onnxruntime.quantization')
    qmod.QuantType = type('Q', (), {'QInt8': 1, 'QUInt8': 2})
    qmod.QuantFormat = type('F', (), {'QDQ': 1})
    qmod.CalibrationMethod = type('C', (), {'MinMax': 1})
    qmod.quantize_static = quantize_static
    if 'onnxruntime' not in sys.modules:
        # CPython's import machinery also needs the parent package to be
        # importable when resolving ``from X.Y import Z``; register an
        # empty stub package so the resolution chain completes.
        stub_pkg = types.ModuleType('onnxruntime')
        stub_pkg.__path__ = []  # mark as a package
        monkeypatch.setitem(sys.modules, 'onnxruntime', stub_pkg)
    monkeypatch.setitem(sys.modules, 'onnxruntime.quantization', qmod)
    monkeypatch.setattr(quantization, '_model_input_contract', lambda path: model_input_contract)


def _fresh_cache(tmp_path: Path, name: str = 'yolo26n.onnx') -> tuple[Path, Path, Path]:
    """Create a source + fresh-by-mtime cache pair with a valid sidecar."""
    source = tmp_path / name
    cache = int8_cache_path(source)
    metadata = _int8_cache_metadata_path(source)
    source.write_bytes(b'fake-source')
    cache.write_bytes(b'fake-cache')
    metadata.write_text(f'{_source_signature(source)}\n{_INT8_CACHE_FORMAT}\n', encoding='ascii')
    os.utime(cache, (2000, 2000))
    os.utime(source, (1000, 1000))
    return source, cache, metadata


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
    ``quantize_int8``. The export kwargs must stay empty."""
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


# -- quantize_int8 ---------------------------------------------------------


def test_quantize_int8_returns_none_for_missing_source(tmp_path: Path):
    """When the source ``.onnx`` doesn't exist (e.g. MODEL MISSING), the
    helper returns ``None`` so the caller falls back to FP32 without
    raising."""
    assert quantize_int8(tmp_path / 'nonexistent.onnx') is None


def test_quantize_int8_returns_none_when_lib_missing(tmp_path: Path, monkeypatch):
    """When ``onnxruntime.quantization`` cannot be imported (the common
    case on minimal installs) the helper returns ``None`` instead of
    raising -- the detector then logs a warning and falls back to FP32."""
    monkeypatch.setattr(quantization, 'int8_quantization_available', lambda: False)
    source = tmp_path / 'yolo26n.onnx'
    source.write_bytes(b'')
    assert quantize_int8(source) is None


def test_quantize_int8_returns_existing_cache_when_fresh(tmp_path: Path, monkeypatch):
    """When the cache exists, its mtime is >= the source's, and its
    sidecar carries the current format marker, the helper short-circuits
    without re-quantizing. Simulated by exposing a fake ``quantize_static``
    that would raise if invoked -- proving the short-circuit works."""
    monkeypatch.setattr(quantization, 'int8_quantization_available', lambda: True)
    monkeypatch.setattr(quantization, '_is_valid_onnx_model', lambda path: True)
    source, cache, _ = _fresh_cache(tmp_path)
    _install_fake_quantization_module(
        monkeypatch,
        quantize_static=lambda **kwargs: (_ for _ in ()).throw(AssertionError('must not re-quantize')),
    )
    assert quantize_int8(source) == cache


def test_quantize_int8_requantizes_when_cache_stale(tmp_path: Path, monkeypatch):
    """When the cache is OLDER than the source (model was re-exported),
    the helper invokes ``quantize_static`` -- simulated here so we
    don't need a real ``onnxruntime`` install."""
    monkeypatch.setattr(quantization, 'int8_quantization_available', lambda: True)
    monkeypatch.setattr(quantization, '_is_valid_onnx_model', lambda path: True)
    source, cache, _ = _fresh_cache(tmp_path)
    os.utime(source, (3000, 3000))
    os.utime(cache, (1000, 1000))
    captured = {}

    def _fake_quantize_static(**kwargs):
        captured['called'] = True
        captured['model_input'] = kwargs['model_input']
        captured['model_output'] = kwargs['model_output']
        captured['reader'] = kwargs['calibration_data_reader']
        captured['format'] = kwargs['quant_format']
        captured['activation_type'] = kwargs['activation_type']
        captured['weight_type'] = kwargs['weight_type']
        Path(kwargs['model_output']).write_bytes(b'fresh-cache')

    _install_fake_quantization_module(monkeypatch, _fake_quantize_static)

    result = quantize_int8(source)
    assert result == cache
    assert captured['called'] is True
    # QDQ static quantization is dispatched, not the legacy quantize_dynamic.
    assert captured['activation_type'] == 2  # QuantType.QUInt8 (stub value)
    assert captured['weight_type'] == 1  # QuantType.QInt8 (stub value)
    # Quantization writes to a private temporary ONNX path and atomically
    # replaces the cache only after completion; callers receive the final
    # cache path, not the now-removed temporary path.
    assert Path(captured['model_output']) != cache
    assert cache.read_bytes() == b'fresh-cache'
    # A calibration reader was supplied (synthetic frames) -- the sidecar
    # records the new cache format marker.
    assert captured['reader'] is not None
    metadata_lines = _int8_cache_metadata_path(source).read_text(encoding='ascii').strip().splitlines()
    assert metadata_lines[1] == _INT8_CACHE_FORMAT
    assert 'real_frames=0' in metadata_lines[2:]


def test_quantize_int8_passes_real_camera_frames_to_static_quantizer(tmp_path: Path, monkeypatch):
    """A cache miss with configured camera samples gives quantize_static a
    reader whose first calibration tensor comes from a real BGR frame."""
    import numpy as np

    monkeypatch.setattr(quantization, 'int8_quantization_available', lambda: True)
    monkeypatch.setattr(quantization, '_is_valid_onnx_model', lambda path: True)
    source = tmp_path / 'camera-calibrated.onnx'
    source.write_bytes(b'fake-source')
    os.utime(source, (3000, 3000))
    captured = {}
    real = np.zeros((32, 64, 3), dtype=np.uint8)
    real[:, :, 2] = 255
    monkeypatch.setattr(quantization, '_configured_camera_calibration_frames', lambda: ([real], 1))

    def _fake_quantize_static(**kwargs):
        reader = kwargs['calibration_data_reader']
        captured['sample'] = reader.get_next()['images']
        Path(kwargs['model_output']).write_bytes(b'fresh-camera-calibrated-cache')

    _install_fake_quantization_module(monkeypatch, _fake_quantize_static)

    result = quantize_int8(source)
    assert result == int8_cache_path(source)
    assert captured['sample'].shape == (1, 3, 64, 64)
    assert captured['sample'].dtype == np.float32
    assert float(captured['sample'][0, 2].max()) > 0.9


def test_quantize_int8_requantizes_legacy_cache_without_format_marker(tmp_path: Path, monkeypatch):
    """A fresh-by-mtime cache whose sidecar predates the QDQ format marker
    (e.g. produced by the old ``quantize_dynamic`` path, which fails to load
    on modern ORT) must NOT be reused -- it is discarded and re-quantized
    exactly once."""
    monkeypatch.setattr(quantization, 'int8_quantization_available', lambda: True)
    monkeypatch.setattr(quantization, '_is_valid_onnx_model', lambda path: True)
    source = tmp_path / 'yolo26n.onnx'
    cache = int8_cache_path(source)
    metadata = _int8_cache_metadata_path(source)
    source.write_bytes(b'fake-source')
    cache.write_bytes(b'legacy-broken-cache')
    # Old-format sidecar: source hash only, no format marker line.
    metadata.write_text(_source_signature(source) + '\n', encoding='ascii')
    os.utime(cache, (2000, 2000))
    os.utime(source, (1000, 1000))

    captured = {}

    def _fake_quantize_static(**kwargs):
        captured['called'] = True
        Path(kwargs['model_output']).write_bytes(b'fresh-qdq-cache')

    _install_fake_quantization_module(monkeypatch, _fake_quantize_static)

    result = quantize_int8(source)
    assert result == cache
    assert captured['called'] is True
    assert cache.read_bytes() == b'fresh-qdq-cache'
    lines = metadata.read_text(encoding='ascii').strip().splitlines()
    assert lines[1] == _INT8_CACHE_FORMAT
    assert 'real_frames=0' in lines[2:]


def test_quantize_int8_preserves_stale_cache_when_conversion_fails(tmp_path: Path, monkeypatch):
    """A failed conversion must not destroy the previous cache artifact."""
    monkeypatch.setattr(quantization, 'int8_quantization_available', lambda: True)
    monkeypatch.setattr(quantization, '_is_valid_onnx_model', lambda path: True)
    source = tmp_path / 'model.onnx'
    cache = int8_cache_path(source)
    source.write_bytes(b'new-source')
    cache.write_bytes(b'old-cache')
    os.utime(source, (3000, 3000))
    os.utime(cache, (1000, 1000))

    def _failing_quantize_static(**kwargs):
        raise RuntimeError('conversion failed')

    _install_fake_quantization_module(monkeypatch, _failing_quantize_static)

    assert quantize_int8(source) is None
    assert cache.read_bytes() == b'old-cache'


# -- synthetic calibration reader -------------------------------------------


def test_synthetic_calibration_reader_yields_model_shaped_frames():
    """The reader yields the requested number of frames via the
    ``get_next``/``rewind`` protocol ONNX Runtime's calibrator drives, each
    keyed by the model's input name with a float32 [0, 1] tensor of the
    model's shape -- the same contract ``OnnxYoloDetector._preprocess`` feeds
    the session."""
    import numpy as np

    reader = quantization._SyntheticCalibrationReader('images', (1, 3, 64, 64), count=8)
    assert len(reader) == 8
    frames = []
    while True:
        sample = reader.get_next()
        if sample is None:
            break
        frames.append(sample)
    assert len(frames) == 8
    for frame in frames:
        assert set(frame) == {'images'}
        tensor = frame['images']
        assert tensor.shape == (1, 3, 64, 64)
        assert tensor.dtype == np.float32
        assert float(tensor.min()) >= 0.0
        assert float(tensor.max()) <= 1.0
    # Exhausted readers return None, not raise.
    assert reader.get_next() is None
    # Deterministic: rewinding reproduces identical tensors from the seed.
    reader.rewind()
    replayed = []
    while True:
        sample = reader.get_next()
        if sample is None:
            break
        replayed.append(sample)
    assert len(replayed) == 8
    for original, replay in zip(frames, replayed):
        assert np.array_equal(original['images'], replay['images'])


def test_calibration_reader_uses_real_frames_then_synthetic_fallback():
    """Captured BGR camera frames are preprocessed first, while the remaining
    calibration set remains deterministic synthetic data."""
    import numpy as np

    real = np.zeros((32, 64, 3), dtype=np.uint8)
    real[:, :, 2] = 255  # distinguish the real frame from gray synthetic data
    reader = quantization._SyntheticCalibrationReader(
        'images', (1, 3, 64, 64), count=3, real_frames=[real]
    )

    first = reader.get_next()['images']
    second = reader.get_next()['images']
    assert reader.real_frame_count == 1
    assert reader.synthetic_frame_count == 2
    assert first.shape == (1, 3, 64, 64)
    assert first.dtype == np.float32
    # BGR red channel lands in CHW channel 2 after preprocessing.
    assert float(first[0, 2].max()) > 0.9
    assert second.shape == (1, 3, 64, 64)
    assert second.dtype == np.float32

    reader.rewind()
    replay = reader.get_next()['images']
    assert np.array_equal(first, replay)


def test_configured_camera_sampler_collects_enabled_frames_and_skips_offline(monkeypatch):
    """Sampling configured cameras is best-effort: available instances
    contribute frames and missing/offline cameras simply use fallback data."""
    import numpy as np

    class FakeCamera:
        def read_frame(self):
            raise AssertionError('INT8 calibration must use shared ingest, not direct RTSP reads')

    real = np.full((24, 32, 3), 80, dtype=np.uint8)

    class FakeRecordingService:
        def prime_rtsp_prebuffer(self, **kwargs):
            return True

        def latest_frame_jpeg(self, camera_id):
            return object()

    monkeypatch.setattr(
        quantization._state,
        'cameras_config',
        [
            {'id': 'online', 'enabled': True, 'stream_url': 'rtsp://camera/stream'},
            {'id': 'offline', 'enabled': True},
            {'id': 'disabled', 'enabled': False},
        ],
    )
    monkeypatch.setattr(quantization._state, 'camera_instances', {'online': FakeCamera()})
    monkeypatch.setattr(quantization._state, 'recording_service', FakeRecordingService())
    monkeypatch.setattr(quantization, '_read_shared_ingest_frame', lambda camera_id: (real, {'timestamp': 1.0}))

    frames, configured_count = quantization._configured_camera_calibration_frames()
    assert configured_count == 2
    assert len(frames) == quantization._REAL_CALIBRATION_FRAMES_PER_CAMERA
    assert all(frame.shape == (24, 32, 3) for frame in frames)


# -- invalidate_int8_cache ---------------------------------------------------


def test_invalidate_int8_cache_removes_cache_and_metadata(tmp_path: Path):
    """A cache that failed to load at runtime is deleted together with its
    provenance sidecar so the next reload re-quantizes."""
    source, cache, metadata = _fresh_cache(tmp_path)
    assert cache.exists() and metadata.exists()
    invalidate_int8_cache(source)
    assert not cache.exists()
    assert not metadata.exists()


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
    metadata = _int8_cache_metadata_path(onnx)
    metadata.write_text('stale-source-hash\n', encoding='ascii')

    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)
    # A different active model, so the delete isn't blocked by the active guard.
    monkeypatch.setattr(mm, 'effective_ai_config', lambda: {'model_path': 'models/yolo11n.onnx'})

    result = mm.delete_model('yolo26n')

    assert result['ok'] is True
    assert not onnx.exists()
    assert not int8.exists()
    assert not _int8_cache_metadata_path(onnx).exists()
