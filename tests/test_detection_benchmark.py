"""Unit tests for detection benchmark parsing and quality metrics."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading as _threading
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


def test_parse_label_thresholds_and_apply_per_label_floor():
    thresholds = evaluate_detection.parse_label_thresholds('Person=0.50, car=0.70')
    assert thresholds == {'person': 0.5, 'car': 0.7}

    detections = [
        {'label': 'person', 'confidence': 0.49},
        {'label': 'person', 'confidence': 0.50},
        {'label': 'car', 'confidence': 0.69},
    ]
    assert evaluate_detection._apply_label_thresholds(detections, thresholds) == [
        {'label': 'person', 'confidence': 0.50},
    ]


def test_post_pipeline_delegates_confirmation_settings(monkeypatch):
    calls = []

    def fake_confirm(camera_id, detections, **kwargs):
        calls.append((camera_id, detections, kwargs))
        return detections

    monkeypatch.setitem(
        sys.modules,
        'app.detection_state',
        SimpleNamespace(confirm_object_detections=fake_confirm),
    )
    # The tracker runs before confirmation in the production stage order, so
    # stub it out to keep this test focused on the confirmation arguments.
    monkeypatch.setitem(
        sys.modules,
        'app.object_tracking',
        SimpleNamespace(update_object_tracks=lambda camera_id, detections: detections),
    )
    args = SimpleNamespace(
        label_thresholds={'person': 0.5},
        confirm_frames=2,
        confirm_window=3,
        confirm_iou=0.2,
    )
    detections = [{'label': 'person', 'confidence': 0.8}]

    result, stages = evaluate_detection._apply_post_pipeline('eval', detections, args)
    assert result == detections
    assert stages['detected'] == 1
    assert stages['after_confirmation'] == 1
    assert calls == [(
        'eval', detections,
        {'required_frames': 2, 'window_frames': 3, 'location_iou': 0.2},
    )]


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


def test_quality_gate_targets_post_pipeline_when_selected():
    args = SimpleNamespace(
        post_pipeline=True,
        fail_below_precision=0.8,
        fail_below_recall=None,
        fail_below_f1=None,
        fail_below_map_50=None,
    )
    report = {
        'benchmark': {'overall': {'precision': 0.95, 'recall': 0.95, 'map_50': 0.95}},
        'post_pipeline_benchmark': {'overall': {'precision': 0.70, 'recall': 0.90, 'map_50': 0.80}},
    }

    assert evaluate_detection._quality_gate_failures(report, args) == [
        'precision 0.700000 is below 0.800000',
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


def test_evaluate_reports_post_pipeline_quality_separately(tmp_path, monkeypatch):
    annotation = tmp_path / 'labels.json'
    annotation.write_text(
        json.dumps({'frames': [{'index': 0, 'objects': [
            {'label': 'person', 'box': [0.1, 0.1, 0.2, 0.4]},
        ]}]}),
        encoding='utf-8',
    )
    state_module = SimpleNamespace(
        _MOTION_FRAME_W=320, _MOTION_FRAME_H=240,
        _frame_motion_mog2={}, _frame_motion_mog2_meta={},
        _frame_motion_prev={}, _frame_motion_last_frame={},
        _frame_motion_last_gray={},
        live_detection_confirm_lock=_threading.Lock(),
        live_detection_confirm_history={},
        _object_tracks_lock=_threading.Lock(),
        _object_tracks={},
    )
    monkeypatch.setitem(sys.modules, 'app.state', state_module)
    monkeypatch.setitem(
        sys.modules, 'app.detection_state',
        SimpleNamespace(
            detect_frame_motion=lambda *a, **k: (True, 0.5, None, 0.1),
            confirm_object_detections=lambda cam, dets, **k: dets,
        ),
    )
    monkeypatch.setattr(evaluate_detection, '_iter_frames', lambda source, limit: iter(['frame']))

    class Detector:
        available = True

        def detect_frame(self, frame, confidence):
            return [{'label': 'person', 'confidence': 0.4, 'box': {
                'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.4,
            }}]

    monkeypatch.setattr(evaluate_detection, '_build_detector', lambda args: Detector())
    args = SimpleNamespace(
        input='unused', model='model.onnx', labels='labels.txt', confidence=0.45,
        detector_confidence=0.45, input_size=640, algorithm='mog2', denoise=True,
        shadow='on', pixel_threshold=30.0, gate_fraction=0.005, scale_fraction=0.03,
        frame_width=320, frame_height=240, gated=False, tiling=None, limit=None,
        annotate=None, ground_truth=str(annotation), iou_threshold=0.5,
        post_pipeline=True, label_thresholds={'person': 0.5}, confirm_frames=1,
        confirm_window=1, confirm_iou=0.0, camera_config=None,
    )

    report = evaluate_detection.evaluate(args)

    assert report['benchmark']['overall']['precision'] == 1.0
    assert report['post_pipeline_benchmark']['overall']['precision'] == 0.0
    assert report['post_pipeline_benchmark']['overall']['predicted_boxes'] == 0


# ---------------------------------------------------------------------------
# Post-pipeline zone/track replay and scenario breakdowns
# ---------------------------------------------------------------------------


def test_load_camera_config_validates_and_defaults_id(tmp_path):
    config = tmp_path / 'camera.json'
    config.write_text(
        json.dumps({'detection': {'zones': [{'id': 'z', 'x': 0, 'y': 0, 'width': 1, 'height': 1}]}}),
        encoding='utf-8',
    )
    loaded = evaluate_detection.load_camera_config(config)
    assert loaded['id'] == 'eval'
    assert loaded['detection']['zones'][0]['id'] == 'z'


@pytest.mark.parametrize('payload, message', [
    (['not', 'an', 'object'], 'must be a JSON object'),
    ({'detection': {'zones': 'nope'}}, 'zones must be an array'),
    ({'detection': {'zones': [{'x': 'left'}]}}, 'x must be a number'),
    ({'detection': {'zones': [{'object_rules': 'nope'}]}}, 'object_rules must be an array'),
])
def test_load_camera_config_rejects_malformed_policy(tmp_path, payload, message):
    config = tmp_path / 'camera.json'
    config.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        evaluate_detection.load_camera_config(config)


def test_label_thresholds_from_camera_config_takes_lowest_floor():
    settings = {'detection': {'zones': [
        {'enabled': True, 'object_rules': [
            {'label': 'Person', 'min_confidence': 0.40, 'enabled': True},
            {'label': 'person', 'min_confidence': 0.25, 'enabled': True},
            {'label': 'car', 'min_confidence': 0.60, 'enabled': True},
            # Disabled rules and motion rules must not shape the floors.
            {'label': 'dog', 'min_confidence': 0.05, 'enabled': False},
            {'label': 'motion', 'min_confidence': 0.01, 'enabled': True},
        ]},
        {'enabled': False, 'object_rules': [
            {'label': 'boat', 'min_confidence': 0.02, 'enabled': True},
        ]},
    ]}}
    assert evaluate_detection.label_thresholds_from_camera_config(settings) == {
        'person': 0.25, 'car': 0.60,
    }


def test_post_pipeline_replays_zone_scope_and_action_decision(monkeypatch):
    """Zone scope and the post-zone action decision gate the final set."""
    monkeypatch.setitem(
        sys.modules, 'app.object_tracking',
        SimpleNamespace(update_object_tracks=lambda camera_id, detections: detections),
    )
    monkeypatch.setitem(
        sys.modules, 'app.detection_state',
        SimpleNamespace(confirm_object_detections=lambda cam, dets, **k: dets),
    )
    # A detection outside the zone is dropped by scope; the in-zone one is kept.
    monkeypatch.setitem(
        sys.modules, 'app.zone_detection',
        SimpleNamespace(
            filter_detections_for_camera=lambda dets, settings: [
                d for d in dets if d['label'] == 'person'
            ],
            zone_alert_detections=lambda settings, dets: list(dets),
            zone_record_on_detect=lambda det, settings: True,
        ),
    )
    args = SimpleNamespace(
        label_thresholds={}, confirm_frames=1, confirm_window=1, confirm_iou=0.0,
    )
    detections = [
        {'label': 'person', 'confidence': 0.9},
        {'label': 'cat', 'confidence': 0.9},
    ]

    result, stages = evaluate_detection._apply_post_pipeline(
        'eval', detections, args, {'detection': {'zones': []}},
    )

    assert [d['label'] for d in result] == ['person']
    assert stages['after_zone_scope'] == 1
    assert stages['actionable'] == 1


def test_post_pipeline_without_camera_config_skips_zone_stages(monkeypatch):
    """Without a camera config the run must not imply zone coverage."""
    monkeypatch.setitem(
        sys.modules, 'app.object_tracking',
        SimpleNamespace(update_object_tracks=lambda camera_id, detections: detections),
    )
    args = SimpleNamespace(
        label_thresholds={}, confirm_frames=1, confirm_window=1, confirm_iou=0.0,
    )
    result, stages = evaluate_detection._apply_post_pipeline(
        'eval', [{'label': 'person', 'confidence': 0.9}], args,
    )
    assert len(result) == 1
    assert stages['after_zone_scope'] == 1
    assert stages['actionable'] == 1


def test_evaluate_reports_post_pipeline_stage_counts(tmp_path, monkeypatch):
    annotation = tmp_path / 'labels.json'
    annotation.write_text(
        json.dumps({'frames': [{'index': 0, 'objects': [
            {'label': 'person', 'box': [0.1, 0.1, 0.2, 0.4]},
        ]}]}), encoding='utf-8',
    )
    state_module = SimpleNamespace(
        _MOTION_FRAME_W=320, _MOTION_FRAME_H=240,
        _frame_motion_mog2={}, _frame_motion_mog2_meta={},
        _frame_motion_prev={}, _frame_motion_last_frame={},
        _frame_motion_last_gray={},
        live_detection_confirm_lock=_threading.Lock(),
        live_detection_confirm_history={},
        _object_tracks_lock=_threading.Lock(),
        _object_tracks={},
    )
    monkeypatch.setitem(sys.modules, 'app.state', state_module)
    monkeypatch.setitem(
        sys.modules, 'app.detection_state',
        SimpleNamespace(
            detect_frame_motion=lambda *a, **k: (True, 0.5, None, 0.1),
            confirm_object_detections=lambda cam, dets, **k: dets,
        ),
    )
    monkeypatch.setattr(evaluate_detection, '_iter_frames', lambda source, limit: iter(['frame']))

    class Detector:
        available = True

        def detect_frame(self, frame, confidence):
            return [{'label': 'person', 'confidence': 0.8, 'box': {
                'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.4,
            }}]

    monkeypatch.setattr(evaluate_detection, '_build_detector', lambda args: Detector())
    args = SimpleNamespace(
        input='unused', model='model.onnx', labels='labels.txt', confidence=0.45,
        detector_confidence=0.45, input_size=640, algorithm='mog2', denoise=True,
        shadow='on', pixel_threshold=30.0, gate_fraction=0.005, scale_fraction=0.03,
        frame_width=320, frame_height=240, gated=False, tiling=None, limit=None,
        annotate=None, ground_truth=str(annotation), iou_threshold=0.5,
        post_pipeline=True, label_thresholds={}, confirm_frames=1,
        confirm_window=1, confirm_iou=0.0, camera_config=None,
    )

    report = evaluate_detection.evaluate(args)

    stages = report['post_pipeline_stages']
    assert stages['detected']['total'] == 1
    assert stages['actionable']['total'] == 1
    assert report['settings']['zone_policy_replayed'] is False


def test_scenarios_runs_both_inference_modes(tmp_path, monkeypatch):
    """--scenarios evaluates always-on AND motion-gated over the same input."""
    annotation = tmp_path / 'labels.json'
    annotation.write_text(
        json.dumps({'frames': [{'index': 0, 'objects': [
            {'label': 'person', 'box': [0.1, 0.1, 0.2, 0.4]},
        ]}]}), encoding='utf-8',
    )
    state_module = SimpleNamespace(
        _MOTION_FRAME_W=320, _MOTION_FRAME_H=240,
        _frame_motion_mog2={}, _frame_motion_mog2_meta={},
        _frame_motion_prev={}, _frame_motion_last_frame={},
        _frame_motion_last_gray={},
        live_detection_confirm_lock=_threading.Lock(),
        live_detection_confirm_history={},
        _object_tracks_lock=_threading.Lock(),
        _object_tracks={},
    )
    monkeypatch.setitem(sys.modules, 'app.state', state_module)
    # A frame with NO motion: always-on still runs inference, motion-gated does
    # not, which is exactly the recall difference a scenario run must expose.
    monkeypatch.setitem(
        sys.modules, 'app.detection_state',
        SimpleNamespace(detect_frame_motion=lambda *a, **k: (False, 0.0, None, 0.0)),
    )
    monkeypatch.setattr(evaluate_detection, '_iter_frames', lambda source, limit: iter(['frame']))

    class Detector:
        available = True

        def __init__(self):
            self.calls = 0

        def detect_frame(self, frame, confidence):
            self.calls += 1
            return [{'label': 'person', 'confidence': 0.8, 'box': {
                'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.4,
            }}]

    shared_detector = Detector()
    monkeypatch.setattr(evaluate_detection, '_build_detector', lambda args: shared_detector)
    monkeypatch.setattr(evaluate_detection, '_iter_frames', lambda source, limit: iter(['frame']))

    assert evaluate_detection.main([
        '--input', 'unused', '--model', 'model.onnx',
        '--ground-truth', str(annotation), '--scenarios', '--json',
    ]) == 0
    # Two scenarios over one motion-free frame: always-on inferred, gated did not.
    assert shared_detector.calls == 1
