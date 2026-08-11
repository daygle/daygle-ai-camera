"""Tests for the lightweight IoU object tracker (app/object_tracking.py)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.object_tracking as ot  # noqa: E402
import app.state as st  # noqa: E402


def _det(label, x, y, w=0.1, h=0.1, conf=0.9):
    return {'label': label, 'confidence': conf, 'box': {'x': x, 'y': y, 'width': w, 'height': h}}


def _reset(cam):
    with st._object_tracks_lock:
        st._object_tracks.pop(cam, None)


def test_same_object_keeps_track_id_across_cycles():
    cam = 'trk-same'
    _reset(cam)
    d1 = ot.update_object_tracks(cam, [_det('person', 0.40, 0.40)])
    tid = d1[0]['track_id']
    assert d1[0]['track_new'] is True and d1[0]['track_age'] == 1
    # Next cycle: the box drifts slightly (overlaps) -> same id, age grows.
    d2 = ot.update_object_tracks(cam, [_det('person', 0.42, 0.41)])
    assert d2[0]['track_id'] == tid
    assert d2[0]['track_new'] is False and d2[0]['track_age'] == 2


def test_distinct_objects_get_distinct_ids():
    cam = 'trk-distinct'
    _reset(cam)
    dets = ot.update_object_tracks(cam, [_det('person', 0.1, 0.1), _det('person', 0.8, 0.8)])
    ids = {d['track_id'] for d in dets}
    assert len(ids) == 2


def test_different_label_does_not_reuse_track():
    cam = 'trk-label'
    _reset(cam)
    a = ot.update_object_tracks(cam, [_det('person', 0.4, 0.4)])
    # A cat in the same spot must NOT inherit the person's track id.
    b = ot.update_object_tracks(cam, [_det('cat', 0.4, 0.4)])
    assert b[0]['track_id'] != a[0]['track_id']
    assert b[0]['track_new'] is True


def test_track_retired_after_max_age_then_new_id():
    cam = 'trk-age'
    _reset(cam)
    first = ot.update_object_tracks(cam, [_det('car', 0.5, 0.5)])[0]['track_id']
    # Object leaves for more than max_age empty cycles.
    for _ in range(6):
        ot.update_object_tracks(cam, [], max_age=5)
    # Reappearing in the same place gets a fresh id (the old track was retired).
    reappeared = ot.update_object_tracks(cam, [_det('car', 0.5, 0.5)])[0]
    assert reappeared['track_id'] != first
    assert reappeared['track_new'] is True


def test_empty_detections_returns_empty_without_error():
    cam = 'trk-empty'
    _reset(cam)
    assert ot.update_object_tracks(cam, []) == []


def test_iou_basic():
    a = {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}
    assert ot._iou(a, a) == 1.0
    disjoint = {'x': 0.9, 'y': 0.9, 'width': 0.05, 'height': 0.05}
    assert ot._iou(a, disjoint) < 0.01
