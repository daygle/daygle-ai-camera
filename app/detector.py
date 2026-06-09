from __future__ import annotations

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
    """YOLOv8 ONNX detector backed by ONNX Runtime."""

    backend = "onnx"

    def __init__(
        self,
        model_path: str | Path,
        labels_path: str | Path | None = None,
        input_size: int | str | list[int] | tuple[int, int] = 640,
        confidence: float = 0.45,
        iou_threshold: float = 0.45,
        categories: list[str] | None = None,
    ) -> None:
        self.model_path = Path(model_path)
        self.labels = load_labels(labels_path, categories)
        self.input_width, self.input_height = parse_input_size(input_size)
        self.confidence = float(confidence)
        self.iou_threshold = float(iou_threshold)
        self.session: Any | None = None
        self.input_name: str | None = None
        self.output_names: list[str] = []
        self.unavailable_reason: str | None = None

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
            providers = ["CPUExecutionProvider"]
            session_options = ort.SessionOptions()
            session_options.intra_op_num_threads = 1
            session_options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(str(self.model_path), sess_options=session_options, providers=providers)
            self.input_name = self.session.get_inputs()[0].name
            self.output_names = [output.name for output in self.session.get_outputs()]
        except Exception as exc:  # pragma: no cover - depends on runtime/model internals
            self.unavailable_reason = f"Failed to load ONNX model {self.model_path}: {exc}"

    @property
    def available(self) -> bool:
        return self.session is not None and self.input_name is not None

    def detect_image(self, image_bytes: bytes, confidence: float | None = None) -> list[dict[str, Any]]:
        if not self.available:
            raise DetectorUnavailableError(self.unavailable_reason or "ONNX detector is not available")

        effective_confidence = confidence if confidence is not None else self.confidence
        image = self._decode_image(image_bytes)
        input_tensor, scale, pad_x, pad_y, original_width, original_height = self._preprocess(image)
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
        predictions = np.squeeze(output)
        if predictions.ndim != 2:
            raise ValueError(f"Unsupported YOLO output shape: {output.shape}")
        if predictions.shape[0] < predictions.shape[1]:
            predictions = predictions.T

        boxes: list[list[float]] = []
        scores: list[float] = []
        classes: list[int] = []

        for row in predictions:
            if row.shape[0] < 5:
                continue

            class_scores = row[4:]
            objectness = 1.0
            if len(self.labels) > 0 and row.shape[0] >= len(self.labels) + 5:
                objectness = float(row[4])
                class_scores = row[5:]

            class_id = int(np.argmax(class_scores))
            score = float(class_scores[class_id]) * objectness
            if score < confidence:
                continue

            cx, cy, width, height = map(float, row[:4])
            x1 = (cx - width / 2 - pad_x) / scale
            y1 = (cy - height / 2 - pad_y) / scale
            x2 = (cx + width / 2 - pad_x) / scale
            y2 = (cy + height / 2 - pad_y) / scale
            x1 = min(max(x1, 0.0), float(original_width))
            y1 = min(max(y1, 0.0), float(original_height))
            x2 = min(max(x2, 0.0), float(original_width))
            y2 = min(max(y2, 0.0), float(original_height))
            if x2 <= x1 or y2 <= y1:
                continue
            boxes.append([x1, y1, x2, y2])
            scores.append(score)
            classes.append(class_id)

        if not boxes:
            return []

        box_array = np.array(boxes, dtype=np.float32)
        score_array = np.array(scores, dtype=np.float32)
        class_array = np.array(classes, dtype=np.int32)
        keep = non_max_suppression(box_array, score_array, class_array, self.iou_threshold)

        detections: list[Detection] = []
        for index in sorted(keep, key=lambda idx: float(score_array[idx]), reverse=True):
            x1, y1, x2, y2 = box_array[index]
            class_id = int(class_array[index])
            label = self.labels[class_id] if 0 <= class_id < len(self.labels) else f"class_{class_id}"
            detections.append(
                Detection(
                    label=label,
                    confidence=float(score_array[index]),
                    box={
                        "x": round(float(x1) / original_width, 4),
                        "y": round(float(y1) / original_height, 4),
                        "width": round(float(x2 - x1) / original_width, 4),
                        "height": round(float(y2 - y1) / original_height, 4),
                    },
                )
            )
        return [detection.to_dict() for detection in detections]


def create_detector(ai_config: dict[str, Any]) -> OnnxYoloDetector:
    backend = str(ai_config.get("backend", "onnx")).lower()
    if backend != "onnx":
        raise ValueError(f"Unsupported ai.backend '{backend}'. Expected 'onnx'.")
    return OnnxYoloDetector(
        model_path=ai_config.get("model_path", "models/model.onnx"),
        labels_path=ai_config.get("labels_path", "models/coco.names"),
        input_size=ai_config.get("input_size", 640),
        confidence=float(ai_config.get("confidence", 0.45)),
        iou_threshold=float(ai_config.get("iou_threshold", 0.45)),
        categories=ai_config.get("categories", []),
    )
