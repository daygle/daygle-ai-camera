from __future__ import annotations

import hashlib
import importlib
import re
from typing import Any


VEHICLE_LABELS = {"car", "truck", "bus", "motorcycle"}


class AnprUnavailableError(RuntimeError):
    pass


class AnprPipeline:
    """Modular ANPR pipeline: vehicle detections in, plate OCR results out."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.enabled = bool(config.get("enabled", True))
        self.backend = str(config.get("backend", "paddleocr")).lower()
        self.min_confidence = float(config.get("min_confidence", 0.75))
        self.vehicle_labels = {str(label).lower() for label in config.get("vehicle_labels", VEHICLE_LABELS)}
        self.ocr = create_ocr_backend(self.backend)

    def process_event(
        self,
        *,
        event_id: int,
        detections: list[dict[str, Any]],
        image_path: str | None,
        storage: Any,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        vehicle_detections = [detection for detection in detections if str(detection.get("label", "")).lower() in self.vehicle_labels]
        results: list[dict[str, Any]] = []
        for index, detection in enumerate(vehicle_detections):
            crop_path = storage.save_plate_crop(event_id=event_id, source_path=image_path, detection=detection, index=index)
            plate_number, confidence = self.ocr.read_plate(crop_path, event_id=event_id, detection=detection, index=index)
            plate_number = normalize_plate(plate_number)
            if not plate_number or confidence < self.min_confidence:
                continue
            results.append(
                {
                    "plate_number": plate_number,
                    "confidence": confidence,
                    "image_path": crop_path,
                    "vehicle_label": detection.get("label"),
                }
            )
        return results


class MockOcrBackend:
    def read_plate(self, image_path: str, *, event_id: int, detection: dict[str, Any], index: int) -> tuple[str, float]:
        seed = f"{event_id}:{index}:{detection.get('label')}:{image_path}"
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest().upper()
        return f"{digest[:3]}{int(digest[3:6], 16) % 1000:03d}", 0.97


class PaddleOcrBackend:
    def __init__(self) -> None:
        try:
            module = importlib.import_module("paddleocr")
            self.reader = module.PaddleOCR(use_angle_cls=True, lang="en")
        except Exception as exc:
            raise AnprUnavailableError(f"PaddleOCR is not available: {exc}") from exc

    def read_plate(self, image_path: str, *, event_id: int, detection: dict[str, Any], index: int) -> tuple[str, float]:
        result = self.reader.ocr(image_path, cls=True)
        candidates: list[tuple[str, float]] = []
        for group in result or []:
            for item in group or []:
                text, confidence = item[1]
                candidates.append((str(text), float(confidence)))
        return max(candidates, key=lambda item: item[1]) if candidates else ("", 0.0)


class EasyOcrBackend:
    def __init__(self) -> None:
        try:
            module = importlib.import_module("easyocr")
            self.reader = module.Reader(["en"], gpu=False)
        except Exception as exc:
            raise AnprUnavailableError(f"EasyOCR is not available: {exc}") from exc

    def read_plate(self, image_path: str, *, event_id: int, detection: dict[str, Any], index: int) -> tuple[str, float]:
        result = self.reader.readtext(image_path)
        candidates = [(str(item[1]), float(item[2])) for item in result or []]
        return max(candidates, key=lambda item: item[1]) if candidates else ("", 0.0)


def create_ocr_backend(backend: str) -> Any:
    backend = backend.lower()
    if backend == "paddleocr":
        try:
            return PaddleOcrBackend()
        except AnprUnavailableError:
            return MockOcrBackend()
    if backend == "easyocr":
        try:
            return EasyOcrBackend()
        except AnprUnavailableError:
            return MockOcrBackend()
    return MockOcrBackend()


def normalize_plate(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def plate_matches(pattern: str | None, plate_number: str) -> bool:
    pattern = normalize_plate(pattern or "")
    plate_number = normalize_plate(plate_number)
    if not pattern:
        return False
    return pattern == plate_number
