from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger('daygle.ai')

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised in minimal installs without ONNX support
    np = None  # type: ignore[assignment]


@dataclass
class Detection:
    label: str
    confidence: float
    box: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "box": self.box,
        }


class DetectorUnavailableError(RuntimeError):
    """Raised when a configured detector backend cannot run inference."""


_BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve_project_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _BASE_DIR / candidate


def load_labels(labels_path: str | Path | None, fallback: list[str] | None = None) -> list[str]:
    if labels_path:
        path = _resolve_project_path(labels_path)
        if path.is_file():
            labels = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if labels:
                return labels
    return fallback or []


def _require_numpy():
    if np is None:
        raise DetectorUnavailableError("numpy is not installed. Install requirements.txt or run pip install numpy.")
    return np


def box_iou(box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    np = _require_numpy()
    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])

    intersection = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    box_area = max(0.0, float(box[2] - box[0])) * max(0.0, float(box[3] - box[1]))
    boxes_area = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
    union = box_area + boxes_area - intersection
    return intersection / np.maximum(union, 1e-9)


def non_max_suppression(boxes: np.ndarray, scores: np.ndarray, classes: np.ndarray, iou_threshold: float) -> list[int]:
    np = _require_numpy()
    if boxes.size == 0:
        return []

    keep: list[int] = []
    for class_id in np.unique(classes):
        indexes = np.where(classes == class_id)[0]
        ordered = indexes[np.argsort(scores[indexes])[::-1]]
        while ordered.size > 0:
            current = int(ordered[0])
            keep.append(current)
            if ordered.size == 1:
                break
            remaining = ordered[1:]
            ious = box_iou(boxes[current], boxes[remaining])
            ordered = remaining[ious <= iou_threshold]
    return keep


class OnnxYoloDetector:
    """YOLO ONNX detector backed by ONNX Runtime.

    Supports:
    - YOLOv8/YOLO11: Traditional NMS-based detection (output shape: [1, 4+nc, 8400])
    - YOLO26: NMS-free end-to-end detection (output shape: [1, 300, 6])
    """

    backend = "onnx"

    def __init__(
        self,
        model_path: str | Path,
        labels_path: str | Path | None = None,
        confidence: float = 0.45,
        iou_threshold: float = 0.45,
        categories: list[str] | None = None,
        num_threads: int | None = None,
        max_concurrency: int | None = None,
        device: str = "auto",
        gpu_mem_limit: int | None = None,
        nms_free: bool = False,
        execution_mode: str = "parallel",
        confidence_only_nms: bool = False,
        precision: str = "fp32",
        use_io_binding: bool = False,
        input_size: int = 640,
    ) -> None:
        # Settings store project-relative paths (for example,
        # ``models/yolo11n.onnx``). Resolve those against the application root,
        # not the process cwd: systemd has an explicit WorkingDirectory, but
        # CLI invocations, tests, and service wrappers do not necessarily share
        # it.
        self.model_path = _resolve_project_path(model_path)
        self.labels = load_labels(labels_path, categories)
        # The requested size is used for dynamic-input exports and as the
        # initial letterbox size. Fixed-shape ONNX models are corrected below
        # from the session's real input shape.
        try:
            configured_size = int(input_size)
        except (TypeError, ValueError):
            configured_size = 640
        if not 32 <= configured_size <= 2048:
            configured_size = 640
        self.input_width = configured_size
        self.input_height = configured_size
        self.confidence = float(confidence)
        self.iou_threshold = float(iou_threshold)
        self.session: Any | None = None
        self.input_name: str | None = None
        self.output_names: list[str] = []
        self._gpu_mem_limit = gpu_mem_limit
        self.unavailable_reason: str | None = None
        self._device = device.lower() if device else "auto"
        # Keep the safety decision conservative even when a caller constructs
        # the detector directly instead of going through ``create_detector``.
        # YOLO26 exports are NMS-free by definition, and allowing a filename
        # such as ``yolo26l-768.onnx`` to enter the INT8 PTQ path would recreate
        # the silent no-detection failure this guard is meant to prevent.
        self._nms_free = bool(nms_free) or _detect_model_type(self.model_path)
        # ``execution_mode`` selects ORT's ``ORT_SEQUENTIAL`` vs ``ORT_PARALLEL``
        # model-level executor. ``parallel`` matches the ORT default and the
        # prior behavior of this codebase; ``sequential`` is an A/B lever --
        # for small per-inference hot paths the per-op thread pool already
        # covers parallelism and the model-level parallel executor can add
        # overhead. CPU-only (CUDA EP manages its own parallelism).
        self._execution_mode = (execution_mode or "parallel").lower()
        # ``confidence_only_nms`` lets a YOLO26 (NMS-free) deployment drop the
        # class-aware NMS dedupe pass entirely and rely on the model head's
        # one-to-one label assignment + the confidence threshold. Off by
        # default so existing recordings keep their current dedupe behavior;
        # flip on after validating on a labeled set.
        self._confidence_only_nms = bool(confidence_only_nms)
        # ``precision`` selects the on-disk / runtime model layout.
        # ``fp32`` (default -- prior behavior) loads the source ONNX.
        # ``fp16`` assumes the export already produced a half-precision
        # ONNX (handled by ``model_management._export_kwargs``). ``int8``
        # runs ``quantize_int8`` once at constructor time and
        # swaps in the cached quantized copy; on failure we silently
        # fall back to FP32 with a warning.
        self._precision = (precision or "fp32").strip().lower()
        # ``use_io_binding`` activates ORT's direct CUDA memory path. A
        # preflight check after session construction will flip this off
        # when CUDA isn't actually available, so the per-inference
        # branch only ever fires when the session is CUDA-bound.
        self._use_io_binding = bool(use_io_binding)
        # The numpy dtype the model's input tensor expects. Resolved from the
        # loaded session below; an FP16 (``half=True``) export declares a
        # ``tensor(float16)`` input and rejects a float32 array, so the
        # preprocess step casts to whatever the model actually wants. Stays
        # None until the session loads; ``_preprocess`` falls back to float32.
        self._input_dtype: Any = None

        cpu_count = os.cpu_count() or 1
        # Let each inference use multiple cores so it finishes fast, then cap how
        # many inferences run at once. Running many single-threaded inferences in
        # parallel (one per camera + the live overlay) thrashed the CPU and made
        # every detection slow; serialising fast inferences keeps latency low.
        self._num_threads = num_threads if (num_threads and num_threads > 0) else max(1, min(4, cpu_count))
        self._max_concurrency = max_concurrency if (max_concurrency and max_concurrency > 0) else 1
        self._inference_semaphore = threading.Semaphore(self._max_concurrency)

        if not self.model_path.exists():
            self.unavailable_reason = f"ONNX model not found: {self.model_path}"
            return

        if np is None:
            self.unavailable_reason = "numpy is not installed. Install requirements.txt or run pip install numpy."
            return

        try:
            import onnxruntime as ort
        except ImportError:
            self.unavailable_reason = "onnxruntime is not installed. Install requirements.txt or run pip install onnxruntime."
            return

        # Lazy import keeps ``quantization`` off the hot import path of
        # callers that never use INT8 (most CPU deployments default to
        # ``precision='fp32'`` and never touch this branch).
        try:
            from app.quantization import invalidate_int8_cache, quantize_int8
        except ImportError:
            quantize_int8 = None  # type: ignore[assignment]
            invalidate_int8_cache = None  # type: ignore[assignment]

        # Resolve the runtime model path. INT8 quantization runs once at
        # construct time and caches the result; FP16 just loads
        # whatever the export produced (no runtime conversion).
        session_model_path = self.model_path
        int8_runtime_model = False
        if self._precision == 'int8':
            if not _int8_precision_supported_for_detector(self._nms_free):
                # YOLO26's end-to-end/NMS-free head is highly sensitive to
                # post-training activation quantization. A QDQ graph can load
                # and warm up successfully while its confidence values collapse
                # on real frames, yielding no detections. Keep this model on
                # FP32 until a representative/QAT path exists; silently losing
                # object recordings is worse than the speed trade-off.
                logger.warning(
                    'precision=int8 is not supported for NMS-free YOLO output; '
                    'forcing FP32 for %s.',
                    self.model_path,
                )
                self._precision = 'fp32'
            else:
                quantized = quantize_int8(self.model_path) if quantize_int8 else None
                if quantized is not None:
                    session_model_path = Path(quantized)
                    int8_runtime_model = True
                    logger.info(
                        'INT8 QDQ quantization ready for %s -> %s',
                        self.model_path, session_model_path,
                    )
                else:
                    logger.warning(
                        'precision=int8 requested but quantization is unavailable or failed; '
                        'falling back to FP32 model %s.',
                        self.model_path,
                    )
                    self._precision = 'fp32'
        elif self._precision == 'fp16':
            # The exported ONNX is already FP16 -- nothing to convert.
            logger.info('precision=fp16: loading pre-exported half-precision model %s', self.model_path)
        elif self._precision != 'fp32':
            # Unknown precision silently normalises to fp32 so a stale
            # config setting can't break the detector.
            logger.warning(
                'Unknown precision=%r; defaulting to fp32 for %s.',
                self._precision, self.model_path,
            )
            self._precision = 'fp32'

        try:
            available_providers = ort.get_available_providers()
            requested_use_cuda = (
                self._device == "cuda"
                or (self._device == "auto" and "CUDAExecutionProvider" in available_providers)
            )
            # ORT dynamic quantization emits CPU-oriented integer operators.
            # Do not create a CUDA session for that graph: mixed CUDA/CPU EP
            # partitioning can silently move tensors between devices, and it
            # is incompatible with the direct CUDA io-binding path. INT8 is
            # explicitly documented as CPU precision in the UI, so a CUDA
            # device request is safely honored as CPU for this runtime model.
            if int8_runtime_model:
                use_cuda = False
                if self._device in {"cuda", "auto"}:
                    logger.info(
                        'INT8 dynamic model is CPU-bound; using CPUExecutionProvider '
                        'instead of requested device=%s.',
                        self._device,
                    )
                self._use_io_binding = False
            else:
                use_cuda = requested_use_cuda
            if use_cuda and "CUDAExecutionProvider" not in available_providers:
                self.unavailable_reason = (
                    "device=cuda requested but CUDAExecutionProvider is not available. "
                    "Install onnxruntime-gpu and ensure CUDA drivers are present."
                )
                return
            if use_cuda:
                cuda_options: dict[str, Any] = {
                    "device_id": 0,
                    # Allocate on demand rather than greedily pre-allocating all
                    # available VRAM - prevents the BFC arena from consuming the
                    # entire GPU and leaving nothing for cuBLAS or other ops.
                    "arena_extend_strategy": "kSameAsRequested",
                }
                if self._gpu_mem_limit:
                    cuda_options["gpu_mem_limit"] = self._gpu_mem_limit
                providers: list[Any] = [("CUDAExecutionProvider", cuda_options), "CPUExecutionProvider"]
            else:
                providers = ["CPUExecutionProvider"]
            session_options = ort.SessionOptions()
            session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            if not use_cuda:
                # Thread tuning only relevant for CPU execution.
                session_options.intra_op_num_threads = self._num_threads
                session_options.inter_op_num_threads = 1
                # ORT CUDA EP manages its own model-level parallelism, so the
                # executor toggle is meaningful only on the CPU path. Defaults
                # to ``ORT_PARALLEL`` (prior behavior); ``ORT_SEQUENTIAL`` is
                # an opt-in A/B lever from the AI settings form.
                session_options.execution_mode = (
                    ort.ExecutionMode.ORT_SEQUENTIAL
                    if self._execution_mode == "sequential"
                    else ort.ExecutionMode.ORT_PARALLEL
                )
            try:
                self.session = ort.InferenceSession(
                    str(session_model_path),
                    sess_options=session_options,
                    providers=providers,
                )
            except Exception as int8_load_exc:
                # Quantization can succeed while a particular ORT build still
                # rejects the rewritten graph (unsupported op or provider
                # kernel). Do not turn that into an unavailable detector: retry
                # the original FP32 model on the already-selected CPU path.
                if not int8_runtime_model:
                    raise
                logger.warning(
                    'INT8 model could not be loaded (%s); falling back to FP32 model %s.',
                    int8_load_exc,
                    self.model_path,
                )
                # The cached INT8 artifact cannot run on this ORT build. Drop
                # it so the next reload re-quantizes instead of reusing a
                # known-bad cache and falling back to FP32 on every restart.
                # Cache cleanup must never block the FP32 fallback below, so
                # failures here are logged and swallowed.
                if invalidate_int8_cache is not None:
                    try:
                        invalidate_int8_cache(self.model_path)
                    except Exception as invalidate_exc:  # pragma: no cover - filesystem/lock issue
                        logger.debug('Could not invalidate INT8 cache for %s: %s', self.model_path, invalidate_exc)
                self._precision = 'fp32'
                int8_runtime_model = False
                self._use_io_binding = False
                session_model_path = self.model_path
                fallback_providers: list[Any]
                if requested_use_cuda:
                    fallback_cuda_options: dict[str, Any] = {
                        "device_id": 0,
                        "arena_extend_strategy": "kSameAsRequested",
                    }
                    if self._gpu_mem_limit:
                        fallback_cuda_options["gpu_mem_limit"] = self._gpu_mem_limit
                    fallback_providers = [
                        ("CUDAExecutionProvider", fallback_cuda_options),
                        "CPUExecutionProvider",
                    ]
                else:
                    fallback_providers = ["CPUExecutionProvider"]
                self.session = ort.InferenceSession(
                    str(session_model_path),
                    sess_options=session_options,
                    providers=fallback_providers,
                )
            self.input_name = self.session.get_inputs()[0].name
            self.output_names = [output.name for output in self.session.get_outputs()]
            self.active_providers = self.session.get_providers()
            # Preflight: ``io_binding`` is a CUDA-only ORT API. If the session
            # ended up CPU-bound (e.g. user toggled the setting on a host
            # without ``onnxruntime-gpu``, or the version ORT providers list
            # didn't include CUDA), disable + log so the per-call branch
            # never has to handle a runtime-binding explosion.
            if self._use_io_binding and 'CUDAExecutionProvider' not in self.active_providers:
                logger.warning(
                    'use_io_binding=True requested but CUDAExecutionProvider is not '
                    'active (active_providers=%s); falling back to session.run.',
                    self.active_providers,
                )
                self._use_io_binding = False
            # Read the model's actual input shape and override configured
            # input_size if it doesn't match.  This prevents the user from
            # accidentally setting a size that mismatches the exported model
            # (e.g. 768 when the model expects 640x640).
            model_input = self.session.get_inputs()[0]
            model_shape = model_input.shape  # e.g. [1, 3, 640, 640]
            if len(model_shape) == 4:
                model_h = model_shape[2]
                model_w = model_shape[3]
                if isinstance(model_h, int) and isinstance(model_w, int):
                    if (model_h, model_w) != (self.input_height, self.input_width):
                        logger.warning(
                            'ONNX model input shape (%dx%d) differs from configured input_size (%dx%d). '
                            'Using model shape.',
                            model_w, model_h, self.input_width, self.input_height,
                        )
                        self.input_width = model_w
                        self.input_height = model_h
            # Resolve the model's expected input dtype. An FP16 export declares
            # a ``tensor(float16)`` input; feeding it the default float32 tensor
            # raises an ORT type-mismatch, so ``_preprocess`` casts to match.
            input_type = getattr(model_input, 'type', None)
            self._input_dtype = np.float16 if input_type == 'tensor(float16)' else np.float32
            # Warm-up: ORT lazily builds kernels + allocations on the first
            # ``run``, spiking latency on the first live frame after every
            # (re)load. Run one throwaway inference on a zero tensor so the hot
            # path starts warm. Best-effort and self-contained: a warm-up
            # failure must never mark an otherwise-loaded detector unavailable.
            try:
                warm = np.zeros(
                    (1, 3, self.input_height, self.input_width),
                    dtype=self._input_dtype,
                )
                if self._use_io_binding:
                    self._run_inference_io_bound(np.ascontiguousarray(warm))
                else:
                    self.session.run(self.output_names, {self.input_name: warm})
            except Exception as warm_exc:
                if int8_runtime_model:
                    # A quantized graph can parse successfully but still fail
                    # its first real execution because an integer kernel is
                    # unavailable in the installed ORT build. Do not leave a
                    # detector marked available while reporting INT8: retry
                    # the original FP32 graph on CPU instead.
                    logger.warning(
                        'INT8 warm-up failed (%s); falling back to FP32 model %s.',
                        warm_exc,
                        self.model_path,
                    )
                    # Same cache hygiene as the session-construction path: a
                    # quantized graph that parses but fails its first run
                    # should not be reused on every restart. Cleanup failures
                    # are logged, never allowed to block the FP32 fallback.
                    if invalidate_int8_cache is not None:
                        try:
                            invalidate_int8_cache(self.model_path)
                        except Exception as invalidate_exc:  # pragma: no cover - filesystem/lock issue
                            logger.debug('Could not invalidate INT8 cache for %s: %s', self.model_path, invalidate_exc)
                    try:
                        self._precision = 'fp32'
                        self._use_io_binding = False
                        fallback_providers: list[Any]
                        if requested_use_cuda:
                            fallback_cuda_options: dict[str, Any] = {
                                "device_id": 0,
                                "arena_extend_strategy": "kSameAsRequested",
                            }
                            if self._gpu_mem_limit:
                                fallback_cuda_options["gpu_mem_limit"] = self._gpu_mem_limit
                            fallback_providers = [
                                ("CUDAExecutionProvider", fallback_cuda_options),
                                "CPUExecutionProvider",
                            ]
                        else:
                            fallback_providers = ["CPUExecutionProvider"]
                        self.session = ort.InferenceSession(
                            str(self.model_path),
                            sess_options=session_options,
                            providers=fallback_providers,
                        )
                        self.input_name = self.session.get_inputs()[0].name
                        self.output_names = [output.name for output in self.session.get_outputs()]
                        self.active_providers = self.session.get_providers()
                        self._input_dtype = np.float32
                        self.session.run(
                            self.output_names,
                            {
                                self.input_name: np.zeros(
                                    (1, 3, self.input_height, self.input_width),
                                    dtype=np.float32,
                                )
                            },
                        )
                    except Exception as fallback_exc:
                        self.session = None
                        self.input_name = None
                        self.unavailable_reason = (
                            f'INT8 warm-up failed and FP32 fallback could not load: {fallback_exc}'
                        )
                else:
                    logger.debug('Detector warm-up inference skipped: %s', warm_exc)
        except Exception as exc:  # pragma: no cover - depends on runtime/model internals
            self.unavailable_reason = f"Failed to load ONNX model {self.model_path}: {exc}"

    @property
    def available(self) -> bool:
        return self.session is not None and self.input_name is not None

    @property
    def active_precision(self) -> str:
        """The precision the detector is *actually* running, not the request.

        Reflects the runtime reality after any fallback:
        - INT8 stays ``'int8'`` only when quantization succeeded (``__init__``
          rewrites ``self._precision`` to ``'fp32'`` when it can't quantize).
        - FP16 is reported only when the loaded model genuinely declares a
          ``float16`` input; an FP16 request that produced an FP32 export (a
          CPU host that dropped ``half=True``) correctly reads back as fp32.
        - Everything else is fp32.
        """
        if self._precision == 'int8':
            return 'int8'
        if self._input_dtype is not None and np is not None and np.dtype(self._input_dtype) == np.float16:
            return 'fp16'
        return 'fp32'

    def detect_frame(self, image: Any, confidence: float | None = None) -> list[dict[str, Any]]:
        """Run inference on a pre-decoded numpy BGR frame (H×W×3 uint8).

        Skipping the JPEG encode→decode round-trip that ``detect_image``
        performs saves ~30-90 ms per detection cycle on the hot path.
        """
        if not self.available:
            raise DetectorUnavailableError(self.unavailable_reason or "ONNX detector is not available")
        return self._run_inference(image, confidence)

    def detect_image(self, image_bytes: bytes, confidence: float | None = None) -> list[dict[str, Any]]:
        if not self.available:
            raise DetectorUnavailableError(self.unavailable_reason or "ONNX detector is not available")
        return self._run_inference(self._decode_image(image_bytes), confidence)

    def _run_inference(self, image: Any, confidence: float | None) -> list[dict[str, Any]]:
        effective_confidence = confidence if confidence is not None else self.confidence
        input_tensor, scale, pad_x, pad_y, original_width, original_height = self._preprocess(image)
        # Cap concurrent inferences so parallel callers (per-camera background
        # detection + live overlay) don't oversubscribe the CPU and slow each other.
        with self._inference_semaphore:
            # The preflight check in ``__init__`` guarantees this branch
            # only fires when CUDAExecutionProvider is the active provider,
            # so ``_run_inference_io_bound`` doesn't need to handle a
            # CPU-bound fallback at call time.
            if self._use_io_binding:
                outputs = self._run_inference_io_bound(input_tensor)
            else:
                outputs = self.session.run(self.output_names, {self.input_name: input_tensor})  # type: ignore[union-attr,index]
        return self._postprocess(outputs[0], scale, pad_x, pad_y, original_width, original_height, effective_confidence)

    def _run_inference_io_bound(self, input_tensor: np.ndarray) -> list[np.ndarray]:
        """Run inference via ORT's ``io_binding`` API (CUDA-only path).

        Avoids the host-side input copy + output round-trip that the
        default ``session.run`` performs when the inputs/outputs live on
        a CUDA EP. The API requires a fresh ``io_binding`` per call
        because it owns its bound-tensor lifetime and the per-camera
        detection threads share this singleton detector -- sharing a
        binding would lead to a cross-thread write race on the bound
        ``OrtValue`` objects.
        """
        from onnxruntime import OrtValue  # type: ignore[import-untyped]
        # ``io_binding()`` allocates a fresh binding; bound tensors are
        # released when ``io`` is garbage-collected (no explicit close).
        io = self.session.io_binding()  # type: ignore[union-attr]
        # Input: numpy on host -> OrtValue on CUDA, device_id 0.
        io.bind_ortvalue_input(
            self.input_name,  # type: ignore[arg-type]
            OrtValue.ortvalue_from_numpy(input_tensor, 'cuda', 0),
        )
        # Output: bind CUDA buffers in the order we declared on the
        # constructor; ORT fills them in-place.
        for name in self.output_names:
            io.bind_ortvalue_output(name, 'cuda', 0)
        self.session.run_with_iobinding(io)  # type: ignore[union-attr]
        # ``io.get_outputs()`` returns a list in the order outputs were
        # bound; we convert each OrtValue back to host numpy for the
        # existing post-process pipeline (which only knows numpy).
        return [ort_value.numpy() for ort_value in io.get_outputs()]     

    def _decode_image(self, image_bytes: bytes) -> np.ndarray:
        np = _require_numpy()
        try:
            import cv2
        except ImportError as exc:
            raise DetectorUnavailableError(
                "opencv-python-headless is not installed. Install requirements.txt or run pip install opencv-python-headless."
            ) from exc

        data = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Uploaded file is not a readable image")
        return image

    def _preprocess(self, image: np.ndarray) -> tuple[np.ndarray, float, float, float, int, int]:
        import cv2

        original_height, original_width = image.shape[:2]
        scale = min(self.input_width / original_width, self.input_height / original_height)
        resized_width = int(round(original_width * scale))
        resized_height = int(round(original_height * scale))
        resized = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)

        # np.empty() + fill(114) avoids the constant-broadcast pass that np.full()
        # does; functionally identical output. Each frame allocates a fresh local
        # buffer because live_monitor.py's per-camera daemon threads all call
        # detect_frame() on the SAME detector singleton - sharing the canvas on
        # `self` would create a cross-thread write race on the resize slice.
        canvas = np.empty((self.input_height, self.input_width, 3), dtype=np.uint8)
        canvas.fill(114)
        pad_x = (self.input_width - resized_width) / 2
        pad_y = (self.input_height - resized_height) / 2
        left = int(round(pad_x - 0.1))
        top = int(round(pad_y - 0.1))
        canvas[top : top + resized_height, left : left + resized_width] = resized

        # Transpose the uint8 (3 bytes per pixel permuted) BEFORE casting to float32
        # so we move the smaller dtype through NumPy's permutation loop. The
        # downstream /255.0 and astype(float32) operate on the same per-pixel
        # values either way - bit-equivalent output, ~4x less memory bandwidth on
        # the transpose pass.
        chw = np.transpose(canvas, (2, 0, 1))
        tensor = (chw.astype(np.float32) / 255.0)[None, ...]
        # Normalise in float32 (better precision than dividing in half) then
        # cast to the model's declared input dtype so FP16 models get a
        # tensor(float16) input instead of an ORT type-mismatch error.
        model_dtype = self._input_dtype or np.float32
        if model_dtype != np.float32:
            tensor = tensor.astype(model_dtype)
        return np.ascontiguousarray(tensor), scale, float(left), float(top), original_width, original_height

    @staticmethod
    def _looks_nms_free(output: np.ndarray) -> bool | None:
        """Infer the detection-head format from the raw output tensor shape.

        Returns ``True`` for an end-to-end / NMS-free head (``[num_det, 6]``
        with ``[x1, y1, x2, y2, conf, class]``), ``False`` for a grid head
        (``[4+nc, num_anchors]``), or ``None`` when the shape is too
        ambiguous to classify (caller falls back to the configured flag).

        Deriving the format from the actual output makes inference correct
        regardless of how the model was exported or named: for the shipped
        COCO-80 model set the two layouts never collide (a grid head's
        feature dim is 84, never 6; a grid head's anchor count is >=2100
        even at the smallest supported input, never <=300), so the shape is
        an authoritative discriminator that does not depend on the
        filename heuristic in ``_detect_model_type`` being right.
        """
        dims = [int(d) for d in np.asarray(output).shape if d != 1]
        if len(dims) == 1:
            # A single end-to-end detection can squeeze down to [6].
            return dims[0] == 6
        if len(dims) != 2:
            return None
        lo, hi = min(dims), max(dims)
        # NMS-free heads emit ``[N, 6]``: the 6-feature row is the LAST
        # axis. A grid head always emits ``[4+nc, anchors]`` with the anchor
        # count last, so requiring ``dims[-1] == 6`` (rather than merely
        # ``lo == 6``) prevents a tiny custom 2-class grid export -- e.g.
        # ``[1, 6, 567]`` at 96px input, where the anchor count dips below
        # 1000 -- from being misread as an end-to-end head and decoded as
        # garbage. ``hi < 1000`` stays as a second bound for the model's
        # NMS-free detection cap (YOLO26 emits at most 300).
        if dims[-1] == 6 and hi < 1000:
            return True
        return False

    def _postprocess(
        self,
        output: np.ndarray,
        scale: float,
        pad_x: float,
        pad_y: float,
        original_width: int,
        original_height: int,
        confidence: float | None = None,
    ) -> list[dict[str, Any]]:
        if confidence is None:
            confidence = self.confidence

        # Prefer the format implied by the real output shape over the
        # configured/auto-detected ``nms_free`` flag. This keeps detection
        # correct even if the flag (or the filename heuristic that seeds it)
        # disagrees with what the exported model actually produces.
        shape_nms_free = self._looks_nms_free(output)
        if shape_nms_free is None:
            use_nms_free = self._nms_free
        else:
            if shape_nms_free != self._nms_free:
                logger.warning(
                    'ONNX output shape %s indicates a %s head, but nms_free=%s was configured; '
                    'using the format implied by the output shape.',
                    tuple(int(d) for d in np.asarray(output).shape),
                    'NMS-free' if shape_nms_free else 'grid/NMS',
                    self._nms_free,
                )
            use_nms_free = shape_nms_free

        # YOLO26 NMS-free format: output shape [1, 300, 6] with [x1, y1, x2, y2, confidence, class_id]
        if use_nms_free:
            return self._postprocess_nms_free(output, scale, pad_x, pad_y, original_width, original_height, confidence)

        # Traditional YOLOv8/YOLO11 format: output shape [1, 4+nc, 8400]
        return self._postprocess_nms(output, scale, pad_x, pad_y, original_width, original_height, confidence)

    def _postprocess_nms_free(
        self,
        output: np.ndarray,
        scale: float,
        pad_x: float,
        pad_y: float,
        original_width: int,
        original_height: int,
        confidence: float | None = None,
    ) -> list[dict[str, Any]]:
        """Postprocess YOLO26 NMS-free output (shape: [1, 300, 6]).
        
        Output format: [x1, y1, x2, y2, confidence, class_id]
        Coordinates are in input-space (relative to model input size, e.g. 640x640)
        and need to be transformed to original image coordinates.
        """
        if confidence is None:
            confidence = self.confidence

        predictions = np.asarray(output)
        # Collapse a leading batch axis of 1 (``[1, 300, 6] -> [300, 6]``)
        # while tolerating the degenerate single-detection case
        # (``[1, 1, 6] -> [1, 6]``) that a plain ``np.squeeze`` would flatten
        # to 1-D and then reject.
        if predictions.ndim == 3 and predictions.shape[0] == 1:
            predictions = predictions[0]
        predictions = predictions.reshape(-1, predictions.shape[-1])
        if predictions.shape[1] != 6:
            raise ValueError(
                f"Unexpected NMS-free output shape: {tuple(np.asarray(output).shape)}. Expected [1, N, 6]."
            )        # Slice into structured arrays. ``class_ids`` is cast to int32 once so
        # the per-box label lookup below doesn't repeatedly coerce floats.
        boxes = predictions[:, :4]  # [x1, y1, x2, y2] in input-space
        scores = predictions[:, 4]  # confidence
        class_ids = predictions[:, 5].astype(np.int32)  # class_id

        # Vectorised conf-threshold filter replaces the old Python loop over
        # all 300 detections; ``np.any`` short-circuits cleanly on empty masks.
        conf_mask = scores >= confidence
        if not np.any(conf_mask):
            return []

        filtered_boxes = boxes[conf_mask]
        filtered_scores = scores[conf_mask]
        filtered_class_ids = class_ids[conf_mask]        # YOLO26 is NMS-free at train time, so when ``confidence_only_nms``
        # is set the model head's one-to-one label assignment plus the
        # confidence threshold already give us a clean detection list --
        # skipping the class-aware NMS saves the per-frame Python sort +
        # IoU matrix cost. Off by default so existing recordings keep
        # their dedupe behavior; flip on after A/B on a labeled set.
        if filtered_boxes.shape[0] > 0 and not self._confidence_only_nms:
            keep = non_max_suppression(
                filtered_boxes, filtered_scores, filtered_class_ids, self.iou_threshold
            )
            filtered_boxes = filtered_boxes[keep]
            filtered_scores = filtered_scores[keep]
            filtered_class_ids = filtered_class_ids[keep]     

        if filtered_boxes.shape[0] == 0:
            return []

        # Vectorised coord transform: subtract letterbox padding, undo the
        # scale factor, then clip to the original frame. Equivalent to
        # ``(x - pad) / scale`` followed by scalar ``max(0, min(w, …))`` but
        # runs entirely in NumPy and avoids Python-list construction per box
        # - this is the per-frame bottleneck for the YOLO26 hot path.
        ow = float(original_width)
        oh = float(original_height)
        orig_x1 = np.clip((filtered_boxes[:, 0] - pad_x) / scale, 0.0, ow)
        orig_y1 = np.clip((filtered_boxes[:, 1] - pad_y) / scale, 0.0, oh)
        orig_x2 = np.clip((filtered_boxes[:, 2] - pad_x) / scale, 0.0, ow)
        orig_y2 = np.clip((filtered_boxes[:, 3] - pad_y) / scale, 0.0, oh)

        valid = (orig_x2 > orig_x1) & (orig_y2 > orig_y1)
        if not np.any(valid):
            return []

        # Round normalised coords in a single NumPy pass (``round`` and
        # ``np.round`` both use round-half-to-even, so this matches the
        # prior scalar ``round(..., 4)`` call bit-for-bit within the
        # ``pytest.approx(abs=0.01)`` tolerance in test_detector_postprocess.py).
        safe_ow = ow if ow > 0 else 1.0
        safe_oh = oh if oh > 0 else 1.0
        inv_ow = 1.0 / safe_ow
        inv_oh = 1.0 / safe_oh
        v_scores = filtered_scores[valid]
        order = np.argsort(-v_scores, kind='stable')
        s_x1 = orig_x1[valid][order]
        s_y1 = orig_y1[valid][order]
        s_x2 = orig_x2[valid][order]
        s_y2 = orig_y2[valid][order]
        s_classes = filtered_class_ids[valid][order]
        s_scores = v_scores[order]
        bx = np.round(s_x1 * inv_ow, 4)
        by = np.round(s_y1 * inv_oh, 4)
        bw = np.round((s_x2 - s_x1) * inv_ow, 4)
        bh = np.round((s_y2 - s_y1) * inv_oh, 4)

        n = s_x1.shape[0]
        labels = self.labels
        n_labels = len(labels)
        detections: list[Detection] = [
            Detection(
                label=labels[int(s_classes[i])] if 0 <= int(s_classes[i]) < n_labels else f"class_{int(s_classes[i])}",
                confidence=float(s_scores[i]),
                box={
                    "x": float(bx[i]),
                    "y": float(by[i]),
                    "width": float(bw[i]),
                    "height": float(bh[i]),
                },
            )
            for i in range(n)
        ]
        return [detection.to_dict() for detection in detections]     

    def _postprocess_nms(
        self,
        output: np.ndarray,
        scale: float,
        pad_x: float,
        pad_y: float,
        original_width: int,
        original_height: int,
        confidence: float | None = None,
    ) -> list[dict[str, Any]]:
        """Postprocess YOLOv8/YOLO11 output (shape: [1, 4+nc, 8400]) with NMS."""
        if confidence is None:
            confidence = self.confidence
        predictions = np.squeeze(output)
        if predictions.ndim != 2:
            raise ValueError(f"Unsupported YOLO output shape: {output.shape}")
        if predictions.shape[0] < predictions.shape[1]:
            predictions = predictions.T

        n_cols = predictions.shape[1]
        if n_cols < 5:
            return []

        # Detect model format: YOLOv5 has an explicit objectness column at index 4
        # (total = 4 bbox + 1 obj + N classes = N+5 cols); YOLOv8 goes straight
        # from bbox to class scores (total = 4 + N = N+4 cols).  Use exact equality
        # so an extra trailing column from some export tools doesn't trigger a
        # false-positive.  Requires a labels file; without one we can't distinguish
        # formats and default to YOLOv8 (no objectness) which is the safer guess.
        has_objectness = len(self.labels) > 0 and n_cols == len(self.labels) + 5

        if has_objectness:
            objectness = predictions[:, 4]
            class_scores = predictions[:, 5:]
        else:
            objectness = None
            class_scores = predictions[:, 4:]

        class_ids = np.argmax(class_scores, axis=1)
        raw_scores = class_scores[np.arange(len(class_ids)), class_ids]
        scores = raw_scores * objectness if objectness is not None else raw_scores

        conf_mask = scores >= confidence
        if not np.any(conf_mask):
            return []

        pred_f = predictions[conf_mask]
        scores_f = scores[conf_mask].astype(np.float32)
        class_ids_f = class_ids[conf_mask].astype(np.int32)

        cx = pred_f[:, 0]
        cy = pred_f[:, 1]
        w = pred_f[:, 2]
        h = pred_f[:, 3]
        x1 = np.clip((cx - w / 2 - pad_x) / scale, 0.0, float(original_width))
        y1 = np.clip((cy - h / 2 - pad_y) / scale, 0.0, float(original_height))
        x2 = np.clip((cx + w / 2 - pad_x) / scale, 0.0, float(original_width))
        y2 = np.clip((cy + h / 2 - pad_y) / scale, 0.0, float(original_height))

        valid = (x2 > x1) & (y2 > y1)
        if not np.any(valid):
            return []

        box_array = np.stack([x1[valid], y1[valid], x2[valid], y2[valid]], axis=1).astype(np.float32)
        score_array = scores_f[valid]
        class_array = class_ids_f[valid]

        keep = non_max_suppression(box_array, score_array, class_array, self.iou_threshold)

        detections: list[Detection] = []
        for index in sorted(keep, key=lambda idx: float(score_array[idx]), reverse=True):
            x1v, y1v, x2v, y2v = box_array[index]
            class_id = int(class_array[index])
            label = self.labels[class_id] if 0 <= class_id < len(self.labels) else f"class_{class_id}"
            box_x = round(max(0.0, min(1.0, float(x1v) / original_width)), 4)
            box_y = round(max(0.0, min(1.0, float(y1v) / original_height)), 4)
            detections.append(
                Detection(
                    label=label,
                    confidence=float(score_array[index]),
                    box={
                        "x": box_x,
                        "y": box_y,
                        "width": round(max(0.0, min(1.0 - box_x, float(x2v - x1v) / original_width)), 4),
                        "height": round(max(0.0, min(1.0 - box_y, float(y2v - y1v) / original_height)), 4),
                    },
                )
            )
        return [detection.to_dict() for detection in detections]


def _int8_precision_supported_for_detector(nms_free: bool) -> bool:
    """Return whether runtime INT8 PTQ is safe for this detector head.

    NMS-free YOLO26 exports use a sensitive end-to-end head whose confidence
    distribution is not preserved reliably by the current static QDQ PTQ
    pipeline. They must remain FP32 rather than appearing healthy while
    silently returning zero detections.
    """
    return not bool(nms_free)


def _detect_model_type(model_path: str) -> bool:
    """Detect if a model uses NMS-free format based on filename.
    
    YOLO26 models use NMS-free end-to-end detection.
    """
    filename = Path(model_path).name.lower()
    # YOLO26 models follow the pattern: yolo26{n,s,m,l,x}.onnx
    return filename.startswith('yolo26')


def create_detector(ai_config: dict[str, Any]) -> OnnxYoloDetector:
    backend = str(ai_config.get("backend", "onnx")).lower()
    if backend != "onnx":
        raise ValueError(f"Unsupported ai.backend '{backend}'. Expected 'onnx'.")
    def _optional_int(key: str) -> int | None:
        value = ai_config.get(key)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    model_path = ai_config.get("model_path", "models/model.onnx")
    
    # Auto-detect NMS-free mode for YOLO26 models
    nms_free = ai_config.get("nms_free")
    if nms_free is None:
        nms_free = _detect_model_type(model_path)

    return OnnxYoloDetector(
        model_path=model_path,
        labels_path=ai_config.get("labels_path", "models/coco.names"),
        confidence=float(ai_config.get("confidence", 0.45)),
        iou_threshold=float(ai_config.get("iou_threshold", 0.45)),
        input_size=ai_config.get("input_size", 640),
        categories=ai_config.get("categories", []),
        num_threads=_optional_int("inference_threads"),
        max_concurrency=_optional_int("max_concurrent_inferences"),
        device=str(ai_config.get("device", "auto")),
        gpu_mem_limit=_optional_int("gpu_mem_limit"),
        nms_free=bool(nms_free),
        execution_mode=str(ai_config.get("execution_mode", "parallel") or "parallel").lower(),
        confidence_only_nms=_resolve_confidence_only_nms(ai_config.get("confidence_only_nms"), nms_free),
        precision=str(ai_config.get("precision", "fp32") or "fp32").strip().lower(),
        use_io_binding=_coerce_bool(ai_config.get("use_io_binding", False)),
    )


def _coerce_bool(value: Any) -> bool:
    """Tri-state tolerant bool coercion for settings form values.

    Mirrors the str -> bool pattern used for ``enabled`` in
    ``validate_ai_settings`` so callers can pass ``'yes'`` / ``'on'`` /
    ``'true'`` from HTML forms AND ``True`` / ``False`` from the API.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def _resolve_confidence_only_nms(value: Any, nms_free: bool) -> bool:
    """Resolve the tri-state ``confidence_only_nms`` setting to a bool.

    NMS-free heads (YOLO26) already perform one-to-one label assignment in the
    model, so the extra class-aware NMS pass is redundant work that can also
    wrongly suppress genuinely overlapping objects. The setting is tri-state:

    - ``'auto'`` / ``None`` (default): follow the model -- skip the NMS pass
      for NMS-free heads, keep it for grid heads (YOLOv8/YOLO11).
    - ``'on'`` / truthy: always skip the NMS pass (confidence threshold only).
    - ``'off'`` / falsy: always run the class-aware NMS dedupe.

    Legacy persisted bools are honoured (``True`` -> skip, ``False`` -> run).
    """
    if value is None:
        return bool(nms_free)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {'on', '1', 'true', 'yes'}:
        return True
    if text in {'off', '0', 'false', 'no'}:
        return False
    return bool(nms_free)  # 'auto' or any unknown value follows the model
