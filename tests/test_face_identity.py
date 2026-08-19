"""Unit tests for the live face-identity annotation hook (app/face_identity.py)."""
from __future__ import annotations

import numpy as np
import pytest

import app.face_identity as fi
from app.face_recognition import MatchResult


class _StubService:
    def __init__(self, result, available=True):
        self.available = available
        self._result = result
        self.calls = 0

    def recognize(self, crop):
        self.calls += 1
        return self._result


def _use_service(monkeypatch, service):
    monkeypatch.setattr(fi, 'get_face_recognition_service', lambda: service)


def _face(track_id, box=None):
    return {
        'label': 'face',
        'track_id': track_id,
        'box': box or {'x': 0.25, 'y': 0.25, 'width': 0.5, 'height': 0.5},
    }


def _frame():
    return np.zeros((100, 100, 3), dtype=np.uint8)


def test_annotate_recognized_person(monkeypatch):
    _use_service(monkeypatch, _StubService(MatchResult(7, 'Alex', 0.82, 3)))
    dets = [_face(1)]
    fi.reset_camera_identities('camA')
    out = fi.annotate_face_identities('camA', dets, _frame())
    assert out[0]['recognized'] is True
    assert out[0]['person_id'] == 7
    assert out[0]['person_name'] == 'Alex'
    assert out[0]['identity'] == 'Alex'
    assert out[0]['identity_score'] == pytest.approx(0.82, abs=1e-4)


def test_annotate_unknown(monkeypatch):
    _use_service(monkeypatch, _StubService(None))
    dets = [_face(1)]
    fi.reset_camera_identities('camB')
    out = fi.annotate_face_identities('camB', dets, _frame())
    assert out[0]['recognized'] is True
    assert out[0]['identity'] == 'unknown'
    assert out[0]['person_id'] is None


def test_known_identity_is_amortized_across_cycles(monkeypatch):
    svc = _StubService(MatchResult(7, 'Alex', 0.9, 3))
    _use_service(monkeypatch, svc)
    fi.reset_camera_identities('camC')
    fi.annotate_face_identities('camC', [_face(1)], _frame())
    fi.annotate_face_identities('camC', [_face(1)], _frame())
    fi.annotate_face_identities('camC', [_face(1)], _frame())
    # A known track is embedded once and then reused.
    assert svc.calls == 1


def test_unknown_identity_is_retried_each_cycle(monkeypatch):
    svc = _StubService(None)
    _use_service(monkeypatch, svc)
    fi.reset_camera_identities('camD')
    fi.annotate_face_identities('camD', [_face(1)], _frame())
    fi.annotate_face_identities('camD', [_face(1)], _frame())
    # An unknown face keeps being retried (a later frame may be clearer).
    assert svc.calls == 2


def test_non_face_detections_untouched(monkeypatch):
    svc = _StubService(MatchResult(7, 'Alex', 0.9, 3))
    _use_service(monkeypatch, svc)
    dets = [{'label': 'person', 'track_id': 1, 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}]
    out = fi.annotate_face_identities('camE', dets, _frame())
    assert 'recognized' not in out[0]
    assert svc.calls == 0


def test_no_op_when_service_unavailable(monkeypatch):
    svc = _StubService(MatchResult(7, 'Alex', 0.9, 3), available=False)
    _use_service(monkeypatch, svc)
    dets = [_face(1)]
    out = fi.annotate_face_identities('camF', dets, _frame())
    assert 'recognized' not in out[0]
    assert svc.calls == 0


def test_no_op_when_frame_missing(monkeypatch):
    _use_service(monkeypatch, _StubService(MatchResult(7, 'Alex', 0.9, 3)))
    dets = [_face(1)]
    out = fi.annotate_face_identities('camG', dets, None)
    assert 'recognized' not in out[0]


def test_cache_pruned_when_faces_leave(monkeypatch):
    _use_service(monkeypatch, _StubService(MatchResult(7, 'Alex', 0.9, 3)))
    fi.reset_camera_identities('camH')
    fi.annotate_face_identities('camH', [_face(1)], _frame())
    assert fi._cache.get('camH')  # cached
    # A cycle with no faces clears the camera's cache.
    fi.annotate_face_identities('camH', [{'label': 'person', 'track_id': 2}], _frame())
    assert 'camH' not in fi._cache


def test_face_identity_metadata_summary():
    dets = [
        {'recognized': True, 'person_id': 7, 'person_name': 'Alex', 'track_id': 1, 'identity_score': 0.8},
        {'recognized': True, 'person_id': None, 'person_name': None, 'track_id': 2},
        {'label': 'car'},  # no recognition -> ignored
    ]
    meta = fi.face_identity_metadata(dets)
    assert meta['face_identities']['unknown'] == 1
    assert meta['face_identities']['people'] == [
        {'person_id': 7, 'name': 'Alex', 'track_id': 1, 'score': 0.8}
    ]


def test_face_identity_metadata_empty_without_faces():
    assert fi.face_identity_metadata([{'label': 'car'}, {'label': 'person'}]) == {}


# ---------------------------------------------------------------------------
# unknown_face_alerts (alert-on-unknown)
# ---------------------------------------------------------------------------

class _AlertService:
    def __init__(self, available=True, alert_unknown=True):
        self.available = available
        self.alert_unknown = alert_unknown


def _unknown_face(track_id):
    return {'label': 'face', 'track_id': track_id, 'recognized': True,
            'person_id': None, 'person_name': None, 'identity': 'unknown', 'confidence': 0.9}


def _known_face(track_id):
    return {'label': 'face', 'track_id': track_id, 'recognized': True,
            'person_id': 7, 'person_name': 'Alex', 'identity': 'Alex'}


def test_unknown_alert_fires_once_per_track(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    fi.reset_camera_identities('uA')
    first = fi.unknown_face_alerts('uA', [_unknown_face(1)])
    assert len(first) == 1
    # Same stranger next cycle -> no duplicate alert.
    second = fi.unknown_face_alerts('uA', [_unknown_face(1)])
    assert second == []


def test_unknown_alert_off_when_setting_disabled(monkeypatch):
    _use_service(monkeypatch, _AlertService(alert_unknown=False))
    assert fi.unknown_face_alerts('uB', [_unknown_face(1)]) == []


def test_unknown_alert_off_when_service_unavailable(monkeypatch):
    _use_service(monkeypatch, _AlertService(available=False))
    assert fi.unknown_face_alerts('uC', [_unknown_face(1)]) == []


def test_known_face_does_not_alert(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    assert fi.unknown_face_alerts('uD', [_known_face(1)]) == []


def test_untracked_unknown_face_does_not_alert(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    assert fi.unknown_face_alerts('uE', [_unknown_face(None)]) == []


def test_returning_stranger_realerts(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    fi.reset_camera_identities('uF')
    assert len(fi.unknown_face_alerts('uF', [_unknown_face(1)])) == 1
    # Track 1 leaves the frame (a cycle without it) -> forgotten.
    fi.unknown_face_alerts('uF', [])
    # A new track for a stranger alerts again.
    assert len(fi.unknown_face_alerts('uF', [_unknown_face(2)])) == 1
