from __future__ import annotations

import importlib
import logging
import re
from typing import Any


VEHICLE_LABELS = {"car", "truck", "bus", "motorcycle"}
logger = logging.getLogger(__name__)


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
        self.unavailable_reason: str | None = None
        self.ocr = None
        if not self.enabled:
            return
        try:
            self.ocr = create_ocr_backend(self.backend)
        except AnprUnavailableError as exc:
            # ANPR is optional. Keep the app running and report why OCR is disabled.
            self.unavailable_reason = str(exc)
            logger.info("ANPR disabled: %s", self.unavailable_reason)

    def process_event(
        self,
        *,
        event_id: int,
        detections: list[dict[str, Any]],
        image_path: str | None,
        storage: Any,
    ) -> list[dict[str, Any]]:
        if not self.enabled or self.ocr is None:
            return []
        vehicle_detections = [detection for detection in detections if str(detection.get("label", "")).lower() in self.vehicle_labels]
        results: list[dict[str, Any]] = []
        for index, detection in enumerate(vehicle_detections):
            crop_path = storage.save_plate_crop(event_id=event_id, source_path=image_path, detection=detection, index=index)
            if not crop_path:
                logger.debug("Skipping ANPR for event %s detection %s: no image available", event_id, index)
                continue
            plate_number, confidence = self.ocr.read_plate(crop_path, event_id=event_id, detection=detection, index=index)
            plate_number = normalize_plate(plate_number)
            if not plate_number:
                logger.debug("No plate text found for event %s detection %s", event_id, index)
                continue
            if confidence < self.min_confidence:
                logger.debug("Plate %r confidence %.2f below threshold %.2f for event %s", plate_number, confidence, self.min_confidence, event_id)
                continue
            logger.info("ANPR event %s: plate=%r confidence=%.2f", event_id, plate_number, confidence)
            results.append(
                {
                    "plate_number": plate_number,
                    "confidence": confidence,
                    "image_path": crop_path,
                    "vehicle_label": detection.get("label"),
                }
            )
        return results


class EasyOcrBackend:
    def __init__(self) -> None:
        try:
            module = importlib.import_module("easyocr")
            self.reader = module.Reader(["en"], gpu=False)
        except Exception as exc:
            raise AnprUnavailableError(f"EasyOCR is not available: {exc}") from exc

    def read_plate(self, image_path: str, *, event_id: int, detection: dict[str, Any], index: int) -> tuple[str, float]:
        try:
            result = self.reader.readtext(image_path)
        except Exception as exc:
            logger.warning("EasyOCR failed for %r: %s", image_path, exc)
            return ("", 0.0)
        candidates: list[tuple[str, float]] = []
        for item in result or []:
            try:
                candidates.append((str(item[1]), float(item[2])))
            except (TypeError, ValueError, IndexError):
                continue
        return max(candidates, key=lambda item: item[1]) if candidates else ("", 0.0)


def create_ocr_backend(backend: str) -> Any:
    backend = backend.lower()
    if backend == "easyocr":
        return EasyOcrBackend()
    raise AnprUnavailableError(f"Unsupported ANPR backend: {backend}")


def normalize_plate(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def plate_matches(pattern: str | None, plate_number: str) -> bool:
    pattern = normalize_plate(pattern or "")
    plate_number = normalize_plate(plate_number)
    if not pattern:
        return False
    return pattern == plate_number
