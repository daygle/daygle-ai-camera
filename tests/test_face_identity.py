"""Unit tests for the live face-identity annotation hook (app/face_identity.py)."""
from __future__ import annotations

import numpy as np
import pytest

import app.face_identity as fi
from app.face_recognition import MatchResult


class _StubService:
    def __init__(self, result, available=True, recognizable=True, auto_enrich=False):
        self.available = available
        self._result = result
        self._recognizable = recognizable
        self.auto_enrich_enabled = auto_enrich
        self.model_id = 'arcface'
        self.calls = 0

    def recognize(self, crop):
        self.calls += 1
        return self._result

    def recognizable(self, crop):
        return self.available and self._recognizable

    def embed_face(self, crop):
        return None


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


def test_indeterminate_face_below_min_size_not_marked_unknown(monkeypatch):
    # A face too small to embed reliably (service.recognizable() == False) is
    # left un-annotated -- neither recognized nor marked 'unknown' -- so it can
    # not masquerade as a stranger for the alert/capture paths. recognize() is
    # never attempted, and nothing is cached so a later larger frame can retry.
    svc = _StubService(None, recognizable=False)
    _use_service(monkeypatch, svc)
    fi.reset_camera_identities('camMin')
    out = fi.annotate_face_identities('camMin', [_face(1)], _frame())
    assert 'recognized' not in out[0]
    assert 'identity' not in out[0]
    assert svc.calls == 0
    # Even with unknown-person alerting configured, an indeterminate face does
    # not alert -- it was never marked recognized.
    _use_rule(monkeypatch, _unknown_rule())
    assert fi.unknown_face_alerts('camMin', out) == []


def test_enriched_tracks_pruned_to_present_tracks(monkeypatch):
    # A high-confidence match triggers auto-enrichment, which records the track
    # in the per-camera _enriched_tracks set (the enrol DB work is offloaded to
    # a background thread that no-ops when the database is unset). That set must
    # be pruned to the tracks still present each cycle: it must not accumulate
    # departed tracks, and must not drop the tracks that ARE still seen (the old
    # per-person pruning did exactly the wrong thing on both counts).
    import app.state as state
    monkeypatch.setattr(state, 'database', None, raising=False)
    _use_service(monkeypatch, _StubService(MatchResult(7, 'Alex', 0.95, 3), auto_enrich=True))
    fi.reset_camera_identities('camEnrich')
    fi.annotate_face_identities('camEnrich', [_face(1)], _frame())
    assert fi._enriched_tracks.get('camEnrich') == {1}
    # Track 1 leaves and a new track 2 appears: the departed track is pruned and
    # the new one recorded, so the set never grows without bound.
    fi.annotate_face_identities('camEnrich', [_face(2)], _frame())
    assert fi._enriched_tracks.get('camEnrich') == {2}
    # Reset clears the per-camera enrichment set.
    fi.reset_camera_identities('camEnrich')
    assert fi._enriched_tracks.get('camEnrich') is None


def test_auto_enrich_off_by_default(monkeypatch):
    # With auto-enrichment disabled (the default), even a near-perfect match does
    # NOT enrol a new embedding -- unsupervised enrolment stays opt-in.
    import app.state as state
    monkeypatch.setattr(state, 'database', None, raising=False)
    _use_service(monkeypatch, _StubService(MatchResult(7, 'Alex', 0.99, 3)))
    fi.reset_camera_identities('camNoEnrich')
    fi.annotate_face_identities('camNoEnrich', [_face(1)], _frame())
    assert not fi._enriched_tracks.get('camNoEnrich')


def test_auto_enrich_skips_low_margin_match(monkeypatch):
    # Enrichment ON, high score, but the match nearly fit a second person
    # (margin 0.05 < _ENRICH_MIN_MARGIN): abstain rather than blur two identities.
    import app.state as state
    monkeypatch.setattr(state, 'database', None, raising=False)
    ambiguous = MatchResult(7, 'Alex', 0.95, 3, runner_up_score=0.90)
    _use_service(monkeypatch, _StubService(ambiguous, auto_enrich=True))
    fi.reset_camera_identities('camAmbig')
    fi.annotate_face_identities('camAmbig', [_face(1)], _frame())
    assert not fi._enriched_tracks.get('camAmbig')


def test_auto_enrich_skips_below_threshold(monkeypatch):
    # Enrichment ON and unambiguous, but the score is below _ENRICH_MIN_SCORE:
    # a merely-decent match is not confident enough to self-enrol.
    import app.state as state
    monkeypatch.setattr(state, 'database', None, raising=False)
    _use_service(monkeypatch, _StubService(MatchResult(7, 'Alex', 0.80, 3), auto_enrich=True))
    fi.reset_camera_identities('camLow')
    fi.annotate_face_identities('camLow', [_face(1)], _frame())
    assert not fi._enriched_tracks.get('camLow')


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
    def __init__(self, available=True):
        self.available = available

    def recognizable(self, crop):
        return self.available


def _unknown_rule(**overrides):
    rule = {
        'id': '_unknown', 'enabled': True, 'email_enabled': True,
        'push_enabled': True, 'email_recipients': '', 'cooldown_minutes': 5,
    }
    rule.update(overrides)
    return rule


def _use_rule(monkeypatch, rule):
    # Unknown-person alerting reads the ``face_detection_rules`` store and
    # filters unknown-type rules by enabled + camera/zone scope. Patch the
    # store so the helper's contract (missing/disabled rule -> no alerts)
    # is preserved through the real code path.
    rules = [rule] if (rule and rule.get('enabled')) else []
    monkeypatch.setattr(
        fi, 'effective_face_detection_rules',
        lambda: {'rules': rules},
    )


def _unknown_face(track_id, confidence=0.9):
    return {'label': 'face', 'track_id': track_id, 'recognized': True,
            'person_id': None, 'person_name': None, 'identity': 'unknown', 'confidence': confidence}


def _known_face(track_id):
    return {'label': 'face', 'track_id': track_id, 'recognized': True,
            'person_id': 7, 'person_name': 'Alex', 'identity': 'Alex'}


def test_unknown_alert_fires_once_per_track(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    _use_rule(monkeypatch, _unknown_rule())
    fi.reset_camera_identities('uA')
    first = fi.unknown_face_alerts('uA', [_unknown_face(1)])
    assert len(first) == 1
    # Same stranger next cycle -> no duplicate alert.
    second = fi.unknown_face_alerts('uA', [_unknown_face(1)])
    assert second == []


def test_unknown_alert_off_when_rule_disabled_or_missing(monkeypatch):
    # No ``_unknown`` rule at all -> no alerts.
    _use_service(monkeypatch, _AlertService())
    _use_rule(monkeypatch, None)
    assert fi.unknown_face_alerts('uB', [_unknown_face(1)]) == []
    # A rule present but disabled is the same as missing.
    _use_rule(monkeypatch, _unknown_rule(enabled=False))
    assert fi.unknown_face_alerts('uB', [_unknown_face(1)]) == []


def test_unknown_alert_off_when_service_unavailable(monkeypatch):
    _use_service(monkeypatch, _AlertService(available=False))
    _use_rule(monkeypatch, _unknown_rule())
    assert fi.unknown_face_alerts('uC', [_unknown_face(1)]) == []


def test_known_face_does_not_alert(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    _use_rule(monkeypatch, _unknown_rule())
    assert fi.unknown_face_alerts('uD', [_known_face(1)]) == []


def test_untracked_unknown_face_does_not_alert(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    _use_rule(monkeypatch, _unknown_rule())
    assert fi.unknown_face_alerts('uE', [_unknown_face(None)]) == []


def test_returning_stranger_realerts(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    _use_rule(monkeypatch, _unknown_rule())
    fi.reset_camera_identities('uF')
    assert len(fi.unknown_face_alerts('uF', [_unknown_face(1)])) == 1
    # Track 1 leaves the frame (a cycle without it) -> forgotten.
    fi.unknown_face_alerts('uF', [])
    # A new track for a stranger alerts again.
    assert len(fi.unknown_face_alerts('uF', [_unknown_face(2)])) == 1


def test_unknown_alert_respects_rule_min_confidence(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    _use_rule(monkeypatch, _unknown_rule(min_confidence=0.8))
    # Below the rule's threshold -> no alert (and the track stays unalerted).
    assert fi.unknown_face_alerts('uG', [_unknown_face(1, confidence=0.5)]) == []
    # A later, higher-confidence sighting of the same track alerts.
    assert len(fi.unknown_face_alerts('uG', [_unknown_face(1, confidence=0.9)])) == 1


def test_unknown_alert_without_rule_min_confidence_gates_nothing(monkeypatch):
    _use_service(monkeypatch, _AlertService())
    _use_rule(monkeypatch, _unknown_rule())
    fi.reset_camera_identities('uH')
    assert len(fi.unknown_face_alerts('uH', [_unknown_face(1, confidence=0.1)])) == 1
