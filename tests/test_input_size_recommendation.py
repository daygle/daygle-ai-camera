"""Tests for the guided input-size recommendation (Item 17).

``recommend_input_size`` is the part of a guided benchmark that decides an
answer, so it gets tested directly and exhaustively: it is a pure function
over already-measured numbers, with no model, no OpenCV, and no video, which
means the recommendation rules can be pinned here instead of being trusted
from a single manual sweep on one machine.

The rule being defended is "smallest input size that still meets the quality
floors" - NOT "highest mAP wins", because detection cost scales with the
square of the input side and a self-hosted NVR is buying headroom.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "evaluate_detection_recommend", REPO_ROOT / "scripts" / "evaluate_detection.py"
)
evaluate_detection = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluate_detection)

recommend_input_size = evaluate_detection.recommend_input_size


def run(input_size, *, precision, recall, map_50=0.5, p95=20.0, f1=0.5):
    """One already-measured sweep entry, in the shape the CLI produces."""
    return {
        'input_size': input_size,
        'metrics': {
            'overall': {
                'precision': precision,
                'recall': recall,
                'f1': f1,
                'map_50': map_50,
            }
        },
        'ms_per_inference': {'p50': p95 * 0.8, 'p95': p95, 'max': p95 * 1.2},
    }


# ─── the core rule ────────────────────────────────────────────────────────

def test_recommends_the_smallest_size_that_meets_the_floors() -> None:
    sweep = [
        run(320, precision=0.60, recall=0.55),
        run(416, precision=0.80, recall=0.85),
        run(512, precision=0.90, recall=0.92),
        run(640, precision=0.93, recall=0.95),
    ]
    result = recommend_input_size(sweep, min_recall=0.80, min_precision=0.75)
    # 320 misses the floors; 416 is the cheapest that clears them, even though
    # 640 scores best. Cost scales with the square of the input side.
    assert result['recommended'] == 416


def test_recommends_the_smallest_size_when_everything_meets_the_floors() -> None:
    sweep = [
        run(320, precision=0.95, recall=0.95),
        run(640, precision=0.99, recall=0.99),
    ]
    result = recommend_input_size(sweep, min_recall=0.5, min_precision=0.5)
    assert result['recommended'] == 320


def test_without_floors_it_still_picks_the_smallest() -> None:
    sweep = [
        run(320, precision=0.10, recall=0.10),
        run(640, precision=0.99, recall=0.99),
    ]
    result = recommend_input_size(sweep)
    assert result['recommended'] == 320


# ─── floors reject sizes ──────────────────────────────────────────────────

def test_a_size_missing_the_recall_floor_is_rejected() -> None:
    sweep = [run(320, precision=0.99, recall=0.20), run(640, precision=0.95, recall=0.90)]
    result = recommend_input_size(sweep, min_recall=0.80)
    assert result['recommended'] == 640
    assert any('recall' in reason for entry in result['rejected'] for reason in entry['rejected_because'])


def test_a_size_missing_the_precision_floor_is_rejected() -> None:
    sweep = [run(320, precision=0.10, recall=0.95), run(640, precision=0.80, recall=0.90)]
    result = recommend_input_size(sweep, min_precision=0.50)
    assert result['recommended'] == 640
    assert any('precision' in reason for entry in result['rejected'] for reason in entry['rejected_because'])


def test_a_latency_budget_rejects_a_slow_size() -> None:
    sweep = [
        run(320, precision=0.95, recall=0.95, p95=10.0),
        run(640, precision=0.95, recall=0.95, p95=90.0),
    ]
    result = recommend_input_size(sweep, latency_budget_ms=50.0)
    assert result['recommended'] == 320
    assert any('p95' in reason for entry in result['rejected'] for reason in entry['rejected_because'])


def test_a_latency_budget_can_reject_everything() -> None:
    sweep = [run(320, precision=0.95, recall=0.95, p95=200.0)]
    result = recommend_input_size(sweep, latency_budget_ms=50.0)
    assert result['recommended'] is None
    assert 'No input size met' in result['reason']


# ─── degenerate inputs ────────────────────────────────────────────────────

def test_an_empty_sweep_recommends_nothing() -> None:
    result = recommend_input_size([])
    assert result['recommended'] is None
    assert result['acceptable'] == []


def test_a_run_with_no_metrics_is_rejected_rather_than_crashing() -> None:
    # Zeroed quality, so it fails any non-zero floor rather than raising.
    result = recommend_input_size([{'input_size': 320}], min_recall=0.5)
    assert result['recommended'] is None
    # With no floors at all, zeroed quality trivially meets them - the point is
    # that it does not raise.
    result = recommend_input_size([{'input_size': 320}], min_recall=0.0, min_precision=0.0)
    assert result['recommended'] == 320


def test_missing_latency_stats_do_not_crash() -> None:
    sweep = [{'input_size': 320, 'metrics': {'overall': {'precision': 1.0, 'recall': 1.0}}}]
    result = recommend_input_size(sweep, latency_budget_ms=50.0)
    # No p95 recorded means 0ms, which trivially meets any budget.
    assert result['recommended'] == 320


def test_equal_sizes_are_broken_by_map() -> None:
    sweep = [
        run(512, precision=0.80, recall=0.80, map_50=0.60),
        run(512, precision=0.80, recall=0.80, map_50=0.75),
    ]
    result = recommend_input_size(sweep)
    assert result['recommended'] == 512
    # The tiebreak orders the table the same way it picks the winner, so the
    # first acceptable row is the one actually recommended.
    assert result['acceptable'][0]['map_50'] == 0.75


def test_acceptable_and_rejected_partition_the_sweep() -> None:
    sweep = [
        run(320, precision=0.10, recall=0.10),
        run(416, precision=0.90, recall=0.90),
        run(512, precision=0.95, recall=0.95),
        run(640, precision=0.05, recall=0.99),
    ]
    result = recommend_input_size(sweep, min_recall=0.80, min_precision=0.80)
    assert len(result['acceptable']) + len(result['rejected']) == len(sweep)
    assert result['recommended'] == 416


def test_the_reason_names_the_floors_that_were_applied() -> None:
    sweep = [run(320, precision=0.9, recall=0.9)]
    result = recommend_input_size(sweep, min_recall=0.8, min_precision=0.8)
    assert '0.80' in result['reason']
    assert '0.80' in result['reason']
