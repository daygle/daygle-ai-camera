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

Nothing here writes to the app database or config; it only reads frames.
"""
from __future__ import annotations

import argparse
import json
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


def _build_detector(args: argparse.Namespace):
    if not args.model:
        return None
    from app.detector import OnnxYoloDetector

    detector = OnnxYoloDetector(
        model_path=args.model,
        labels_path=args.labels,
        confidence=args.confidence,
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
    annotate_dir = Path(args.annotate) if args.annotate else None
    if annotate_dir:
        annotate_dir.mkdir(parents=True, exist_ok=True)

    frames = 0
    motion_frames = 0
    motion_fractions: list[float] = []
    motion_ms: list[float] = []
    detect_ms: list[float] = []
    frames_with_detection = 0
    label_counts: dict[str, int] = {}
    label_confidences: dict[str, list[float]] = {}

    import cv2  # noqa: PLC0415 - only needed when annotating / decoding

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

        detections: list[dict[str, Any]] = []
        # Match the live default (always-on) unless --gated is requested.
        if detector is not None and (has_motion or not args.gated):
            t1 = time.perf_counter()
            detections = detector.detect_frame(frame, confidence=args.confidence)
            if args.tiling:
                from app.region_detection import detect_with_tiling, parse_tile_grid
                grid = parse_tile_grid(args.tiling)
                if grid is not None:
                    detections = detect_with_tiling(
                        detector, frame, detections, cols=grid[0], rows=grid[1],
                        confidence=args.confidence,
                    )
            detect_ms.append((time.perf_counter() - t1) * 1000.0)
            if detections:
                frames_with_detection += 1
            for det in detections:
                label = str(det.get("label") or "?")
                label_counts[label] = label_counts.get(label, 0) + 1
                label_confidences.setdefault(label, []).append(float(det.get("confidence") or 0.0))

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
            "frame_size": [args.frame_width, args.frame_height],
        },
    }
    return report


def _print_human(report: dict[str, Any]) -> None:
    m = report["motion"]
    o = report["objects"]
    print(f"\nEvaluated {report['frames']} frame(s) from {report['input']}")
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
    parser.add_argument("--limit", type=int, help="Stop after N frames.")
    parser.add_argument("--annotate", help="Directory to write annotated frames into.")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON instead of text.")
    args = parser.parse_args(argv)

    report = evaluate(args)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
