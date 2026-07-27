from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def parse_input_size(value: Any) -> tuple[int, int]:
    if isinstance(value, int):
        return value, value
    if isinstance(value, str):
        if "x" in value.lower():
            width, height = value.lower().split("x", 1)
            return int(width), int(height)
        size = int(value)
        return size, size
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    raise ValueError("ai.input_size must be an integer, WIDTHxHEIGHT string, or two-item list")


def load_labels(labels_path: str | Path | None, fallback: list[str] | None = None) -> list[str]:
    if labels_path:
        path = Path(labels_path)
        if path.exists():
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
        input_size: int | str | list[int] | tuple[int, int] = 640,
        confidence: float = 0.45,
        iou_threshold: float = 0.45,
        categories: list[str] | None = None,
        num_threads: int | None = None,
        max_concurrency: int | None = None,
        device: str = "auto",
        gpu_mem_limit: int | None = None,
        nms_free: bool = False,
    ) -> None:
        self.model_path = Path(model_path)
        self.labels = load_labels(labels_path, categories)
        self.input_width, self.input_height = parse_input_size(input_size)
        self.confidence = float(confidence)
        self.iou_threshold = float(iou_threshold)
        self.session: Any | None = None
        self.input_name: str | None = None
        self.output_names: list[str] = []
        self._gpu_mem_limit = gpu_mem_limit
        self.unavailable_reason: str | None = None
        self._device = device.lower() if device else "auto"
        self._nms_free = nms_free

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

        try:
            available_providers = ort.get_available_providers()
            use_cuda = (
                self._device == "cuda"
                or (self._device == "auto" and "CUDAExecutionProvider" in available_providers)
            )
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
            self.session = ort.InferenceSession(str(self.model_path), sess_options=session_options, providers=providers)
            self.input_name = self.session.get_inputs()[0].name
            self.output_names = [output.name for output in self.session.get_outputs()]
            self.active_providers = self.session.get_providers()
        except Exception as exc:  # pragma: no cover - depends on runtime/model internals
            self.unavailable_reason = f"Failed to load ONNX model {self.model_path}: {exc}"

    @property
    def available(self) -> bool:
        return self.session is not None and self.input_name is not None

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
            outputs = self.session.run(self.output_names, {self.input_name: input_tensor})  # type: ignore[union-attr,index]
        return self._postprocess(outputs[0], scale, pad_x, pad_y, original_width, original_height, effective_confidence)

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

        canvas = np.full((self.input_height, self.input_width, 3), 114, dtype=np.uint8)
        pad_x = (self.input_width - resized_width) / 2
        pad_y = (self.input_height - resized_height) / 2
        left = int(round(pad_x - 0.1))
        top = int(round(pad_y - 0.1))
        canvas[top : top + resized_height, left : left + resized_width] = resized

        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        tensor = rgb.astype(np.float32) / 255.0
        tensor = np.transpose(tensor, (2, 0, 1))[None, ...]
        return np.ascontiguousarray(tensor), scale, float(left), float(top), original_width, original_height

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
        
        # YOLO26 NMS-free format: output shape [1, 300, 6] with [x1, y1, x2, y2, confidence, class_id]
        if self._nms_free:
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
            
        predictions = np.squeeze(output)
        if predictions.ndim != 2 or predictions.shape[1] != 6:
            raise ValueError(f"Unexpected YOLO26 output shape: {output.shape}. Expected [1, 300, 6].")
        
        # Extract components
        boxes = predictions[:, :4]  # [x1, y1, x2, y2] in input-space
        scores = predictions[:, 4]  # confidence
        class_ids = predictions[:, 5].astype(np.int32)  # class_id
        
        # Filter by confidence
        conf_mask = scores >= confidence
        if not np.any(conf_mask):
            return []
        
        filtered_boxes = boxes[conf_mask]
        filtered_scores = scores[conf_mask]
        filtered_class_ids = class_ids[conf_mask]
        
        # Apply IoU-based NMS to remove overlapping detections
        # Even though YOLO26 is "NMS-free", it can still produce
        # duplicate detections for the same object
        if len(filtered_boxes) > 0:
            keep = non_max_suppression(filtered_boxes, filtered_scores, filtered_class_ids, self.iou_threshold)
            filtered_boxes = filtered_boxes[keep]
            filtered_scores = filtered_scores[keep]
            filtered_class_ids = filtered_class_ids[keep]
        
        # Build detections with coordinate transformation from input-space to original image
        detections: list[Detection] = []
        for i in range(len(filtered_boxes)):
            x1, y1, x2, y2 = filtered_boxes[i]
            class_id = int(filtered_class_ids[i])
            label = self.labels[class_id] if 0 <= class_id < len(self.labels) else f"class_{class_id}"
            
            # Transform from input-space to original image coordinates:
            # 1. Remove padding (subtract pad_x, pad_y)
            # 2. Divide by scale to get original coordinates
            orig_x1 = float((x1 - pad_x) / scale)
            orig_y1 = float((y1 - pad_y) / scale)
            orig_x2 = float((x2 - pad_x) / scale)
            orig_y2 = float((y2 - pad_y) / scale)
            
            # Clip to image bounds
            orig_x1 = max(0.0, min(float(original_width), orig_x1))
            orig_y1 = max(0.0, min(float(original_height), orig_y1))
            orig_x2 = max(0.0, min(float(original_width), orig_x2))
            orig_y2 = max(0.0, min(float(original_height), orig_y2))
            
            # Skip invalid boxes
            if orig_x2 <= orig_x1 or orig_y2 <= orig_y1:
                continue
            
            # Convert to normalized coordinates (x, y, width, height)
            box_x = round(orig_x1 / original_width, 4)
            box_y = round(orig_y1 / original_height, 4)
            box_w = round((orig_x2 - orig_x1) / original_width, 4)
            box_h = round((orig_y2 - orig_y1) / original_height, 4)
            
            detections.append(
                Detection(
                    label=label,
                    confidence=float(filtered_scores[i]),
                    box={
                        "x": box_x,
                        "y": box_y,
                        "width": box_w,
                        "height": box_h,
                    },
                )
            )
        
        # Sort by confidence descending
        detections.sort(key=lambda d: d.confidence, reverse=True)
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
        input_size=ai_config.get("input_size", 640),
        confidence=float(ai_config.get("confidence", 0.45)),
        iou_threshold=float(ai_config.get("iou_threshold", 0.45)),
        categories=ai_config.get("categories", []),
        num_threads=_optional_int("inference_threads"),
        max_concurrency=_optional_int("max_concurrent_inferences"),
        device=str(ai_config.get("device", "auto")),
        gpu_mem_limit=_optional_int("gpu_mem_limit"),
        nms_free=bool(nms_free),
    )
