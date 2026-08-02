"""ONNX Runtime quantization + precision-dispatch helpers.

Cluster membership:

- ``int8_quantization_available()`` -- capability check for
  ``onnxruntime.quantization`` (ships with both ``onnxruntime`` and
  ``onnxruntime-gpu`` since 1.16).

- ``int8_cache_path(model_path)`` -- deterministic on-disk path for the
  cached INT8-quantized copy of a model.

- ``quantize_int8(model_path)`` -- quantize a FP32 ONNX to an INT8 QDQ
  model via ``onnxruntime.quantization.quantize_static`` (QDQ format with
  camera-frame MinMax calibration plus synthetic fallback) and cache to disk.
  QDQ is the only format
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
import queue
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import app.state as _state

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
# INT8 QDQ quantization (static, camera-aware calibration)
# ---------------------------------------------------------------------------

# The legacy ``quantize_dynamic`` (Integer-ops) format no longer runs on
# recent ONNX Runtime CPU builds: the ConvInteger/MatMulInteger kernels were
# removed, so a dynamically quantized model fails to load with "Could not find
# an implementation for ConvInteger(10) node" and the detector silently falls
# back to FP32. QDQ (QuantizeLinear/DequantizeLinear) models fuse into
# QLinearConv/QLinearMatMul kernels that every modern ORT CPU build ships, so
# INT8 is produced with ``quantize_static`` here. When cameras are configured,
# a few frames are sampled from their active instances before quantization so
# activation ranges resemble the deployment. Offline cameras contribute
# deterministic synthetic frames instead; the same synthetic reader remains
# the complete fallback when no real frame can be obtained. Calibration is a
# one-time cost, cached to ``*.int8.onnx``.
_INT8_CACHE_FORMAT = 'int8-qdq-v2-camera-calibration'
_CALIBRATION_SAMPLE_COUNT = 64
_REAL_CALIBRATION_FRAMES_PER_CAMERA = 2
_REAL_CALIBRATION_MAX_FRAMES = 16
_REAL_CALIBRATION_TIMEOUT_SECONDS = 4.0


def _configured_camera_count() -> int:
    """Return the number of enabled, identified cameras in the config."""
    return sum(
        1
        for config in list(getattr(_state, 'cameras_config', []) or [])
        if isinstance(config, dict)
        and config.get('id')
        and config.get('enabled', True) is not False
    )


def _read_shared_ingest_frame(camera_id: str) -> tuple[Any, dict[str, Any]] | None:
    """Read one frame from the shared camera ingest without opening RTSP."""
    try:
        from app.camera_instance import read_ingest_frame
        return read_ingest_frame(camera_id)
    except Exception as exc:
        logger.debug('Shared ingest frame read failed for camera %s: %s', camera_id, exc)
        return None


def _prime_shared_ingest(configs: list[dict[str, Any]]) -> set[str]:
    """Best-effort start of shared ingest before first-start calibration.

    Detector construction happens before the live-monitor loop on a fresh
    boot. Starting the existing shared ingest here lets the sampler consume
    its latest JPEG rather than opening a second uncancellable RTSP capture.
    The worker itself is asynchronous; offline cameras simply yield no frame
    and remain covered by synthetic calibration samples.
    """
    service = getattr(_state, 'recording_service', None)
    prime = getattr(service, 'prime_rtsp_prebuffer', None)
    if not callable(prime):
        return set()
    try:
        from app.config_facades import effective_recording_config
        recording_config = effective_recording_config()
    except Exception:
        recording_config = dict(getattr(_state, 'config', {}).get('recording', {}) or {})
    try:
        from app.utils import build_stream_url
    except Exception:
        return set()
    primed_ids: set[str] = set()
    for config in configs:
        try:
            stream_url = build_stream_url(config)
            if stream_url:
                result = prime(
                    stream_url=stream_url,
                    camera_id=str(config['id']),
                    recording_config=recording_config,
                )
                # ``False`` means ffmpeg/ingest could not be started. Treat
                # ``None`` as success for lightweight test doubles and legacy
                # services whose prime method has no explicit return value.
                if result is not False:
                    primed_ids.add(str(config['id']))
        except Exception as exc:
            logger.debug('INT8 calibration could not prime camera %s ingest: %s', config.get('id'), exc)
    return primed_ids


def _configured_camera_calibration_frames() -> tuple[list[Any], int]:
    """Best-effort sample frames from configured active camera instances.

    Camera capture is deliberately isolated in daemon workers: an RTSP open
    can block while a camera is offline, and INT8 calibration must never make
    detector startup depend on camera availability. The returned count is the
    number of enabled configured cameras, allowing the caller/reader to report
    synthetic fallback coverage accurately.
    """
    try:
        import numpy as np  # type: ignore[import-untyped]
    except ImportError:
        return [], 0

    configs = [
        config for config in list(getattr(_state, 'cameras_config', []) or [])
        if isinstance(config, dict)
        and config.get('id')
        and config.get('enabled', True) is not False
    ]
    if not configs:
        return [], 0
    instances = getattr(_state, 'camera_instances', {}) or {}
    sampled: list[Any] = []
    primed_ids = _prime_shared_ingest(configs)
    shared_ingest_ready = callable(getattr(getattr(_state, 'recording_service', None), 'latest_frame_jpeg', None))
    if not shared_ingest_ready or not primed_ids:
        # There is no cancellable read API on the camera backend. Do not call
        # read_frame/read_jpeg here: an offline OpenCV/RTSP read can block while
        # holding the camera lock after the sampler deadline. The shared ingest
        # is the production-safe source and synthetic samples cover cold/offline
        # startup when it has not produced a frame yet.
        return [], len(configs)

    sample_deadline = time.monotonic() + _REAL_CALIBRATION_TIMEOUT_SECONDS

    def read_instance(camera_id: str, output: queue.Queue, deadline: float) -> None:
        frames: list[Any] = []
        try:
            for frame_index in range(_REAL_CALIBRATION_FRAMES_PER_CAMERA):
                image = None
                # The shared ingest may need a moment to write its first JPEG
                # after being primed. Poll only until the global sampler
                # deadline; never open a second RTSP connection here.
                while time.monotonic() < deadline:
                    shared = _read_shared_ingest_frame(camera_id)
                    if shared is not None:
                        image = shared[0]
                        break
                    time.sleep(0.1)
                if isinstance(image, np.ndarray) and image.ndim == 3 and image.shape[0] > 0 and image.shape[1] > 0:
                    frames.append(image.copy())
                if frame_index + 1 < _REAL_CALIBRATION_FRAMES_PER_CAMERA:
                    time.sleep(0.1)
        except Exception as exc:
            logger.debug('INT8 calibration frame sampling failed for camera %s: %s', camera_id, exc)
        output.put(frames)

    pending: list[tuple[dict[str, Any], queue.Queue, threading.Thread]] = []
    for config in configs:
        camera_id = str(config['id'])
        if camera_id not in primed_ids:
            continue
        instance = instances.get(camera_id)
        if instance is None:
            continue
        output: queue.Queue = queue.Queue(maxsize=1)
        thread = threading.Thread(
            target=read_instance,
            args=(camera_id, output, sample_deadline),
            name=f'int8-calibration-{config["id"]}',
            daemon=True,
        )
        pending.append((config, output, thread))
        thread.start()

    for config, output, thread in pending:
        remaining = max(0.0, sample_deadline - time.monotonic())
        thread.join(timeout=remaining)
        if thread.is_alive():
            logger.info('INT8 calibration camera %s did not provide frames within %.1fs; using synthetic fallback.', config['id'], _REAL_CALIBRATION_TIMEOUT_SECONDS)
            continue
        try:
            sampled.extend(output.get_nowait())
        except queue.Empty:
            continue
        if len(sampled) >= _REAL_CALIBRATION_MAX_FRAMES:
            break
    return sampled[:_REAL_CALIBRATION_MAX_FRAMES], len(configs)


def _read_cache_metadata(metadata: Path) -> str:
    """Return the raw sidecar text, or ``''`` when missing/unreadable."""
    try:
        return metadata.read_text(encoding='ascii').strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return ''


def _cached_real_frame_count(metadata: Path) -> int:
    """Return real-frame provenance from the cache sidecar."""
    lines = [line.strip() for line in _read_cache_metadata(metadata).splitlines() if line.strip()]
    for line in lines[2:]:
        if line.startswith('real_frames='):
            try:
                return max(0, int(line.split('=', 1)[1]))
            except (TypeError, ValueError):
                return 0
    return 0


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
    """Calibration reader combining captured camera frames and fallback data.

    Real BGR frames are letterboxed with the same geometry as detector
    preprocessing. Any remaining samples use deterministic flat/gradient/blob
    frames, so offline cameras never prevent quantization and re-quantization
    remains reproducible for its synthetic portion.
    """

    def __init__(
        self,
        input_name: str,
        shape: tuple[int, ...],
        count: int = _CALIBRATION_SAMPLE_COUNT,
        seed: int = 20260802,
        real_frames: list[Any] | None = None,
    ) -> None:
        self._input_name = input_name
        self._shape = tuple(int(d) for d in shape)
        self._count = count
        self._seed = seed
        self._real_frames = tuple(real_frames or ())
        # ``get_next``-driven iteration state; ``rewind`` restarts both so a
        # re-iterated reader reproduces identical synthetic frames. Real
        # frames are captured once before quantization and replayed on rewind.
        self._index = 0
        self._rng = None

    @property
    def real_frame_count(self) -> int:
        """Number of real camera frames available to this reader."""
        return min(len(self._real_frames), self._count)

    @property
    def synthetic_frame_count(self) -> int:
        """Number of deterministic fallback frames this reader will emit."""
        return max(0, self._count - self.real_frame_count)

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
        sample_index = self._index
        self._index += 1
        if sample_index < self.real_frame_count:
            try:
                return {self._input_name: self._real_frame(self._real_frames[sample_index])}
            except Exception as exc:
                logger.debug('INT8 calibration real-frame preprocessing failed: %s; using synthetic fallback.', exc)
        return {self._input_name: self._synthetic_frame(self._rng)}

    def rewind(self) -> None:
        """Restart the sequence from the same seed (deterministic)."""
        self._index = 0
        self._rng = None

    def _real_frame(self, image: Any) -> Any:
        """Apply the detector's BGR uint8 -> letterboxed NCHW preprocessing."""
        import cv2
        import numpy as np  # type: ignore[import-untyped]
        batch, channels, height, width = self._shape
        image = np.asarray(image)
        if image.ndim != 3:
            raise ValueError('camera calibration frame is not HWC')
        if channels == 1:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)[..., None]
        elif channels != image.shape[2]:
            if channels < image.shape[2]:
                image = image[:, :, :channels]
            else:
                image = np.repeat(image[:, :, :1], channels, axis=2)
        original_height, original_width = image.shape[:2]
        scale = min(width / original_width, height / original_height)
        resized_width = max(1, int(round(original_width * scale)))
        resized_height = max(1, int(round(original_height * scale)))
        resized = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
        canvas = np.empty((height, width, channels), dtype=np.uint8)
        canvas.fill(114)
        pad_x = (width - resized_width) / 2
        pad_y = (height - resized_height) / 2
        left = int(round(pad_x - 0.1))
        top = int(round(pad_y - 0.1))
        canvas[top : top + resized_height, left : left + resized_width] = resized
        tensor = np.transpose(canvas, (2, 0, 1)).astype(np.float32) / 255.0
        return np.broadcast_to(tensor[None, ...], (batch, channels, height, width)).copy()

    def _synthetic_frame(self, rng: Any) -> Any:
        import numpy as np  # type: ignore[import-untyped]
        batch, channels, height, width = self._shape
        # Flat letterbox-gray base with per-sample brightness variance.
        frame = np.full((height, width), rng.uniform(0.15, 0.55), dtype=np.float32)
        # Soft horizontal/vertical lighting gradients.
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
    builds accelerate) with a few real active-camera frames plus deterministic
    synthetic MinMax fallback samples, so it can run unattended at detector
    load time and is cached to disk.
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
        real_frames: list[Any] | None = None
        configured_camera_count = _configured_camera_count()
        if not _should_requantize(source, cache):
            cache_is_valid = _cached_signature_matches(metadata, source_signature) and _is_valid_onnx_model(cache)
            if cache_is_valid:
                cached_real_frames = _cached_real_frame_count(metadata)
                # A synthetic-only cache is reusable while cameras are offline,
                # but when cameras are configured give them one bounded chance
                # to improve that cache with real deployment frames.
                if configured_camera_count == 0 or cached_real_frames > 0:
                    return cache
                real_frames, _ = _configured_camera_calibration_frames()
                if not real_frames:
                    return cache
            if not cache_is_valid:
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
            if real_frames is None:
                real_frames, configured_camera_count = _configured_camera_calibration_frames()
            calibration_reader = _SyntheticCalibrationReader(
                input_name,
                input_shape,
                real_frames=real_frames,
            )
            logger.info(
                'INT8 QDQ quantization (MinMax calibration, %d samples: %d real camera, %d synthetic fallback; %d configured cameras) for %s ...',
                len(calibration_reader),
                calibration_reader.real_frame_count,
                calibration_reader.synthetic_frame_count,
                configured_camera_count,
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
                f'{source_signature}\n{_INT8_CACHE_FORMAT}\nreal_frames={calibration_reader.real_frame_count}\n',
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
