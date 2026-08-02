"""ONNX Runtime quantization + precision-dispatch helpers.

Cluster membership:

- ``int8_quantization_available()`` -- capability check for
  ``onnxruntime.quantization`` (ships with both ``onnxruntime`` and
  ``onnxruntime-gpu`` since 1.16).

- ``int8_cache_path(model_path)`` -- deterministic on-disk path for the
  cached INT8-quantized copy of a model.

- ``quantize_int8(model_path)`` -- quantize a FP32 ONNX to an INT8 QDQ
  model via ``onnxruntime.quantization.quantize_static`` (QDQ format with
  synthetic MinMax calibration) and cache to disk. QDQ is the only format
  modern ONNX Runtime accelerates on CPU: the legacy ``quantize_dynamic``
  output (``ConvInteger``/``MatMulInteger`` nodes) no longer has CPU kernels
  in recent ORT builds.

- ``invalidate_int8_cache(model_path)`` -- delete a cached INT8 model that
  failed to load at runtime so the next reload re-quantizes.

- ``precision_export_kwargs(precision, device, onnxruntime_gpu_available)``
  -- emit the Ultralytics ``YOLO(...).export(...)`` kwargs string fragment
  for the requested precision. ``fp16`` only emits ``half=True`` when
  device is CUDA AND onnxruntime-gpu is available; ``int8`` is NOT
  honored at export time (Ultralytics' ``int8=True`` requires calibration
  data -- INT8 happens at runtime via ``quantize_int8``).

Cache invalidation uses source mtime plus structural ONNX validation and
source provenance:
- A re-quantization is triggered when the cached ``*.int8.onnx`` is missing,
  older than the source, lacks its source-hash sidecar, carries a legacy
  cache-format marker, or is not a valid ONNX graph.
- A SHA-256 source signature is stored in a sibling ``*.int8.sha256`` sidecar
  and checked before reusing a cache. The signature is also checked again
  before replacement, preventing a cache from being accepted when the source
  was replaced during quantization.
- ``model_management.export_yolo_onnx`` rewrites the source file, so its
  mtime/size changes whenever a new export is produced.

Pool-C reach sites (resolved at call time inside function bodies):

- ``app.detector._run_inference`` -- ``quantize_int8`` is invoked once
  during ``OnnxYoloDetector.__init__`` when ``precision='int8'``.
- ``app.model_management._export_kwargs`` -- imports
  ``precision_export_kwargs`` to compose the Ultralytics export kwargs
  string when ``precision`` is non-default.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger('daygle.ai')

# Dynamic quantization can be requested by multiple detector reloads at once
# (for example, a settings save racing with a camera worker). The temporary
# output below makes each conversion safe across processes; this lock avoids
# duplicate work and keeps same-process cache checks/conversions serialized.
_int8_quantization_lock = threading.Lock()


@contextlib.contextmanager
def _int8_process_lock(cache: Path):
    """Serialize cache readers/writers across worker processes.

    The thread lock alone cannot protect deployments that run multiple
    processes. A one-byte advisory lock keeps cache and provenance-sidecar
    publication together without introducing another dependency; the lock
    file is intentionally retained as a harmless sibling after release.
    """
    lock_path = cache.with_suffix('.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+b')
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            handle.write(b'0')
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except Exception as unlock_exc:  # pragma: no cover - OS-specific
                    # Never mask the conversion/deletion exception with an
                    # advisory-lock cleanup failure.
                    logger.warning('Failed to release INT8 cache lock %s: %s', lock_path, unlock_exc)
        finally:
            handle.close()


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


def _int8_cache_metadata_path(model_path: Path) -> Path:
    """Return the sidecar that records which source produced the cache."""
    return int8_cache_path(model_path).with_suffix('.sha256')


def _source_signature(source: Path) -> str:
    """Return a content signature used to detect replacement races."""
    digest = hashlib.sha256()
    with source.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _is_valid_onnx_model(path: Path) -> bool:
    """Return whether ``path`` is a structurally valid ONNX model.

    Existing caches may have been produced by an interrupted older version
    that wrote directly to the final filename. Rejecting malformed caches is
    important because mtime alone cannot distinguish a complete graph from a
    truncated file. The small fallback keeps the helper usable in minimal
    environments where the optional ``onnx`` package is absent; callers then
    conservatively reject the cache instead of trusting a non-empty file.
    """
    if not path.is_file():
        return False
    try:
        import onnx  # type: ignore[import-untyped]
        model = onnx.load(str(path))
        onnx.checker.check_model(model)
        return True
    except ImportError:
        # ``onnx`` is a declared runtime dependency. If an installation is
        # incomplete, do not trust an arbitrary non-empty cache as a model.
        return False
    except Exception:
        return False


def _should_requantize(source: Path, cache: Path) -> bool:
    """Return ``True`` iff the INT8 cache is missing or older than the source.

    Structural validity is checked separately by ``quantize_int8``.
    The source is rewritten by ``model_management.export_yolo_onnx``; on
    filesystems with coarse mtime resolution this may over-requantize once,
    which is harmless.
    """
    if not cache.exists():
        return True
    try:
        return cache.stat().st_mtime < source.stat().st_mtime
    except FileNotFoundError:
        # Source vanished between ``cache.exists()`` and ``source.stat()``
        # -- treat as missing; the caller will fall back to FP32.
        return True


# ---------------------------------------------------------------------------
# INT8 QDQ quantization (static, synthetic calibration)
# ---------------------------------------------------------------------------

# The legacy ``quantize_dynamic`` (Integer-ops) format no longer runs on
# recent ONNX Runtime CPU builds: the ConvInteger/MatMulInteger kernels were
# removed, so a dynamically quantized model fails to load with "Could not find
# an implementation for ConvInteger(10) node" and the detector silently falls
# back to FP32. QDQ (QuantizeLinear/DequantizeLinear) models fuse into
# QLinearConv/QLinearMatMul kernels that every modern ORT CPU build ships, so
# INT8 is produced with ``quantize_static`` here. ``quantize_static`` needs a
# calibration set; there is no labelled dataset at install time, so a fixed,
# seeded set of camera-like synthetic frames is used (see
# ``_SyntheticCalibrationReader``). Calibration is a one-time cost, cached to
# ``*.int8.onnx``.
_INT8_CACHE_FORMAT = 'int8-qdq-v1'
_CALIBRATION_SAMPLE_COUNT = 64


def _read_cache_metadata(metadata: Path) -> str:
    """Return the raw sidecar text, or ``''`` when missing/unreadable."""
    try:
        return metadata.read_text(encoding='ascii').strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return ''


def _cached_signature_matches(metadata: Path, source_signature: str) -> bool:
    """Return whether the sidecar records this source hash AND the current
    cache-format marker.

    Requiring the marker means caches produced by older code paths (including
    the pre-QDQ ``quantize_dynamic`` era, whose artifacts fail to load on
    modern ORT) are treated as stale and re-quantized exactly once instead of
    being reused forever.
    """
    lines = [line.strip() for line in _read_cache_metadata(metadata).splitlines() if line.strip()]
    return len(lines) >= 2 and lines[0] == source_signature and lines[1] == _INT8_CACHE_FORMAT


def _model_input_contract(model_path: Path) -> tuple[str, tuple[int, ...]]:
    """Return the model's first real (non-initializer) input name and a
    concrete 4-D shape for calibration tensors.

    Fixed-shape exports (the normal case) are used verbatim; dynamic
    dimensions are replaced with detector defaults (batch 1, 3 channels,
    640x640) so the synthetic calibration reader can materialise tensors.
    """
    import onnx  # type: ignore[import-untyped]
    model = onnx.load(str(model_path))
    initializer_names = {initializer.name for initializer in model.graph.initializer}
    for tensor_input in model.graph.input:
        if tensor_input.name in initializer_names:
            continue
        dims = [
            dim.dim_value if dim.HasField('dim_value') and dim.dim_value > 0 else None
            for dim in tensor_input.type.tensor_type.shape.dim
        ]
        if len(dims) == 4:
            batch, channels, height, width = dims
            return tensor_input.name, (
                int(batch or 1),
                int(channels or 3),
                int(height or 640),
                int(width or 640),
            )
    raise ValueError('model input is not a 4-D tensor; cannot synthesise calibration data')


class _SyntheticCalibrationReader:
    """Deterministic iterable calibration reader for ``quantize_static``.

    Yields ``count`` letterbox-style frames -- flat gray base with soft
    lighting gradients, blurred blob "objects" and per-channel noise,
    normalised to float32 in [0, 1] exactly like
    ``OnnxYoloDetector._preprocess`` -- so the MinMax activation ranges
    resemble a real deployment closely enough for QDQ calibration without any
    on-disk dataset. Regenerating from a fixed seed keeps every re-quantize
    reproducible.
    """

    def __init__(
        self,
        input_name: str,
        shape: tuple[int, ...],
        count: int = _CALIBRATION_SAMPLE_COUNT,
        seed: int = 20260802,
    ) -> None:
        self._input_name = input_name
        self._shape = tuple(int(d) for d in shape)
        self._count = count
        self._seed = seed
        # ``get_next``-driven iteration state; ``rewind`` restarts both so a
        # re-iterated reader reproduces identical frames (same seed).
        self._index = 0
        self._rng = None

    def __len__(self) -> int:
        return self._count

    def _make_rng(self) -> Any:
        import numpy as np  # type: ignore[import-untyped]
        return np.random.default_rng(self._seed)

    def get_next(self) -> dict[str, Any] | None:
        """Return the next calibration sample, or ``None`` when exhausted.

        ONNX Runtime's calibrator drives calibration through the
        ``CalibrationDataReader`` protocol (``get_next``/``rewind``), not
        plain iteration -- this is the method ``quantize_static`` calls.
        """
        if self._rng is None:
            self._rng = self._make_rng()
        if self._index >= self._count:
            return None
        self._index += 1
        return {self._input_name: self._synthetic_frame(self._rng)}

    def rewind(self) -> None:
        """Restart the sequence from the same seed (deterministic)."""
        self._index = 0
        self._rng = None

    def _synthetic_frame(self, rng: Any) -> Any:
        import numpy as np  # type: ignore[import-untyped]
        batch, channels, height, width = self._shape
        # Flat letterbox-gray base with per-sample brightness variance.
        frame = np.full((height, width), rng.uniform(0.15, 0.55), dtype=np.float32)        # Soft horizontal/vertical lighting gradients.
        frame += rng.uniform(-0.20, 0.20) * np.linspace(0.0, 1.0, width, dtype=np.float32)
        frame += rng.uniform(-0.20, 0.20) * np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
        # Blurred blobs act as object stand-ins so middle layers see
        # structured (not purely noisy) activations.
        for _ in range(int(rng.integers(3, 10))):
            bh = int(rng.integers(height // 32, max(2, height // 4)))
            bw = int(rng.integers(width // 32, max(2, width // 4)))
            y0 = int(rng.integers(0, max(1, height - bh)))
            x0 = int(rng.integers(0, max(1, width - bw)))
            frame[y0 : y0 + bh, x0 : x0 + bw] += rng.uniform(-0.45, 0.85)
        frame = np.clip(frame, 0.0, 1.0)
        # Independent per-channel noise plus slight per-channel gain.
        noise = rng.normal(0.0, 0.04, size=(channels, height, width)).astype(np.float32)
        gain = rng.uniform(0.85, 1.15, size=(channels, 1, 1)).astype(np.float32)
        tensor = np.clip(frame[None, :, :] * gain + noise, 0.0, 1.0).astype(np.float32)
        return np.broadcast_to(tensor, (batch, channels, height, width)).copy()


def quantize_int8(model_path: Path) -> Path | None:
    """Cache an INT8 QDQ-quantized copy of ``model_path`` and return its path.

    Returns ``None`` when:
    - ``onnxruntime.quantization`` is not importable,
    - the source model is missing,
    - the quantization raises (callers fall back to FP32 with a warning).

    Uses ``quantize_static`` in QDQ format (the representation modern ORT CPU
    builds accelerate) with a synthetic MinMax calibration set, so it can run
    unattended at detector load time and is cached to disk.
    """
    source = Path(model_path)
    if not source.is_file():
        return None
    cache = int8_cache_path(source)
    metadata = _int8_cache_metadata_path(source)
    with _int8_quantization_lock, _int8_process_lock(cache):
        try:
            source_signature = _source_signature(source)
        except FileNotFoundError:
            return None

        # Re-check after waiting: another detector may have populated the
        # cache while this caller was blocked on the lock. Validate even a
        # fresh-by-mtime cache so a truncated legacy cache or a cache produced
        # from a different source cannot poison the detector load.
        if not _should_requantize(source, cache):
            if _cached_signature_matches(metadata, source_signature) and _is_valid_onnx_model(cache):
                return cache
            # A missing quantization dependency should not destroy an older
            # artifact; it is unusable for this request but may be recoverable
            # after dependencies are restored. Only remove it once a new
            # conversion is actually available.
            if not int8_quantization_available():
                return None
            logger.warning('Discarding invalid or stale-provenance INT8 cache for %s.', source)
            cache.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)

        if not int8_quantization_available():
            return None

        temporary_path: Path | None = None
        temporary_metadata: Path | None = None
        fd: int | None = None
        try:
            from onnxruntime.quantization import (  # type: ignore[import-untyped]
                CalibrationMethod,
                QuantFormat,
                QuantType,
                quantize_static,
            )
            input_name, input_shape = _model_input_contract(source)
            calibration_reader = _SyntheticCalibrationReader(input_name, input_shape)
            logger.info(
                'INT8 QDQ quantization (synthetic MinMax calibration, %d samples) for %s ...',
                len(calibration_reader),
                source,
            )

            # Never let ORT write directly to the cache. A killed or failed
            # conversion must not leave a newer, truncated file that passes
            # the mtime check on the next startup. A unique .onnx-suffixed
            # temporary path also works with ORT versions that validate the
            # output extension.
            fd, temporary_name = tempfile.mkstemp(
                prefix=f'.{cache.stem}.',
                suffix='.tmp.onnx',
                dir=str(cache.parent),
            )
            # Close the descriptor before removing the placeholder and before
            # ORT opens the path; this matters on Windows, where an open
            # descriptor can prevent unlinking or replacement.
            os.close(fd)
            fd = None
            temporary_path = Path(temporary_name)
            temporary_path.unlink(missing_ok=True)
            quantize_static(
                model_input=str(source),
                model_output=str(temporary_path),
                calibration_data_reader=calibration_reader,
                quant_format=QuantFormat.QDQ,
                activation_type=QuantType.QUInt8,
                weight_type=QuantType.QInt8,
                calibrate_method=CalibrationMethod.MinMax,
            )
            if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
                raise RuntimeError('quantize_static produced no usable output file')
            if _source_signature(source) != source_signature:
                raise RuntimeError('source model changed during INT8 quantization')
            if not _is_valid_onnx_model(temporary_path):
                raise RuntimeError('quantize_static produced an invalid ONNX model')
            # Write provenance to a sibling temporary file before publishing
            # either artifact. The cache itself is still replaced atomically;
            # a crash between the two replaces is safe because the next call
            # sees the missing/mismatched sidecar and re-quantizes.
            fd, metadata_name = tempfile.mkstemp(
                prefix=f'.{metadata.stem}.',
                suffix='.tmp',
                dir=str(metadata.parent),
            )
            os.close(fd)
            fd = None
            temporary_metadata = Path(metadata_name)
            temporary_metadata.write_text(
                f'{source_signature}\n{_INT8_CACHE_FORMAT}\n',
                encoding='ascii',
            )
            temporary_path.replace(cache)
            temporary_path = None
            temporary_metadata.replace(metadata)
            temporary_metadata = None
            # Close the source-race window around publication as well. If the
            # source changed after quantization, discard this cache rather than
            # returning a model built from an older source.
            if _source_signature(source) != source_signature:
                cache.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                raise RuntimeError('source model changed while publishing INT8 cache')
            return cache
        except Exception as exc:  # pragma: no cover - quantization failure is environment-specific
            logger.warning('INT8 quantization failed for %s: %s', source, exc)
            if fd is not None:
                os.close(fd)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if temporary_metadata is not None:
                temporary_metadata.unlink(missing_ok=True)
            # Deliberately preserve an older cache. It is still stale and will
            # not be returned by this call, but preserving it avoids turning a
            # transient conversion failure into destructive cache loss.
            return None


def invalidate_int8_cache(model_path: Path) -> None:
    """Delete the INT8 cache and its provenance sidecar for ``model_path``.

    Used when a cached quantized model fails to load or warm up at runtime: a
    cache that cannot run is worse than no cache, because the detector would
    otherwise reuse it on every restart and keep silently falling back to
    FP32. Deleting it makes the next reload re-quantize from scratch.
    """
    source = Path(model_path)
    cache = int8_cache_path(source)
    metadata = _int8_cache_metadata_path(source)
    with _int8_quantization_lock, _int8_process_lock(cache):
        cache.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)


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
      codebase -- see ``quantize_int8``. Ultralytics'
      ``int8=True`` would require a calibration dataset which the
      download flow can't supply.
    - ``fp32``: always returns ``''``.
    """
    if precision == 'fp16' and device.lower() == 'cuda' and onnxruntime_gpu:
        return 'half=True'
    return ''
