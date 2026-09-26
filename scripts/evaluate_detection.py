#!/usr/bin/env python3
"""Replay real footage through the motion + object detectors and report stats.

This is a tuning/evaluation aid: point it at a saved recording (or a folder of
frames) and it runs the SAME motion gate (:func:`app.detection_state.detect_frame_motion`)
and, when a model is given, the SAME ONNX object detector the live monitor uses,
then prints how they behaved. Use it to choose motion thresholds and confidence
floors with evidence from your own cameras instead of the generic defaults.

Examples
--------
Motion-only sweep of a clip::

    python scripts/evaluate_detection.py --input clip.mp4

Full motion + object evaluation with a model, night shadow handling on auto::

    python scripts/evaluate_detection.py --input clip.mp4 \
        --model models/yolo11n.onnx --labels models/coco.names \
        --shadow auto --confidence 0.35 --annotate /tmp/annotated

Quality benchmarking uses ``--ground-truth annotations.json`` to calculate
class-aware precision, recall, F1, AP@0.5, and mAP@0.5:0.95 from labeled frames.
``--post-pipeline`` replays the production decision path after inference (label
rule floors, tracking, camera/zone scope, N-of-M confirmation, and the post-zone
action decision), so a tuning study measures what actually alerts rather than
what the model alone produced. ``--camera-config`` supplies the real zone policy
and ``--scenarios`` reports the always-on and motion-gated operating modes
separately. The JSON format and workflow are documented in
``docs/detection-benchmarking.md``.

Nothing here writes to the app database or config; it only reads frames. An
explicit ``--output`` path writes only the generated JSON report.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _iter_frames(source: Path, limit: int | None) -> Iterator[Any]:
    """Yield BGR numpy frames from a video file or a directory of images."""
    import cv2

    count = 0
    if source.is_dir():
        files = sorted(
            p for p in source.iterdir() if p.suffix.lower() in _IMAGE_SUFFIXES
        )
        for path in files:
            frame = cv2.imread(str(path))
            if frame is None:
                continue
            yield frame
            count += 1
            if limit and count >= limit:
                return
        return
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise SystemExit(f"Could not open video source: {source}")
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            yield frame
            count += 1
            if limit and count >= limit:
                return
    finally:
        capture.release()


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "max": 0.0}
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "max": round(ordered[-1], 3),
    }


def _validated_box(value: Any, context: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{context} box must be [x, y, width, height]")
    try:
        box = [float(part) for part in value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context} box values must be numbers") from exc
    x, y, width, height = box
    if not all(0.0 <= part <= 1.0 for part in box):
        raise ValueError(f"{context} box values must be normalized between 0 and 1")
    if width <= 0 or height <= 0:
        raise ValueError(f"{context} box width and height must be positive")
    if x + width > 1.000001 or y + height > 1.000001:
        raise ValueError(f"{context} box extends beyond the normalized frame")
    return box


def _validated_confidence(value: Any, context: str) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context} confidence must be a number") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError(f"{context} confidence must be between 0 and 1")
    return confidence


def _validated_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    label = value.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"{context}.label must be a non-empty string")
    difficult = value.get("difficult", False)
    if not isinstance(difficult, bool):
        raise ValueError(f"{context}.difficult must be true or false")
    return {
        "label": label.strip().casefold(),
        "box": _validated_box(value.get("box"), f"{context}.box"),
        "difficult": difficult,
    }


def parse_label_thresholds(value: str | None) -> dict[str, float]:
    """Parse ``person=0.50,car=0.60`` into normalized label thresholds."""
    if not value:
        return {}
    thresholds: dict[str, float] = {}
    for item in value.split(','):
        item = item.strip()
        if not item:
            continue
        if '=' not in item:
            raise ValueError(f"invalid label threshold {item!r}; expected label=value")
        label, raw_threshold = (part.strip() for part in item.split('=', 1))
        if not label:
            raise ValueError("label thresholds must include a non-empty label")
        threshold = _validated_confidence(raw_threshold, f"label threshold {label}")
        thresholds[label.casefold()] = threshold
    if not thresholds:
        raise ValueError("--label-thresholds must include at least one label=value")
    return thresholds


def _apply_label_thresholds(
    detections: list[dict[str, Any]], thresholds: dict[str, float]
) -> list[dict[str, Any]]:
    """Apply the action-level per-label rule floor used by live zones."""
    if not thresholds:
        return detections
    return [
        detection for detection in detections
        if float(detection.get('confidence') or 0.0)
        >= thresholds.get(str(detection.get('label') or '').strip().casefold(), 0.0)
    ]


def load_camera_config(path: str | Path) -> dict[str, Any]:
    """Load and validate the camera settings used to replay zone policy.

    The evaluator otherwise replays a POLICY-FREE pipeline, which cannot answer
    the question a zone-scoped operator actually has: "my person rule did not
    fire - was that the detector, or my zone geometry?". This accepts a camera
    settings object shaped like the app's own ``detection`` block (the same
    structure persisted by ``PUT /api/cameras``), so an operator can export
    their real configuration rather than hand-writing a benchmark fixture.
    """
    config_path = Path(path)
    try:
        document = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'could not read camera config {config_path}: {exc}') from exc
    if not isinstance(document, dict):
        raise ValueError('camera config must be a JSON object')
    detection = document.get('detection', {})
    if detection is None:
        detection = {}
    if not isinstance(detection, dict):
        raise ValueError('camera config detection must be an object')
    zones = detection.get('zones', []) or []
    if not isinstance(zones, list):
        raise ValueError('camera config detection.zones must be an array')
    for index, zone in enumerate(zones):
        if not isinstance(zone, dict):
            raise ValueError(f'camera config zone[{index}] must be an object')
        for key in ('x', 'y', 'width', 'height'):
            if key in zone and zone[key] is not None:
                try:
                    float(zone[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f'camera config zone[{index}].{key} must be a number'
                    ) from exc
        rules = zone.get('object_rules', []) or []
        if not isinstance(rules, list):
            raise ValueError(f'camera config zone[{index}].object_rules must be an array')
        for rule_index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise ValueError(
                    f'camera config zone[{index}].object_rules[{rule_index}] '
                    'must be an object'
                )
    validated = dict(document)
    validated['detection'] = {**detection, 'zones': zones}
    validated.setdefault('id', 'eval')
    return validated


def label_thresholds_from_camera_config(settings: dict[str, Any]) -> dict[str, float]:
    """Derive per-label rule floors from a camera's enabled object rules.

    Mirrors how the live path builds its per-label behaviour: each enabled,
    non-motion object rule's ``min_confidence`` is the floor for its label. A
    label with several rules takes the LOWEST floor, because that is the value
    the detector must clear for the camera to see the object at all.
    """
    thresholds: dict[str, float] = {}
    for zone in (settings.get('detection') or {}).get('zones', []) or []:
        if not isinstance(zone, dict) or zone.get('enabled', True) is False:
            continue
        for rule in zone.get('object_rules') or []:
            if not isinstance(rule, dict) or not rule.get('enabled', True):
                continue
            label = str(rule.get('label') or '').strip().casefold()
            if not label or label == 'motion':
                continue
            try:
                confidence = float(rule.get('min_confidence'))
            except (TypeError, ValueError):
                continue
            if label not in thresholds or confidence < thresholds[label]:
                thresholds[label] = confidence
    return thresholds


def _apply_post_pipeline(
    camera_id: str,
    detections: list[dict[str, Any]],
    args: argparse.Namespace,
    settings: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Replay the production decision path after inference.

    Stage order matches ``process_live_stream_alerts`` so the benchmark measures
    the alerting decision, not just the model:

    1. per-label rule floors (``--label-thresholds``, or the camera's own rules
       when ``--camera-config`` is supplied);
    2. tracking (``update_object_tracks``) so the stable track ids and
       displacement history the live path depends on exist here too;
    3. camera/zone scope filtering (``filter_detections_for_camera``);
    4. N-of-M temporal confirmation (``--confirm-frames``/``--confirm-window``);
    5. the post-zone action decision (alert rules + record-on-detect), which is
       what actually decides whether an event fires.

    Returns the actionable detections plus per-stage survivor counts. Without a
    camera config, stages 3 and 5 are skipped and reported as such, so a run
    can never imply zone coverage it did not measure.
    """
    stage_counts: dict[str, int] = {'detected': len(detections)}
    candidates = _apply_label_thresholds(
        detections, getattr(args, 'label_thresholds', {}) or {}
    )
    stage_counts['after_label_rules'] = len(candidates)
    if not candidates:
        return [], stage_counts

    from app.object_tracking import update_object_tracks
    tracked = update_object_tracks(camera_id, candidates)
    stage_counts['tracked'] = len(tracked)

    scoped = tracked
    if settings is not None:
        from app.zone_detection import filter_detections_for_camera
        scoped = filter_detections_for_camera(tracked, settings)
    stage_counts['after_zone_scope'] = len(scoped)

    confirmed = scoped
    required = int(getattr(args, 'confirm_frames', 1))
    if required > 1:
        from app.detection_state import confirm_object_detections
        confirmed = confirm_object_detections(
            camera_id,
            scoped,
            required_frames=required,
            window_frames=int(getattr(args, 'confirm_window', required) or required),
            location_iou=float(getattr(args, 'confirm_iou', 0.0) or 0.0),
        )
    stage_counts['after_confirmation'] = len(confirmed)

    if settings is None:
        stage_counts['actionable'] = len(confirmed)
        return confirmed, stage_counts

    from app.zone_detection import zone_alert_detections, zone_record_on_detect
    alerting = zone_alert_detections(settings, confirmed)
    alerting_ids = {id(detection) for detection in alerting}
    record_only = [
        detection for detection in confirmed
        if id(detection) not in alerting_ids and zone_record_on_detect(detection, settings)
    ]
    stage_counts['actionable'] = len(alerting) + len(record_only)
    return alerting + record_only, stage_counts


def _evaluation_prediction(
    detection: dict[str, Any], frame_index: int
) -> dict[str, Any]:
    """Convert detector output into the benchmark's normalized record shape."""
    return {
        'frame': frame_index,
        'label': str(detection.get('label') or '?').strip().casefold(),
        'box': _validated_box(
            (
                detection.get('box', {}).get('x'),
                detection.get('box', {}).get('y'),
                detection.get('box', {}).get('width'),
                detection.get('box', {}).get('height'),
            ),
            f'prediction at frame {frame_index}',
        ),
        'confidence': _validated_confidence(
            detection.get('confidence'), f'prediction at frame {frame_index}'
        ),
    }


def load_ground_truth(path: str | Path) -> dict[int, list[dict[str, Any]]]:
    """Load and strictly validate normalized, zero-based frame annotations."""
    annotation_path = Path(path)
    try:
        document = json.loads(annotation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read ground truth {annotation_path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("frames"), list):
        raise ValueError("ground truth must be an object with a frames array")
    result: dict[int, list[dict[str, Any]]] = {}
    for position, frame in enumerate(document["frames"]):
        context = f"frames[{position}]"
        if not isinstance(frame, dict):
            raise ValueError(f"{context} must be an object")
        index = frame.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError(f"{context}.index must be a non-negative integer")
        if index in result:
            raise ValueError(f"duplicate ground-truth frame index {index}")
        objects = frame.get("objects")
        if not isinstance(objects, list):
            raise ValueError(f"{context}.objects must be an array")
        result[index] = [
            _validated_object(obj, f"{context}.objects[{obj_index}]")
            for obj_index, obj in enumerate(objects)
        ]
    if not result:
        raise ValueError("ground truth must contain at least one frame")
    return result


def box_iou(left: Any, right: Any) -> float:
    """Return intersection-over-union for normalized xywh boxes."""
    lx, ly, lw, lh = _validated_box(left, "left")
    rx, ry, rw, rh = _validated_box(right, "right")
    intersection_w = max(0.0, min(lx + lw, rx + rw) - max(lx, rx))
    intersection_h = max(0.0, min(ly + lh, ry + rh) - max(ly, ry))
    intersection = intersection_w * intersection_h
    union = lw * lh + rw * rh - intersection
    return intersection / union if union > 0 else 0.0


def _match_frame(
    ground_truth: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    threshold: float,
) -> tuple[list[str], list[float]]:
    """Greedily one-to-one match predictions, ignoring difficult-object overlaps."""
    statuses = ["fp"] * len(predictions)
    ious = [0.0] * len(predictions)
    matched_truth: set[int] = set()
    for pred_index in sorted(
        range(len(predictions)),
        key=lambda index: (-float(predictions[index]["confidence"]), index),
    ):
        prediction = predictions[pred_index]
        candidates = sorted(
            (
                (box_iou(prediction["box"], truth["box"]), truth_index)
                for truth_index, truth in enumerate(ground_truth)
                if truth["label"] == prediction["label"]
                and truth_index not in matched_truth
            ),
            key=lambda item: (-item[0], item[1]),
        )
        regular = [
            candidate
            for candidate in candidates
            if not ground_truth[candidate[1]]["difficult"]
        ]
        best = regular[0] if regular else (candidates[0] if candidates else None)
        if best is None or best[0] < threshold:
            continue
        iou, truth_index = best
        if ground_truth[truth_index]["difficult"]:
            statuses[pred_index] = "ignored"
        else:
            matched_truth.add(truth_index)
            statuses[pred_index] = "tp"
        ious[pred_index] = float(iou)
    return statuses, ious


def _safe_ratio(numerator: int | float, denominator: int | float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _metrics(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(2 * precision * recall / (precision + recall), 6)
        if precision + recall
        else 0.0,
    }


def _average_precision(
    ground_truth: dict[int, list[dict[str, Any]]],
    predictions: list[dict[str, Any]],
    label: str,
    threshold: float,
) -> float:
    regular_by_frame = {
        index: [obj for obj in objects if obj["label"] == label and not obj["difficult"]]
        for index, objects in ground_truth.items()
    }
    support = sum(len(objects) for objects in regular_by_frame.values())
    if support == 0:
        return 0.0
    label_predictions = sorted(
        (pred for pred in predictions if pred["label"] == label),
        key=lambda pred: (-float(pred["confidence"]), pred["frame"]),
    )
    matched_by_frame: dict[int, set[int]] = {index: set() for index in ground_truth}
    true_positives: list[int] = []
    false_positives: list[int] = []
    for prediction in label_predictions:
        index = int(prediction["frame"])
        frame_truth = [
            obj for obj in ground_truth.get(index, []) if obj["label"] == label
        ]
        if not frame_truth:
            false_positives.append(0)
            true_positives.append(0)
            continue
        candidates = sorted(
            (
                (box_iou(prediction["box"], obj["box"]), obj_index)
                for obj_index, obj in enumerate(frame_truth)
                if obj_index not in matched_by_frame[index]
            ),
            key=lambda item: (-item[0], item[1]),
        )
        regular = [
            candidate
            for candidate in candidates
            if not frame_truth[candidate[1]]["difficult"]
        ]
        best = regular[0] if regular else (candidates[0] if candidates else None)
        if best is not None and best[0] >= threshold:
            iou, truth_index = best
            if frame_truth[truth_index]["difficult"]:
                # Difficult matches are removed from evaluation, not scored.
                continue
            matched_by_frame[index].add(truth_index)
            true_positives.append(1)
            false_positives.append(0)
        else:
            true_positives.append(0)
            false_positives.append(1)
    if not true_positives:
        return 0.0
    cumulative_tp = 0
    cumulative_fp = 0
    recalls: list[float] = []
    precisions: list[float] = []
    for true_positive, false_positive in zip(true_positives, false_positives, strict=True):
        cumulative_tp += true_positive
        cumulative_fp += false_positive
        recalls.append(cumulative_tp / support)
        precisions.append(cumulative_tp / (cumulative_tp + cumulative_fp))
    for index in range(len(precisions) - 2, -1, -1):
        precisions[index] = max(precisions[index], precisions[index + 1])
    average_precision = 0.0
    previous_recall = 0.0
    for recall, precision in zip(recalls, precisions, strict=True):
        if recall > previous_recall:
            average_precision += (recall - previous_recall) * precision
            previous_recall = recall
    return round(average_precision, 6)


def evaluate_detections(
    ground_truth: dict[int, list[dict[str, Any]]],
    predictions: list[dict[str, Any]],
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    """Calculate class-aware quality metrics for annotated frame predictions."""
    if not 0 < iou_threshold <= 1:
        raise ValueError("IoU threshold must be greater than 0 and at most 1")
    labels = sorted(
        {obj["label"] for objects in ground_truth.values() for obj in objects}
        | {pred["label"] for pred in predictions}
    )
    per_label: dict[str, Any] = {}
    true_positives = false_positives = false_negatives = 0
    for label in labels:
        label_truth = {
            index: [obj for obj in objects if obj["label"] == label]
            for index, objects in ground_truth.items()
        }
        label_predictions = [pred for pred in predictions if pred["label"] == label]
        label_tp = label_fp = label_fn = 0
        for index, objects in label_truth.items():
            frame_predictions = [
                pred for pred in label_predictions if pred["frame"] == index
            ]
            statuses, _ious = _match_frame(objects, frame_predictions, iou_threshold)
            label_tp += statuses.count("tp")
            label_fp += statuses.count("fp")
            label_fn += sum(1 for obj in objects if not obj["difficult"]) - statuses.count("tp")
        metrics = _metrics(label_tp, label_fp, label_fn)
        metrics["support"] = sum(
            1 for objects in label_truth.values() for obj in objects if not obj["difficult"]
        )
        metrics["ap"] = _average_precision(
            ground_truth, predictions, label, iou_threshold
        )
        per_label[label] = metrics
        true_positives += label_tp
        false_positives += label_fp
        false_negatives += label_fn
    ap50 = [
        _average_precision(ground_truth, predictions, label, 0.5)
        for label in labels
        if per_label[label]["support"]
    ]
    ap_thresholds = [
        _average_precision(ground_truth, predictions, label, threshold)
        for label in labels
        if per_label[label]["support"]
        for threshold in (0.5 + step * 0.05 for step in range(10))
    ]
    overall = _metrics(true_positives, false_positives, false_negatives)
    overall.update(
        {
            "annotated_frames": len(ground_truth),
            "predicted_boxes": len(predictions),
            "iou_threshold": iou_threshold,
            "map_50": round(statistics.mean(ap50), 6) if ap50 else 0.0,
            "map_50_95": round(statistics.mean(ap_thresholds), 6) if ap_thresholds else 0.0,
        }
    )
    return {"overall": overall, "per_label": per_label}


def recommend_input_size(
    sweep: list[dict[str, Any]],
    *,
    min_recall: float = 0.0,
    min_precision: float = 0.0,
    latency_budget_ms: float | None = None,
) -> dict[str, Any]:
    """Pick the smallest input size that still meets the quality floors.

    The point of a guided benchmark is to stop an operator hand-tuning
    ``--input-size`` blind, so the recommendation has to encode a defensible
    rule rather than "highest mAP wins":

    1. Discard any run that misses a required quality floor. A size that
       cannot hold recall is not a faster option, it is a broken one.
    2. Discard any run that blows the latency budget, if one was given.
    3. Of what survives, take the SMALLEST input size. Detection cost scales
       with the square of the input side, so the smallest size that still
       meets the floors is the cheapest acceptable answer - and on a self-
       hosted NVR that is the whole point of the exercise.

    Ties at equal size are broken by mAP. Deliberately a pure function over
    already-measured numbers, with no model or OpenCV involved, so the
    recommendation logic is testable on its own.
    """
    scored: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for run in sweep:
        size = int(run.get('input_size') or 0)
        metrics = run.get('metrics') or {}
        overall = metrics.get('overall') or {}
        recall = float(overall.get('recall') or 0.0)
        precision = float(overall.get('precision') or 0.0)
        latency = run.get('ms_per_inference') or {}
        p95 = float(latency.get('p95') or 0.0)
        candidate = {
            'input_size': size,
            'precision': round(precision, 6),
            'recall': round(recall, 6),
            'f1': round(float(overall.get('f1') or 0.0), 6),
            'map_50': round(float(overall.get('map_50') or 0.0), 6),
            'ms_p95': round(p95, 3),
        }
        reasons: list[str] = []
        if recall < min_recall:
            reasons.append(f'recall {recall:.3f} < {min_recall:.3f}')
        if precision < min_precision:
            reasons.append(f'precision {precision:.3f} < {min_precision:.3f}')
        if latency_budget_ms is not None and p95 > latency_budget_ms:
            reasons.append(f'p95 {p95:.1f}ms > {latency_budget_ms:.1f}ms')
        if reasons:
            rejected.append({**candidate, 'rejected_because': reasons})
        else:
            scored.append(candidate)

    if not scored:
        return {
            'recommended': None,
            'reason': 'No input size met the required quality/latency floors.',
            'acceptable': [],
            'rejected': rejected,
        }

    best = min(scored, key=lambda run: (run['input_size'], -run['map_50']))
    return {
        'recommended': best['input_size'],
        'reason': (
            f"Smallest input size meeting recall>={min_recall:.2f} and "
            f"precision>={min_precision:.2f}"
            + (f' within {latency_budget_ms:.0f}ms p95' if latency_budget_ms is not None else '')
        ),
        'acceptable': sorted(scored, key=lambda run: (run['input_size'], -run['map_50'])),
        'rejected': rejected,
    }


def _build_detector(args: argparse.Namespace):
    if not args.model:
        return None
    from app.detector import OnnxYoloDetector

    detector = OnnxYoloDetector(
        model_path=args.model,
        labels_path=args.labels,
        confidence=float(getattr(args, 'detector_confidence', args.confidence)),
        input_size=args.input_size,
    )
    if not detector.available:
        print(f"WARNING: detector unavailable ({detector.unavailable_reason}); "
              f"running motion-only.", file=sys.stderr)
        return None
    return detector


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    import app.state as state
    from app.detection_state import detect_frame_motion

    # Size the motion thumbnail exactly like the live monitor would.
    state._MOTION_FRAME_W = args.frame_width
    state._MOTION_FRAME_H = args.frame_height
    # Isolate this run's per-camera motion state.
    cam = "eval"
    for store in (
        state._frame_motion_mog2, state._frame_motion_mog2_meta,
        state._frame_motion_prev, state._frame_motion_last_frame,
        state._frame_motion_last_gray,
    ):
        store.pop(cam, None)

    detector = _build_detector(args)
    post_pipeline = bool(getattr(args, 'post_pipeline', False))
    camera_config = getattr(args, 'camera_config', None)
    post_frame_predictions: dict[int, list[dict[str, Any]]] = {}
    post_stage_totals: dict[str, int] = {}
    if post_pipeline:
        # Isolate every per-camera post-pipeline store this run will touch
        # (confirmation window + tracker), so a clip never inherits history
        # from a previous run in the same process.
        confirm_lock = getattr(state, 'live_detection_confirm_lock', None)
        confirm_history = getattr(state, 'live_detection_confirm_history', None)
        if confirm_lock is not None and isinstance(confirm_history, dict):
            with confirm_lock:
                confirm_history.pop(cam, None)
        tracks_lock = getattr(state, '_object_tracks_lock', None)
        tracks = getattr(state, '_object_tracks', None)
        if tracks_lock is not None and isinstance(tracks, dict):
            with tracks_lock:
                tracks.pop(cam, None)
    annotate_dir = Path(args.annotate) if args.annotate else None
    if annotate_dir:
        annotate_dir.mkdir(parents=True, exist_ok=True)

    ground_truth_path = getattr(args, "ground_truth", None)
    ground_truth = load_ground_truth(ground_truth_path) if ground_truth_path else None
    frame_predictions: dict[int, list[dict[str, Any]]] = {}
    frames = 0
    motion_frames = 0
    motion_fractions: list[float] = []
    motion_ms: list[float] = []
    detect_ms: list[float] = []
    frames_with_detection = 0
    label_counts: dict[str, int] = {}
    label_confidences: dict[str, list[float]] = {}

    if annotate_dir is not None:
        import cv2  # noqa: PLC0415 - annotation output is optional

    for frame in _iter_frames(Path(args.input), args.limit):
        frames += 1
        t0 = time.perf_counter()
        has_motion, _conf, _mask, fraction = detect_frame_motion(
            cam, frame,
            pixel_threshold=args.pixel_threshold,
            gate_fraction=args.gate_fraction,
            scale_fraction=args.scale_fraction,
            algorithm=args.algorithm,
            denoise=args.denoise,
            shadow_suppression=args.shadow,
        )
        motion_ms.append((time.perf_counter() - t0) * 1000.0)
        motion_fractions.append(fraction)
        if has_motion:
            motion_frames += 1

        if ground_truth is not None and frames - 1 in ground_truth:
            frame_predictions.setdefault(frames - 1, [])
        detections: list[dict[str, Any]] = []
        # Match the live default (always-on) unless --gated is requested.
        inference_ran = detector is not None and (has_motion or not args.gated)
        if inference_ran:
            t1 = time.perf_counter()
            detections = detector.detect_frame(
                frame,
                confidence=float(getattr(args, 'detector_confidence', args.confidence)),
            )
            if args.tiling:
                from app.region_detection import detect_with_tiling, parse_tile_grid
                grid = parse_tile_grid(args.tiling)
                if grid is not None:
                    detections = detect_with_tiling(
                        detector, frame, detections, cols=grid[0], rows=grid[1],
                        confidence=float(getattr(args, 'detector_confidence', args.confidence)),
                    )
            detect_ms.append((time.perf_counter() - t1) * 1000.0)
            if detections:
                frames_with_detection += 1
            for det in detections:
                label = str(det.get("label") or "?")
                label_counts[label] = label_counts.get(label, 0) + 1
                label_confidences.setdefault(label, []).append(float(det.get("confidence") or 0.0))
            quality_detections = detections
            if post_pipeline:
                quality_detections, stage_counts = _apply_post_pipeline(
                    cam, detections, args, camera_config,
                )
                for stage, count in stage_counts.items():
                    post_stage_totals[stage] = post_stage_totals.get(stage, 0) + count
                if ground_truth is not None and frames - 1 in ground_truth:
                    post_frame_predictions.setdefault(frames - 1, [])
                    post_frame_predictions[frames - 1].extend(
                        _evaluation_prediction(det, frames - 1)
                        for det in quality_detections
                    )
            if ground_truth is not None and frames - 1 in ground_truth:
                frame_predictions[frames - 1].extend(
                    _evaluation_prediction(det, frames - 1)
                    for det in detections
                )

        if annotate_dir is not None:
            annotated = frame.copy()
            for det in detections:
                box = det.get("box") or {}
                h, w = annotated.shape[:2]
                x1 = int(float(box.get("x", 0)) * w)
                y1 = int(float(box.get("y", 0)) * h)
                x2 = int((float(box.get("x", 0)) + float(box.get("width", 0))) * w)
                y2 = int((float(box.get("y", 0)) + float(box.get("height", 0))) * h)
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(annotated, f"{det.get('label')} {det.get('confidence'):.2f}",
                            (x1, max(0, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            cv2.imwrite(str(annotate_dir / f"frame_{frames:06d}.jpg"), annotated)

    if ground_truth is not None:
        missing_frames = sorted(set(ground_truth) - set(frame_predictions))
        evaluated_frames = set(frame_predictions)
        if missing_frames:
            raise ValueError(
                "ground truth references frames not reached by the input "
                f"(first missing index: {missing_frames[0]})"
            )
        predictions = [
            prediction
            for index in sorted(evaluated_frames)
            for prediction in frame_predictions[index]
        ]
        benchmark = evaluate_detections(
            {index: ground_truth[index] for index in sorted(evaluated_frames)},
            predictions,
            getattr(args, "iou_threshold", 0.5),
        )
        if post_pipeline:
            post_predictions = [
                prediction
                for index in sorted(post_frame_predictions)
                for prediction in post_frame_predictions[index]
            ]
            post_benchmark = evaluate_detections(
                {index: ground_truth[index] for index in sorted(evaluated_frames)},
                post_predictions,
                getattr(args, "iou_threshold", 0.5),
            )
        else:
            post_benchmark = None
    else:
        benchmark = None
        post_benchmark = None

    report = {
        "input": str(args.input),
        "frames": frames,
        "motion": {
            "frames_with_motion": motion_frames,
            "motion_rate": round(motion_frames / frames, 4) if frames else 0.0,
            "changed_fraction": _percentiles(motion_fractions),
            "ms_per_frame": _percentiles(motion_ms),
        },
        "objects": {
            "enabled": detector is not None,
            "gated_by_motion": bool(args.gated),
            "frames_with_detection": frames_with_detection,
            "detection_rate": round(frames_with_detection / frames, 4) if frames else 0.0,
            "labels": {
                label: {
                    "count": count,
                    "mean_confidence": round(statistics.mean(label_confidences[label]), 3),
                }
                for label, count in sorted(label_counts.items(), key=lambda kv: -kv[1])
            },
            "ms_per_inference": _percentiles(detect_ms),
        },
        "settings": {
            "algorithm": args.algorithm, "denoise": args.denoise, "shadow": args.shadow,
            "pixel_threshold": args.pixel_threshold, "gate_fraction": args.gate_fraction,
            "scale_fraction": args.scale_fraction, "confidence": args.confidence,
            "detector_confidence": float(getattr(args, 'detector_confidence', args.confidence)),
            "frame_size": [args.frame_width, args.frame_height],
            "post_pipeline": post_pipeline,
            "camera_config": str(camera_config) if camera_config else None,
            "zone_policy_replayed": camera_config is not None,
            "inference_mode": "motion_gated" if args.gated else "always_on",
            "label_thresholds": dict(sorted((getattr(args, 'label_thresholds', {}) or {}).items())),
            "confirm_frames": int(getattr(args, 'confirm_frames', 1)),
            "confirm_window": int(getattr(args, 'confirm_window', getattr(args, 'confirm_frames', 1)) or getattr(args, 'confirm_frames', 1)),
            "confirm_iou": float(getattr(args, 'confirm_iou', 0.0) or 0.0),
        },
        "benchmark": benchmark,
        "post_pipeline_benchmark": post_benchmark,
        "post_pipeline_stages": {
            stage: {
                'total': total,
                'per_frame': round(total / frames, 3) if frames else 0.0,
            }
            for stage, total in sorted(post_stage_totals.items())
        } if post_pipeline else None,
    }
    return report


def _quality_gate_failures(report: dict[str, Any], args: argparse.Namespace) -> list[str]:
    thresholds = {
        "precision": ("precision", args.fail_below_precision),
        "recall": ("recall", args.fail_below_recall),
        "f1": ("f1", args.fail_below_f1),
        "map@0.5": ("map_50", args.fail_below_map_50),
    }
    benchmark = report.get(
        "post_pipeline_benchmark" if getattr(args, "post_pipeline", False) else "benchmark"
    )
    if not benchmark:
        return ["quality thresholds require --ground-truth"]
    quality = benchmark["overall"]
    return [
        f"{name} {float(quality[key]):.6f} is below {float(minimum):.6f}"
        for name, (key, minimum) in thresholds.items()
        if minimum is not None and float(quality[key]) < float(minimum)
    ]


def _print_per_label_quality(report: dict[str, Any], key: str, title: str) -> None:
    benchmark = report.get(key)
    if not benchmark or not benchmark.get('per_label'):
        return
    print(f"  {title} per-label precision/recall/F1/AP:")
    for label, metrics in sorted(benchmark['per_label'].items()):
        print(
            f"    {label:<16} {metrics['precision']:.3f} / "
            f"{metrics['recall']:.3f} / {metrics['f1']:.3f} / "
            f"{metrics['ap']:.3f} (support={metrics['support']})"
        )


def _print_human(report: dict[str, Any]) -> None:
    m = report["motion"]
    o = report["objects"]
    print(f"\nEvaluated {report['frames']} frame(s) from {report['input']}")
    if report.get('settings', {}).get('inference_mode'):
        print(f"Inference mode: {report['settings']['inference_mode']}")
    print("\nMotion")
    print(f"  motion frames : {m['frames_with_motion']} ({m['motion_rate'] * 100:.1f}%)")
    print(f"  changed frac  : p50={m['changed_fraction']['p50']} p95={m['changed_fraction']['p95']} max={m['changed_fraction']['max']}")
    print(f"  ms/frame      : p50={m['ms_per_frame']['p50']} p95={m['ms_per_frame']['p95']}")
    print("\nObjects")
    if not o["enabled"]:
        print("  (no model given - motion-only run)")
    else:
        print(f"  frames w/ det : {o['frames_with_detection']} ({o['detection_rate'] * 100:.1f}%)"
              f"{'  [gated by motion]' if o['gated_by_motion'] else '  [always-on]'}")
        print(f"  ms/inference  : p50={o['ms_per_inference']['p50']} p95={o['ms_per_inference']['p95']}")
        if o["labels"]:
            print("  by label:")
            for label, info in o["labels"].items():
                print(f"    {label:<16} count={info['count']:<6} mean_conf={info['mean_confidence']}")
        else:
            print("  (no detections)")
    if report.get("benchmark"):
        b = report["benchmark"]["overall"]
        print("\nQuality (detector output)")
        print(
            f"  precision/recall/f1 : {b['precision']:.3f} / "
            f"{b['recall']:.3f} / {b['f1']:.3f}"
        )
        print(f"  mAP@0.5            : {b['map_50']:.3f}")
        print(f"  mAP@0.5:0.95       : {b['map_50_95']:.3f}")
        print(
            f"  TP / FP / FN       : {b['true_positives']} / "
            f"{b['false_positives']} / {b['false_negatives']}"
        )
        _print_per_label_quality(report, 'benchmark', 'detector')
    if report.get("post_pipeline_benchmark"):
        p = report["post_pipeline_benchmark"]["overall"]
        print("\nQuality (post-pipeline)")
        print(
            f"  precision/recall/f1 : {p['precision']:.3f} / "
            f"{p['recall']:.3f} / {p['f1']:.3f}"
        )
        print(f"  mAP@0.5            : {p['map_50']:.3f}")
        print(f"  mAP@0.5:0.95       : {p['map_50_95']:.3f}")
        print(
            f"  TP / FP / FN       : {p['true_positives']} / "
            f"{p['false_positives']} / {p['false_negatives']}"
        )
        _print_per_label_quality(report, 'post_pipeline_benchmark', 'post-pipeline')
    if report.get('post_pipeline_stages'):
        print("\nPost-pipeline stages (total survivor count across frames)")
        for stage, info in report['post_pipeline_stages'].items():
            print(f"  {stage:<26} {info['total']:<8} ({info['per_frame']}/frame)")
        if not report['settings'].get('zone_policy_replayed'):
            print(
                "  note: zone scope and the post-zone action decision were NOT "
                "replayed (no --camera-config). Add one to measure them."
            )
    print()


def _print_input_size_recommendation(recommendation: dict[str, Any]) -> None:
    """Human-readable output for a guided input-size sweep (Item 17)."""
    print()
    print("Input size trade-off (lower input size = cheaper inference):")
    header = f"  {'size':>6}  {'precision':>9}  {'recall':>7}  {'mAP@50':>7}  {'p95 ms':>7}  verdict"
    print(header)
    print(f"  {'-' * (len(header) - 2)}")
    rows = list(recommendation.get('acceptable') or []) + list(recommendation.get('rejected') or [])
    for run in sorted(rows, key=lambda item: item.get('input_size') or 0):
        rejected = run.get('rejected_because')
        verdict = 'meets floors' if not rejected else '; '.join(rejected)
        print(
            f"  {run.get('input_size', 0):>6}  {run.get('precision', 0.0):>9.3f}  "
            f"{run.get('recall', 0.0):>7.3f}  {run.get('map_50', 0.0):>7.3f}  "
            f"{run.get('ms_p95', 0.0):>7.1f}  {verdict}"
        )
    recommended = recommendation.get('recommended')
    print()
    if recommended:
        print(f"  Recommended input size: {recommended}")
        print(f"  ({recommendation.get('reason')})")
        print("  Detection cost scales with the square of the input side, so this is the")
        print("  cheapest size that still holds your quality floors.")
    else:
        print(f"  No recommendation: {recommendation.get('reason')}")
        print("  Loosen --min-recall/--min-precision, add a larger size to the sweep,")
        print("  or check that --ground-truth covers the subjects you care about.")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="Video file OR a directory of frame images.")
    parser.add_argument("--model", help="ONNX model path (omit for a motion-only run).")
    parser.add_argument("--labels", default="models/coco.names", help="Labels file for the model.")
    parser.add_argument("--confidence", type=float, default=0.45, help="Object confidence floor (default 0.45).")
    parser.add_argument("--input-size", type=int, default=640, help="Model input size (default 640).")
    parser.add_argument("--algorithm", choices=["mog2", "diff"], default="mog2")
    parser.add_argument("--denoise", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--shadow", choices=["on", "off", "auto"], default="on")
    parser.add_argument("--pixel-threshold", type=float, default=30.0)
    parser.add_argument("--gate-fraction", type=float, default=0.005)
    parser.add_argument("--scale-fraction", type=float, default=0.03)
    parser.add_argument("--frame-width", type=int, default=320)
    parser.add_argument("--frame-height", type=int, default=240)
    parser.add_argument("--tiling", help="Tiled inference grid, e.g. '3x3' (off when omitted). "
                                         "Runs the detector on a grid of tiles to recover small subjects anywhere.")
    parser.add_argument("--gated", action="store_true",
                        help="Only run object detection on motion frames (the legacy CPU-saving gate). "
                             "Default runs it every frame, matching the always-on live default.")
    parser.add_argument(
        "--post-pipeline", action="store_true",
        help="Replay the production decision path after inference: label-rule floors, "
             "tracking, camera/zone scope, N-of-M confirmation, and the post-zone "
             "action decision. Zone and action stages need --camera-config.",
    )
    parser.add_argument(
        "--camera-config",
        help="JSON camera settings (the persisted 'detection' block) used to replay "
             "zone scope and the post-zone action decision. Requires --post-pipeline.",
    )
    parser.add_argument(
        "--scenarios", action="store_true",
        help="Run both inference scenarios (always-on and motion-gated) over the same "
             "input and report each separately, so a camera can be sized for its "
             "actual operating mode.",
    )
    parser.add_argument(
        "--label-thresholds",
        help="Per-label rule floors for --post-pipeline, e.g. person=0.50,car=0.60. "
             "Defaults to the per-label floors in --camera-config.",
    )
    parser.add_argument(
        "--confirm-frames", type=int, default=1,
        help="Post-pipeline detections required in the confirmation window (default: 1).",
    )
    parser.add_argument(
        "--confirm-window", type=int,
        help="Post-pipeline confirmation window; defaults to --confirm-frames.",
    )
    parser.add_argument(
        "--confirm-iou", type=float, default=0.0,
        help="Optional post-pipeline spatial persistence IoU (0 disables it).",
    )
    parser.add_argument("--limit", type=int, help="Stop after N frames.")
    parser.add_argument("--annotate", help="Directory to write annotated frames into.")
    parser.add_argument("--ground-truth", help="JSON annotations for precision/recall and mAP.")
    parser.add_argument("--iou-threshold", type=float, default=0.5,
                        help="IoU threshold for precision/recall/AP (default: 0.5).")
    parser.add_argument(
        "--confidence-sweep",
        help="Comma-separated confidence floors to benchmark, e.g. 0.25,0.35,0.45.",
    )
    parser.add_argument(
        "--sweep-input-sizes",
        help="Comma-separated model input sizes to benchmark, e.g. 320,416,512,640. "
             "Needs --ground-truth to be able to recommend one.",
    )
    parser.add_argument(
        "--min-recall", type=float, default=0.0,
        help="Recall floor for the --sweep-input-sizes recommendation (default: 0.0, i.e. none).",
    )
    parser.add_argument(
        "--min-precision", type=float, default=0.0,
        help="Precision floor for the --sweep-input-sizes recommendation (default: 0.0, i.e. none).",
    )
    parser.add_argument(
        "--latency-budget-ms", type=float,
        help="Optional p95 inference budget (ms) for the --sweep-input-sizes recommendation.",
    )
    parser.add_argument("--output", help="Write the generated JSON report to this path.")
    parser.add_argument("--fail-below-precision", type=float,
                        help="Exit non-zero when benchmark precision is below this value.")
    parser.add_argument("--fail-below-recall", type=float,
                        help="Exit non-zero when benchmark recall is below this value.")
    parser.add_argument("--fail-below-f1", type=float,
                        help="Exit non-zero when benchmark F1 is below this value.")
    parser.add_argument("--fail-below-map-50", type=float,
                        help="Exit non-zero when benchmark mAP@0.5 is below this value.")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON instead of text.")
    args = parser.parse_args(argv)

    confidences = [args.confidence]
    if args.confidence_sweep:
        if not args.model:
            parser.error("--confidence-sweep requires --model")
        try:
            confidences = [float(value.strip()) for value in args.confidence_sweep.split(",")]
        except ValueError:
            parser.error("--confidence-sweep must contain comma-separated numbers")
        if not all(0.0 <= value <= 1.0 for value in confidences):
            parser.error("--confidence-sweep values must be between 0 and 1")
    if not 0.0 < args.iou_threshold <= 1.0:
        parser.error("--iou-threshold must be greater than 0 and at most 1")
    try:
        args.label_thresholds = parse_label_thresholds(args.label_thresholds)
    except ValueError as exc:
        parser.error(str(exc))
    if args.camera_config and not args.post_pipeline:
        parser.error("--camera-config requires --post-pipeline")
    if args.camera_config:
        try:
            args.camera_config = load_camera_config(args.camera_config)
        except ValueError as exc:
            parser.error(str(exc))
        if not args.label_thresholds:
            # Mirror the live path: a camera's own enabled object rules define
            # its per-label floors, so the benchmark measures the camera as
            # actually configured rather than an invented policy.
            args.label_thresholds = label_thresholds_from_camera_config(args.camera_config)
    if not 1 <= args.confirm_frames <= 10:
        parser.error("--confirm-frames must be between 1 and 10")
    args.confirm_window = args.confirm_frames if args.confirm_window is None else args.confirm_window
    if not args.confirm_frames <= args.confirm_window <= 30:
        parser.error("--confirm-window must be at least --confirm-frames and at most 30")
    if not 0.0 <= args.confirm_iou <= 0.9:
        parser.error("--confirm-iou must be between 0 and 0.9")
    if args.label_thresholds and not args.post_pipeline:
        parser.error("--label-thresholds requires --post-pipeline")
    if args.scenarios and not args.input:
        parser.error("--scenarios requires --input")
    quality_limits = {
        "--fail-below-precision": args.fail_below_precision,
        "--fail-below-recall": args.fail_below_recall,
        "--fail-below-f1": args.fail_below_f1,
        "--fail-below-map-50": args.fail_below_map_50,
    }
    for option, minimum in quality_limits.items():
        if minimum is not None and not 0.0 <= minimum <= 1.0:
            parser.error(f"{option} must be between 0 and 1")
    if args.ground_truth is None and any(value is not None for value in quality_limits.values()):
        parser.error("quality thresholds require --ground-truth")
    if args.ground_truth is not None and not args.model:
        parser.error("--ground-truth requires --model")

    # Scenario expansion: the SAME clip replayed under both inference modes.
    # Always-on is the live default; motion-gated is the CPU-saving mode a
    # camera may be switched to. Their recall differs by construction (a
    # subject that never moves enough to clear the gate is invisible in gated
    # mode), so sizing a floor requires both numbers, not one blended figure.
    scenarios: list[tuple[str, bool]] = []
    if args.scenarios:
        scenarios = [('always_on', False), ('motion_gated', True)]
    else:
        scenarios = [('always_on' if not args.gated else 'motion_gated', bool(args.gated))]

    reports = []
    scenario_reports: dict[str, Any] = {}
    size_reports: list[dict[str, Any]] = []
    input_sizes = [
        int(value) for value in str(getattr(args, 'sweep_input_sizes', '') or '').split(',') if value.strip()
    ]
    for confidence in confidences:
        for input_size in (input_sizes or [None]):
            for scenario_name, gated in scenarios:
                run_args = argparse.Namespace(**vars(args))
                run_args.confidence = confidence
                run_args.gated = gated
                if input_size is not None:
                    run_args.input_size = input_size
                label_floor = min(run_args.label_thresholds.values()) if run_args.label_thresholds else confidence
                run_args.detector_confidence = min(confidence, label_floor)
                run_report = evaluate(run_args)
                reports.append(run_report)
                if input_sizes:
                    size_reports.append({
                        'input_size': run_args.input_size,
                        'metrics': (run_report.get('benchmark') or {}).get('overall')
                        or (run_report.get('benchmark') or {}),
                        'ms_per_inference': (run_report.get('objects') or {}).get('ms_per_inference'),
                    })
                if args.scenarios:
                    scenario_reports.setdefault(scenario_name, []).append(run_report)
    report: dict[str, Any] = (
        reports[0]
        if len(reports) == 1
        else {"input": str(args.input), "confidence_sweep": reports}
    )
    if args.scenarios:
        report = {
            "input": str(args.input),
            "scenarios": {
                name: (runs[0] if len(runs) == 1 else {"confidence_sweep": runs})
                for name, runs in scenario_reports.items()
            },
        }
    if size_reports:
        recommendation = recommend_input_size(
            size_reports,
            min_recall=float(getattr(args, 'min_recall', 0.0) or 0.0),
            min_precision=float(getattr(args, 'min_precision', 0.0) or 0.0),
            latency_budget_ms=getattr(args, 'latency_budget_ms', None),
        )
        report = {
            "input": str(args.input),
            "input_size_sweep": size_reports,
            "recommendation": recommendation,
        }
        if not args.json:
            _print_input_size_recommendation(recommendation)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if not args.json:
            print(f"Wrote JSON report to {output_path}")
    elif args.json:
        print(json.dumps(report, indent=2))
    else:
        label_index = 0
        for run_report in reports:
            header_parts = []
            if args.scenarios:
                header_parts.append(str(run_report.get('settings', {}).get('inference_mode') or '?'))
            if len(reports) > 1:
                confidence_index = label_index % len(confidences)
                header_parts.append(f"confidence {confidences[confidence_index]:.3f}")
            if header_parts:
                print(f"\n=== {' / '.join(header_parts)} ===")
            _print_human(run_report)
            label_index += 1
    failures = []
    if any(value is not None for value in quality_limits.values()):
        failures = [
            failure
            for run_report in reports
            for failure in _quality_gate_failures(run_report, args)
        ]
    if failures:
        for failure in failures:
            print(f"QUALITY GATE FAILED: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
