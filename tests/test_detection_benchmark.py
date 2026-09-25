"""Unit tests for detection benchmark parsing and quality metrics."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "evaluate_detection", REPO_ROOT / "scripts" / "evaluate_detection.py"
)
evaluate_detection = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(evaluate_detection)


def _prediction(frame: int, label: str, box: list[float], confidence: float) -> dict:
    return {"frame": frame, "label": label, "box": box, "confidence": confidence}


def _truth(label: str, box: list[float], difficult: bool = False) -> dict:
    return {"label": label, "box": box, "difficult": difficult}


def test_box_iou_is_symmetric_and_uses_xywh_coordinates():
    left = [0.0, 0.0, 0.5, 0.5]
    right = [0.25, 0.0, 0.5, 0.5]
    assert evaluate_detection.box_iou(left, right) == pytest.approx(1 / 3)
    assert evaluate_detection.box_iou(right, left) == pytest.approx(1 / 3)


def test_load_ground_truth_normalizes_labels_and_validates_boxes(tmp_path):
    annotation = tmp_path / "labels.json"
    annotation.write_text(
        json.dumps(
            {
                "frames": [
                    {"index": 0, "objects": [{"label": " Person ", "box": [0, 0, 0.2, 0.4]}]},
                    {"index": 2, "objects": []},
                ]
            }
        ),
        encoding="utf-8",
    )

    loaded = evaluate_detection.load_ground_truth(annotation)

    assert list(loaded) == [0, 2]
    assert loaded[0][0] == _truth("person", [0.0, 0.0, 0.2, 0.4])

    annotation.write_text(
        json.dumps({"frames": [{"index": 0, "objects": [{"label": "person", "box": [0.9, 0, 0.2, 0.2]}]}]}),
        encoding="utf-8",
    )
    try:
        evaluate_detection.load_ground_truth(annotation)
    except ValueError as exc:
        assert "extends beyond" in str(exc)
    else:
        raise AssertionError("out-of-bounds ground-truth box was accepted")


def test_quality_metrics_are_class_aware_and_one_to_one():
    ground_truth = {
        0: [_truth("person", [0.10, 0.10, 0.20, 0.40])],
        1: [_truth("dog", [0.60, 0.50, 0.20, 0.30])],
    }
    predictions = [
        _prediction(0, "person", [0.11, 0.11, 0.19, 0.39], 0.90),
        _prediction(0, "person", [0.12, 0.12, 0.18, 0.38], 0.70),
        _prediction(0, "cat", [0.60, 0.10, 0.20, 0.20], 0.60),
    ]

    result = evaluate_detection.evaluate_detections(ground_truth, predictions, 0.5)
    overall = result["overall"]

    assert overall["true_positives"] == 1
    assert overall["false_positives"] == 2
    assert overall["false_negatives"] == 1
    assert overall["precision"] == 0.333333
    assert overall["recall"] == 0.5
    assert overall["f1"] == 0.4
    assert overall["map_50"] == 0.5
    assert result["per_label"]["person"]["ap"] == 1.0
    assert result["per_label"]["dog"]["ap"] == 0.0
    assert result["per_label"]["cat"]["support"] == 0


def test_matching_uses_confidence_order_for_precision_and_ap():
    ground_truth = {0: [_truth("person", [0.1, 0.1, 0.4, 0.4])]}
    predictions = [
        _prediction(0, "person", [0.55, 0.1, 0.4, 0.4], 0.9),
        _prediction(0, "person", [0.11, 0.11, 0.38, 0.38], 0.6),
    ]

    result = evaluate_detection.evaluate_detections(ground_truth, predictions)

    assert result["overall"]["true_positives"] == 1
    assert result["overall"]["false_positives"] == 1
    assert result["per_label"]["person"]["ap"] == 0.5


def test_difficult_detections_are_ignored_instead_of_counted_as_false_positives():
    ground_truth = {0: [_truth("person", [0.1, 0.1, 0.2, 0.2], difficult=True)]}
    predictions = [_prediction(0, "person", [0.11, 0.11, 0.18, 0.18], 0.8)]

    result = evaluate_detection.evaluate_detections(ground_truth, predictions)

    assert result["overall"]["false_positives"] == 0
    assert result["overall"]["false_negatives"] == 0
    assert result["overall"]["map_50"] == 0.0


def test_confidence_sweep_writes_comparison_report(tmp_path, monkeypatch):
    output = tmp_path / "reports" / "sweep.json"

    def fake_evaluate(args):
        return {
            "input": str(args.input),
            "frames": 1,
            "settings": {"confidence": args.confidence},
        }

    monkeypatch.setattr(evaluate_detection, "evaluate", fake_evaluate)

    result = evaluate_detection.main(
        [
            "--input", "frames",
            "--model", "model.onnx",
            "--confidence-sweep", "0.2,0.4",
            "--output", str(output),
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert [run["settings"]["confidence"] for run in report["confidence_sweep"]] == [0.2, 0.4]


def test_quality_gate_reports_metrics_below_floor():
    args = SimpleNamespace(
        fail_below_precision=0.8,
        fail_below_recall=0.7,
        fail_below_f1=None,
        fail_below_map_50=0.5,
    )
    report = {"benchmark": {"overall": {"precision": 0.75, "recall": 0.8, "map_50": 0.4}}}

    failures = evaluate_detection._quality_gate_failures(report, args)

    assert failures == [
        "precision 0.750000 is below 0.800000",
        "map@0.5 0.400000 is below 0.500000",
    ]


def test_evaluate_adds_quality_report_when_ground_truth_is_supplied(tmp_path, monkeypatch):
    annotation = tmp_path / "labels.json"
    annotation.write_text(
        json.dumps(
            {
                "frames": [
                    {"index": 0, "objects": [{"label": "person", "box": [0.1, 0.1, 0.2, 0.4]}]},
                    {"index": 1, "objects": []},
                ]
            }
        ),
        encoding="utf-8",
    )

    state_module = SimpleNamespace(
        _MOTION_FRAME_W=320,
        _MOTION_FRAME_H=240,
        _frame_motion_mog2={},
        _frame_motion_mog2_meta={},
        _frame_motion_prev={},
        _frame_motion_last_frame={},
        _frame_motion_last_gray={},
    )
    monkeypatch.setitem(sys.modules, "app.state", state_module)
    monkeypatch.setitem(
        sys.modules,
        "app.detection_state",
        SimpleNamespace(detect_frame_motion=lambda *args, **kwargs: (True, 0.5, None, 0.1)),
    )
    monkeypatch.setattr(
        evaluate_detection,
        "_iter_frames",
        lambda source, limit: iter(["first", "second"]),
    )

    class Detector:
        available = True

        def detect_frame(self, frame, confidence):
            if frame == "second":
                return []
            return [{"label": "person", "confidence": 0.9, "box": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.4}}]

    monkeypatch.setattr(evaluate_detection, "_build_detector", lambda args: Detector())
    args = SimpleNamespace(
        input="unused",
        model="model.onnx",
        labels="labels.txt",
        confidence=0.45,
        input_size=640,
        algorithm="mog2",
        denoise=True,
        shadow="on",
        pixel_threshold=30.0,
        gate_fraction=0.005,
        scale_fraction=0.03,
        frame_width=320,
        frame_height=240,
        gated=False,
        tiling=None,
        limit=None,
        annotate=None,
        ground_truth=str(annotation),
        iou_threshold=0.5,
    )

    report = evaluate_detection.evaluate(args)

    assert report["benchmark"]["overall"]["annotated_frames"] == 2
    assert report["benchmark"]["overall"]["precision"] == 1.0
    assert report["benchmark"]["overall"]["recall"] == 1.0
    assert report["benchmark"]["overall"]["map_50"] == 1.0
