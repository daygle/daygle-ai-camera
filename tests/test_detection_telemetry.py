"""Unit tests for the detection-pipeline telemetry store.

The telemetry store is the measurement layer behind "why did this camera miss
the person?". These tests pin the aggregation arithmetic (per-stage survivor
counts and per-reason rejection counts), the bounded window, per-camera
isolation, and pruning, without needing a camera, a detector, or numpy.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.detection_telemetry import (  # noqa: E402
    MODE_ALWAYS_ON,
    MODE_ERROR,
    MODE_MOTION_GATED,
    REJECT_CONFIRMATION,
    REJECT_MOTION_MODE,
    REJECT_ZONE,
    WINDOW_SIZE,
    clear_detection_telemetry,
    detection_telemetry_payload,
    prune_detection_telemetry,
    record_detection_cycle,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_detection_telemetry()
    yield
    clear_detection_telemetry()


def test_totals_sum_stage_survivors_and_rejections():
    record_detection_cycle(
        'cam-a',
        inference_mode=MODE_ALWAYS_ON,
        camera_motion=False,
        frame_motion=True,
        candidates={
            'detected': 5,
            'after_motion_mode': 3,
            'after_camera_filter': 3,
            'after_confirmation': 2,
            'alertable': 2,
        },
        rejected={REJECT_MOTION_MODE: 2, REJECT_ZONE: 1},
        event_created=True,
    )
    record_detection_cycle(
        'cam-a',
        inference_mode=MODE_ALWAYS_ON,
        candidates={'detected': 4, 'after_motion_mode': 4, 'alertable': 4},
    )

    payload = detection_telemetry_payload('cam-a')
    totals = payload['totals']
    assert totals['cycles'] == 2
    assert totals['inference_cycles'] == 2
    assert totals['candidates_detected'] == 9
    assert totals['candidates_after_motion_mode'] == 7
    assert totals['candidates_alertable'] == 6
    assert totals['events'] == 1
    assert totals['rejected'][REJECT_MOTION_MODE] == 2
    assert totals['rejected'][REJECT_ZONE] == 1
    # A reason never seen stays at zero rather than going missing.
    assert totals['rejected'][REJECT_CONFIRMATION] == 0


def test_last_cycle_records_mode_and_camera_motion():
    record_detection_cycle(
        'cam-a', inference_mode=MODE_MOTION_GATED, camera_motion=True,
        camera_motion_reason='ptz_command', candidates={'detected': 1},
    )
    last = detection_telemetry_payload('cam-a')['last_cycle']
    assert last['inference_mode'] == MODE_MOTION_GATED
    assert last['camera_motion'] is True
    assert last['camera_motion_reason'] == 'ptz_command'
    assert last['candidates']['detected'] == 1


def test_error_cycles_count_as_cycles_but_not_inferences():
    record_detection_cycle('cam-a', inference_mode=MODE_ERROR)
    record_detection_cycle('cam-a', inference_mode=MODE_ERROR)
    totals = detection_telemetry_payload('cam-a')['totals']
    assert totals['cycles'] == 2
    assert totals['inference_cycles'] == 0


def test_history_window_is_bounded_but_totals_keep_accumulating():
    for _ in range(WINDOW_SIZE + 25):
        record_detection_cycle('cam-a', inference_mode=MODE_ALWAYS_ON, candidates={'detected': 1})
    payload = detection_telemetry_payload('cam-a')
    assert payload['cycles_recorded'] == WINDOW_SIZE
    # The dropped cycles are still counted in the running totals.
    assert payload['totals']['candidates_detected'] == WINDOW_SIZE + 25


def test_cameras_are_isolated():
    record_detection_cycle('cam-a', inference_mode=MODE_ALWAYS_ON, candidates={'detected': 2})
    record_detection_cycle('cam-b', inference_mode=MODE_MOTION_GATED, candidates={'detected': 7})

    assert detection_telemetry_payload('cam-a')['totals']['candidates_detected'] == 2
    assert detection_telemetry_payload('cam-b')['totals']['candidates_detected'] == 7

    everything = detection_telemetry_payload()
    assert everything['cameras'] == ['cam-a', 'cam-b']
    assert everything['totals']['candidates_detected'] == 9
    assert everything['totals']['inference_cycles'] == 2
    assert set(everything['last_cycle_by_camera']) == {'cam-a', 'cam-b'}


def test_unknown_camera_reads_as_empty_not_missing():
    payload = detection_telemetry_payload('never-seen')
    assert payload['cycles_recorded'] == 0
    assert payload['last_cycle'] is None
    assert payload['totals']['cycles'] == 0


def test_prune_drops_only_removed_cameras():
    record_detection_cycle('keep', inference_mode=MODE_ALWAYS_ON)
    record_detection_cycle('drop', inference_mode=MODE_ALWAYS_ON)

    prune_detection_telemetry({'keep'})

    assert detection_telemetry_payload()['cameras'] == ['keep']


def test_prune_with_none_is_a_no_op():
    record_detection_cycle('keep', inference_mode=MODE_ALWAYS_ON)
    prune_detection_telemetry(None)
    assert detection_telemetry_payload()['cameras'] == ['keep']


def test_clear_single_and_all():
    record_detection_cycle('a', inference_mode=MODE_ALWAYS_ON)
    record_detection_cycle('b', inference_mode=MODE_ALWAYS_ON)
    clear_detection_telemetry('a')
    assert detection_telemetry_payload()['cameras'] == ['b']
    clear_detection_telemetry()
    assert detection_telemetry_payload()['cameras'] == []


def test_recording_never_raises_on_hostile_input():
    """A malformed count must not break the detection cycle that reported it."""
    entry = record_detection_cycle(
        'cam-a',
        inference_mode=MODE_ALWAYS_ON,
        candidates={'detected': 'not-a-number'},
    )
    assert entry == {}
    # The cycle still advances, so a later good record is stored normally.
    record_detection_cycle('cam-a', inference_mode=MODE_ALWAYS_ON, candidates={'detected': 3})
    assert detection_telemetry_payload('cam-a')['totals']['candidates_detected'] == 3
