"""ONNX Runtime quantization + precision-dispatch helpers.

Cluster membership:

- ``int8_quantization_available()`` -- capability check for
  ``onnxruntime.quantization`` (ships with both ``onnxruntime`` and
  ``onnxruntime-gpu`` since 1.16).

- ``int8_cache_path(model_path)`` -- deterministic on-disk path for the
  cached INT8-quantized copy of a model.

- ``quantize_int8_dynamic(model_path)`` -- quantize a FP32 ONNX to INT8
  via ``onnxruntime.quantization.quantize_dynamic`` (per-tensor weight
  quantization, no calibration data needed) and cache to disk.

- ``precision_export_kwargs(precision, device, onnxruntime_gpu_available)``
  -- emit the Ultralytics ``YOLO(...).export(...)`` kwargs string fragment
  for the requested precision. ``fp16`` only emits ``half=True`` when
  device is CUDA AND onnxruntime-gpu is available; ``int8`` is NOT
  honored at export time (Ultralytics' ``int8=True`` requires calibration
  data -- INT8 happens at runtime via ``quantize_int8_dynamic``).

Cache invalidation strategy is **mtime-only**:
- A re-quantization is triggered when the cached ``*.int8.onnx`` is
  missing OR its mtime is older than the source ``.onnx``.
- ``model_management.export_yolo_onnx`` always rewrites the source file,
  so its mtime advances every time. SHA256 sidecars would be belt-and-
  suspenders but are overkill for this single-writer cache.

Pool-C reach sites (resolved at call time inside function bodies):

- ``app.detector._run_inference`` -- ``quantize_int8_dynamic`` is invoked
  once during ``OnnxYoloDetector.__init__`` when ``precision='int8'``.
- ``app.model_management._export_kwargs`` -- imports
  ``precision_export_kwargs`` to compose the Ultralytics export kwargs
  string when ``precision`` is non-default.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger('daygle.ai')


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------

def int8_quantization_available() -> bool:
    """Return ``True`` iff ``onnxruntime.quantization`` is importable.

    Dynamic quantization requires ``onnxruntime >= 1.16``; static needs the
    same. The module is shipped in both ``onnxruntime`` and
    ``onnxruntime-gpu`` wheels.

    Uses ``importlib.util.find_spec`` for a non-importing probe, falling
    back to a try-import when the parent package isn't installed (which
    makes ``find_spec`` raise ``ModuleNotFoundError`` despite the docs
    claiming it returns ``None``).
    """
    try:
        return importlib.util.find_spec('onnxruntime.quantization') is not None
    except (ModuleNotFoundError, ValueError):
        # Parent package isn't installed; ``find_spec`` raises on the
        # ``onnxruntime`` -> ``onnxruntime.quantization`` resolution.
        try:
            import onnxruntime.quantization  # noqa: F401
            return True
        except ImportError:
            return False


def onnxruntime_gpu_available() -> bool:
    """Return ``True`` iff the CUDA execution provider is registered.

    Defensive try/except: a missing top-level ``onnxruntime`` wheel
    returns False instead of crashing the detector bootstrap.
    """
    try:
        import onnxruntime as ort  # type: ignore[import-untyped]
        return 'CUDAExecutionProvider' in ort.get_available_providers()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# INT8 dynamic quantization cache
# ---------------------------------------------------------------------------

def int8_cache_path(model_path: Path) -> Path:
    """Return the on-disk path used to cache the INT8-quantized model.

    Sibling-file convention keeps the cache alongside the source so
    ``MODEL MISSING`` and cache cleanup paths behave identically.
    """
    return Path(model_path).with_suffix('.int8.onnx')


def _should_requantize(source: Path, cache: Path) -> bool:
    """Return ``True`` iff the INT8 cache is missing or stale.

    ``st_mtime`` comparison is sufficient because the only writer is
    ``model_management.export_yolo_onnx`` and it always replaces the
    source file atomically (advancing its mtime). On non-POSIX filesystems
    with coarse mtime resolution the comparison may over-requantize by
    one cycle, which is harmless.
    """
    if not cache.exists():
        return True
    try:
        return cache.stat().st_mtime < source.stat().st_mtime
    except FileNotFoundError:
        # Source vanished between ``cache.exists()`` and ``source.stat()``
        # -- treat as missing; the caller will fall back to FP32.
        return True


def quantize_int8_dynamic(model_path: Path) -> Path | None:
    """Cache an INT8-quantized copy of ``model_path`` and return its path.

    Returns ``None`` when:
    - ``onnxruntime.quantization`` is not importable,
    - the source model is missing,
    - the quantization raises (callers fall back to FP32 with a warning).

    Per-tensor weight quantization via ``quantize_dynamic`` does not
    require a calibration dataset, so this can run unattended at detector
    load time.
    """
    if not int8_quantization_available():
        return None
    source = Path(model_path)
    if not source.exists():
        return None
    cache = int8_cache_path(source)
    if not _should_requantize(source, cache):
        return cache
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic  # type: ignore[import-untyped]
        quantize_dynamic(
            model_input=str(source),
            model_output=str(cache),
            weight_type=QuantType.QInt8,
        )
    except Exception as exc:  # pragma: no cover - quantization failure is environment-specific
        logger.warning('INT8 quantization failed for %s: %s', source, exc)
        cache.unlink(missing_ok=True)
        return None
    if not cache.exists():
        # ``quantize_dynamic`` writes nothing on a graph-level failure that
        # doesn't raise. Treat that the same as a raise.
        return None
    return cache


# ---------------------------------------------------------------------------
# Precision-dispatch for Ultralytics export
# ---------------------------------------------------------------------------

_VALID_PRECISION_VALUES = {'fp32', 'fp16', 'int8'}


def normalize_precision(value: Any) -> str:
    """Lower-case the precision value and validate it; fallback to ``fp32``.

    Mirrors the lenient posture used elsewhere in the AI settings:
    unknown values default to ``fp32`` rather than raising, so a stale
    ``precision`` in saved config doesn't break detector loading.
    """
    text = str(value or 'fp32').strip().lower()
    if text not in _VALID_PRECISION_VALUES:
        return 'fp32'
    return text


def precision_export_kwargs(
    precision: str,
    device: str,
    onnxruntime_gpu: bool,
) -> str:
    """Return the kwargs string fragment for the requested ``precision``.

    - ``fp16``: emits ``half=True`` only when ``device='cuda'`` AND
      onnxruntime-gpu is registered. Otherwise returns the empty string
      so the export keeps working on CPU hosts that happen to carry the
      ``precision=fp16`` setting from a prior CUDA deployment.
    - ``int8``: always returns ``''``. INT8 is a runtime concern in this
      codebase -- see ``quantize_int8_dynamic``. Ultralytics'
      ``int8=True`` would require a calibration dataset which the
      download flow can't supply.
    - ``fp32``: always returns ``''``.
    """
    if precision == 'fp16' and device.lower() == 'cuda' and onnxruntime_gpu:
        return 'half=True'
    return ''
