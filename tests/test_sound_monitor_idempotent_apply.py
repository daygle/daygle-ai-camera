"""Regression tests for ``apply_sound_settings`` idempotency.

Every camera / zones / sounds settings save funnels
``apply_cameras_settings`` -> ``apply_sound_settings``. The old
stop-everything-then-restart-everything behavior meant each save (however
trivial) tore down every live SoundDetector: rolling audio buffers dropped,
last-detected status reset, and one "Sound monitor started" INFO line per
camera flooded the application log -- the per-minute restart spam operators
reported. The fix keeps running detectors whose enabled-rule fingerprint is
unchanged and whose worker thread is alive, and restarts only new, changed,
or dead detectors. These tests pin that behavior.
"""
from __future__ import annotations

import app.state as _state
import app.sound_monitor as sound_monitor


def _cam(cid: str, *, threshold: float = 0.35, enabled: bool = True) -> dict:
    return {
        'id': cid,
        'name': cid.upper(),
        'rtsp_url': f'rtsp://example/{cid}',
        'detection': {'sound': {'enabled': enabled, 'rules': [
            {'class': 'dog_bark', 'name': 'Dog Bark', 'enabled': True,
             'confidence_threshold': threshold, 'cooldown_seconds': 20,
             'record_on_detect': True},
        ]}},
    }


class _FakeDetector:
    """Stand-in for SoundDetector: records start/stop, reports liveness."""

    def __init__(self, on_detect=None, rules=None, source='ingest',
                 sample_duration_seconds=1.0, audio_segment_provider=None, **_ignored):
        self.rules = rules or []
        self.sound_rules_fingerprint = None
        self._running = False
        self.started = False
        self.stopped = False
        self.backend = 'fake-yamnet'

    @property
    def running(self):
        return self._running and not self.stopped

    def start(self):
        self.started = True
        self._running = True

    def stop(self):
        self.stopped = True


def _setup(monkeypatch, cameras: list[dict]) -> None:
    monkeypatch.setattr(_state, 'cameras_config', cameras)
    monkeypatch.setattr(_state, '_sound_detectors', {})
    monkeypatch.setattr(_state, '_sound_statuses', {})
    monkeypatch.setattr(sound_monitor, 'SoundDetector', _FakeDetector)
    monkeypatch.setattr(sound_monitor, 'build_stream_url', lambda cam: cam.get('rtsp_url'))


def _seed(cid: str, cam: dict, *, running: bool = True) -> _FakeDetector:
    det = _FakeDetector()
    det.rules = [r for r in cam['detection']['sound']['rules'] if r.get('enabled')]
    det.sound_rules_fingerprint = sound_monitor._sound_rules_fingerprint(det.rules)
    det._running = running
    _state._sound_detectors[cid] = det
    return det


def test_unchanged_detectors_are_kept_running(monkeypatch):
    _setup(monkeypatch, [_cam('cam-1'), _cam('cam-2')])
    kept = _seed('cam-1', _cam('cam-1'), running=True)
    _state._sound_statuses['cam-1'] = {
        'state': 'listening', 'last_detected_at': '2026-09-07T00:00:00Z',
        'last_confidence': 0.9, 'backend': 'fake-yamnet',
    }

    sound_monitor.apply_sound_settings()

    # cam-1: same live detector object, not stopped, status untouched.
    assert _state._sound_detectors['cam-1'] is kept
    assert kept.stopped is False
    assert _state._sound_statuses['cam-1']['last_detected_at'] == '2026-09-07T00:00:00Z'
    # cam-2: freshly started detector with its fingerprint recorded.
    started = _state._sound_detectors['cam-2']
    assert started is not kept
    assert started.started is True
    assert started.sound_rules_fingerprint == sound_monitor._sound_rules_fingerprint(
        _cam('cam-2')['detection']['sound']['rules'])
    assert _state._sound_statuses['cam-2']['state'] == 'listening'


def test_changed_rules_restart_only_that_camera(monkeypatch, caplog):
    _setup(monkeypatch, [_cam('cam-1', threshold=0.5), _cam('cam-2')])
    stale = _seed('cam-1', _cam('cam-1', threshold=0.35), running=True)  # fingerprint mismatch
    _seed('cam-2', _cam('cam-2'), running=True)

    with caplog.at_level('INFO', logger='daygle.ai'):
        sound_monitor.apply_sound_settings()

    # cam-1 rules changed -> old detector stopped and replaced by a new one.
    assert stale.stopped is True
    replacement = _state._sound_detectors['cam-1']
    assert replacement is not stale and replacement.started is True
    # cam-2 rules unchanged -> detector kept, no restart log line for it.
    assert _state._sound_detectors['cam-2'].started is False
    started_lines = [r.message for r in caplog.records if 'Sound monitor started for camera' in r.message]
    assert started_lines == ['Sound monitor started for camera cam-1 (rules=[\'dog_bark\'])']


def test_dead_detector_is_restarted_even_when_config_unchanged(monkeypatch):
    _setup(monkeypatch, [_cam('cam-1')])
    dead = _seed('cam-1', _cam('cam-1'), running=False)  # crashed worker thread

    sound_monitor.apply_sound_settings()

    # Apply-as-recovery semantics: a dead worker is restarted despite the
    # matching fingerprint.
    assert dead.stopped is True
    replacement = _state._sound_detectors['cam-1']
    assert replacement is not dead and replacement.started is True


def test_disabled_camera_is_stopped_and_marked_disabled(monkeypatch):
    _setup(monkeypatch, [_cam('cam-1', enabled=False)])
    old = _seed('cam-1', _cam('cam-1'), running=True)

    sound_monitor.apply_sound_settings()

    assert old.stopped is True
    assert 'cam-1' not in _state._sound_detectors
    assert _state._sound_statuses['cam-1']['state'] == 'disabled'
