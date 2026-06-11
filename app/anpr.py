from __future__ import annotations

import importlib
import importlib.util
import logging
import multiprocessing
import queue
import re
import threading
import time
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

    def close(self) -> None:
        close = getattr(self.ocr, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("Error closing ANPR OCR backend", exc_info=True)


def _run_easyocr_worker(request_queue: Any, response_queue: Any, languages: list[str]) -> None:
    """Entry point for the isolated EasyOCR worker process.

    EasyOCR runs PyTorch on the CPU. On hardware that lacks the SIMD
    instructions the prebuilt torch wheel expects, inference dies with a fatal
    SIGILL (illegal instruction) that Python cannot catch. Running it here, in a
    separate process, means such a crash takes down only this worker, not the
    parent FastAPI service.
    """
    try:
        easyocr = importlib.import_module("easyocr")
        reader = easyocr.Reader(list(languages), gpu=False)
    except BaseException as exc:  # noqa: BLE001 - report any startup failure to the parent
        try:
            response_queue.put(("__startup_error__", repr(exc)))
        except Exception:
            pass
        return
    while True:
        try:
            job = request_queue.get()
        except (EOFError, OSError):
            break
        if job is None:
            break
        job_id, image_path = job
        candidates: list[tuple[str, float]] = []
        try:
            # workers=0 keeps EasyOCR's DataLoader in-process; a daemonic worker
            # process is not allowed to spawn its own children.
            result = reader.readtext(image_path, workers=0)
            for item in result or []:
                try:
                    candidates.append((str(item[1]), float(item[2])))
                except (TypeError, ValueError, IndexError):
                    continue
        except Exception as exc:  # noqa: BLE001 - one bad image must not kill the worker
            logger.warning("EasyOCR worker failed to read %r: %s", image_path, exc)
        try:
            response_queue.put((job_id, candidates))
        except Exception:
            break


class EasyOcrBackend:
    """EasyOCR backend that runs inference in an isolated child process.

    A native crash in the worker (e.g. SIGILL) is observed by the parent as a
    dead process; ANPR is then disabled at runtime and the service keeps
    serving instead of being killed and restart-looped by systemd.
    """

    def __init__(self, languages: tuple[str, ...] = ("en",), call_timeout: float = 120.0) -> None:
        if importlib.util.find_spec("easyocr") is None:
            raise AnprUnavailableError("EasyOCR is not available: module 'easyocr' is not installed")
        self._languages = list(languages)
        self._call_timeout = float(call_timeout)
        self._lock = threading.Lock()
        self._job_counter = 0
        self._failed = False
        self.unavailable_reason: str | None = None
        # "spawn" gives the worker a clean interpreter that imports torch itself,
        # rather than inheriting the parent's already-loaded state via fork.
        self._ctx = multiprocessing.get_context("spawn")
        self._request_queue: Any = self._ctx.Queue()
        self._response_queue: Any = self._ctx.Queue()
        self._process = self._ctx.Process(
            target=_run_easyocr_worker,
            args=(self._request_queue, self._response_queue, self._languages),
            name="daygle-easyocr",
            daemon=True,
        )
        self._process.start()

    @property
    def available(self) -> bool:
        return not self._failed and self._process is not None and self._process.is_alive()

    def read_plate(self, image_path: str, *, event_id: int, detection: dict[str, Any], index: int) -> tuple[str, float]:
        with self._lock:
            if self._failed:
                return ("", 0.0)
            if self._process is None or not self._process.is_alive():
                self._mark_failed(f"OCR worker is not running (exitcode={getattr(self._process, 'exitcode', None)})")
                return ("", 0.0)
            self._job_counter += 1
            job_id = self._job_counter
            try:
                self._request_queue.put((job_id, image_path))
            except Exception as exc:  # noqa: BLE001
                self._mark_failed(f"failed to dispatch OCR job: {exc}")
                return ("", 0.0)
            deadline = time.monotonic() + self._call_timeout
            while True:
                if not self._process.is_alive():
                    self._mark_failed(f"OCR worker crashed during inference (exitcode={self._process.exitcode})")
                    return ("", 0.0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._mark_failed("OCR worker timed out")
                    return ("", 0.0)
                try:
                    message = self._response_queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                tag = message[0]
                if tag == "__startup_error__":
                    self._mark_failed(f"OCR worker failed to start: {message[1]}")
                    return ("", 0.0)
                if tag == job_id:
                    candidates = message[1]
                    return max(candidates, key=lambda item: item[1]) if candidates else ("", 0.0)
                # Stale reply from an earlier job; ignore and keep waiting.

    def _mark_failed(self, reason: str) -> None:
        if not self._failed:
            logger.warning("ANPR OCR disabled at runtime: %s", reason)
        self._failed = True
        self.unavailable_reason = reason
        self._terminate_worker()

    def _terminate_worker(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    def close(self) -> None:
        with self._lock:
            process = self._process
            if process is None:
                return
            try:
                if process.is_alive():
                    try:
                        self._request_queue.put(None)
                    except Exception:
                        pass
                    process.join(timeout=5)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


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
