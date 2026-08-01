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

Cache invalidation uses source mtime plus structural ONNX validation and
source provenance:
- A re-quantization is triggered when the cached ``*.int8.onnx`` is missing,
  older than the source, lacks its source-hash sidecar, or is not a valid ONNX
  graph.
- A SHA-256 source signature is stored in a sibling ``*.int8.sha256`` sidecar
  and checked before reusing a cache. The signature is also checked again
  before replacement, preventing a cache from being accepted when the source
  was replaced during quantization.
- ``model_management.export_yolo_onnx`` rewrites the source file, so its
  mtime/size changes whenever a new export is produced.

Pool-C reach sites (resolved at call time inside function bodies):

- ``app.detector._run_inference`` -- ``quantize_int8_dynamic`` is invoked
  once during ``OnnxYoloDetector.__init__`` when ``precision='int8'``.
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

    Structural validity is checked separately by ``quantize_int8_dynamic``.
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
            cached_signature = ''
            try:
                cached_signature = metadata.read_text(encoding='ascii').strip()
            except (FileNotFoundError, OSError, UnicodeError):
                pass
            if cached_signature == source_signature and _is_valid_onnx_model(cache):
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
            from onnxruntime.quantization import QuantType, quantize_dynamic  # type: ignore[import-untyped]

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
            quantize_dynamic(
                model_input=str(source),
                model_output=str(temporary_path),
                weight_type=QuantType.QInt8,
            )
            if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
                raise RuntimeError('quantize_dynamic produced no usable output file')
            if _source_signature(source) != source_signature:
                raise RuntimeError('source model changed during INT8 quantization')
            if not _is_valid_onnx_model(temporary_path):
                raise RuntimeError('quantize_dynamic produced an invalid ONNX model')
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
            temporary_metadata.write_text(source_signature + '\n', encoding='ascii')
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
