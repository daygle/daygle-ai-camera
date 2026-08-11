"""Tests for the standalone detection-evaluation harness (scripts/evaluate_detection.py).

Motion-only (no model) so it runs anywhere numpy + OpenCV are available.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("cv2")
pytest.importorskip("numpy")

import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "evaluate_detection", REPO_ROOT / "scripts" / "evaluate_detection.py"
)
evaluate_detection = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(evaluate_detection)


def _write_frames(directory: Path) -> None:
    import cv2
    import numpy as np
    base = np.full((240, 320, 3), 100, dtype=np.uint8)
    for i in range(6):
        frame = base.copy()
        if i >= 3:
            frame[40:140, 40:200] = 240  # motion in the second half
        cv2.imwrite(str(directory / f"f{i:03d}.jpg"), frame)


def _args(**overrides):
    defaults = dict(
        input=None, model=None, labels="models/coco.names", confidence=0.45,
        input_size=640, algorithm="mog2", denoise=True, shadow="on",
        pixel_threshold=30.0, gate_fraction=0.005, scale_fraction=0.03,
        frame_width=320, frame_height=240, gated=False, limit=None, annotate=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_evaluate_reports_motion_on_synthetic_clip(tmp_path):
    _write_frames(tmp_path)
    report = evaluate_detection.evaluate(_args(input=str(tmp_path)))
    assert report["frames"] == 6
    # The 3 moving frames should register; the seed/static frames should not.
    assert 1 <= report["motion"]["frames_with_motion"] <= 4
    assert report["objects"]["enabled"] is False  # no model -> motion-only
    assert "changed_fraction" in report["motion"]
    assert report["settings"]["algorithm"] == "mog2"


def test_evaluate_respects_frame_limit(tmp_path):
    _write_frames(tmp_path)
    report = evaluate_detection.evaluate(_args(input=str(tmp_path), limit=2))
    assert report["frames"] == 2


def test_evaluate_diff_engine_runs(tmp_path):
    _write_frames(tmp_path)
    report = evaluate_detection.evaluate(_args(input=str(tmp_path), algorithm="diff"))
    assert report["frames"] == 6
    assert report["settings"]["algorithm"] == "diff"
