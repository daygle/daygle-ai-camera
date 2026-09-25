from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path

from app.detection_state import build_track_from_live_history
from app.recordings import RecordingService
import app.state as state


def _service(tmp_path: Path) -> RecordingService:
    return RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {},
    })


def _write_segment(directory: Path, name: str, mtime: float) -> Path:
    segment = directory / name
    segment.write_bytes(b'segment')
    os.utime(segment, (mtime, mtime))
    return segment


def test_segment_timeline_reuses_unchanged_directory(tmp_path):
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    camera_dir.mkdir(parents=True)
    now = time.time()
    first_segment = _write_segment(camera_dir, 'segment-0001.mp4', now)

    first = service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, 4.0)
    second = service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, 4.0)

    assert second is first
    assert first[0][0] == first_segment
    assert len(service._segment_timeline_cache) == 1


def test_segment_timeline_refreshes_when_directory_mtime_changes(tmp_path):
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    camera_dir.mkdir(parents=True)
    now = time.time()
    _write_segment(camera_dir, 'segment-0001.mp4', now)
    first = service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, 4.0)

    # Creation normally changes the directory mtime. Set it explicitly as well
    # so the test remains deterministic on filesystems with coarse mtime ticks.
    second_segment = _write_segment(camera_dir, 'segment-0002.mp4', now + 4.0)
    directory_mtime_ns = camera_dir.stat().st_mtime_ns
    os.utime(camera_dir, ns=(directory_mtime_ns + 1_000_000, directory_mtime_ns + 1_000_000))
    second = service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, 4.0)

    assert second is not first
    assert [item[0] for item in second] == [first[0][0], second_segment]


def test_pruning_and_explicit_invalidation_drop_timeline_cache(tmp_path):
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    camera_dir.mkdir(parents=True)
    old_segment = _write_segment(camera_dir, 'segment-old.mp4', time.time() - 60)
    assert service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, 4.0)

    service._prune_prebuffer_segments(camera_dir, 5)

    assert not old_segment.exists()
    assert service._segment_timeline_cache == {}
    assert service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, 4.0) == []

    service._invalidate_segment_timeline_cache()
    assert service._segment_timeline_cache == {}


def test_live_history_track_uses_inclusive_timestamp_bounds():
    camera_id = 'track-camera'
    sample = lambda ts, label: (ts, [{'label': label}])
    history = deque([
        sample(10.0, 'before'),
        sample(20.0, 'start-a'),
        sample(20.0, 'start-b'),
        sample(30.0, 'end'),
        sample(40.0, 'after'),
    ])
    with state.live_detection_history_lock:
        previous = state.live_detection_history.get(camera_id)
        state.live_detection_history[camera_id] = history
    try:
        track = build_track_from_live_history(camera_id, 20.0, 30.0)
    finally:
        with state.live_detection_history_lock:
            if previous is None:
                state.live_detection_history.pop(camera_id, None)
            else:
                state.live_detection_history[camera_id] = previous

    assert track == [
        {'t': 0.0, 'detections': [{'label': 'start-a'}]},
        {'t': 0.0, 'detections': [{'label': 'start-b'}]},
        {'t': 10.0, 'detections': [{'label': 'end'}]},
    ]
