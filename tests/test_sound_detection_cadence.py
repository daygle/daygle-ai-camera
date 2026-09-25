"""Sound-detection cadence: the classifier's sampling rate is configurable.

YAMNet runs on every overlapping audio window, so with the historical hop the
model classified twice a second per sound-enabled camera with no way to trade
that back. ``detection_interval_seconds`` (per camera, on /sounds) now sets the
hop while the analysis WINDOW stays at one second, so a short transient is
never truncated - only sampled less often.

These tests pin the clamp (a hop can never exceed the window, which would
silently drop audio), the persisted setting, and the fact that a changed
interval restarts the detector rather than being ignored on a live one.
"""

from __future__ import annotations

import app.state as _state
import app.sound_monitor as sound_monitor
from app.recording_settings import _normalize_camera_sound_settings
from app.sound_detector import SAMPLE_RATE, SoundDetector


def _detector(interval=None, window: float = 1.0) -> SoundDetector:
    kwargs = {'source': 'ingest', 'sample_duration_seconds': window}
    if interval is not None:
        kwargs['detection_interval_seconds'] = interval
    return SoundDetector(on_detect=lambda *a, **k: None, rules=[], **kwargs)


# ─── The hop ──────────────────────────────────────────────────────────────


def test_default_cadence_is_unchanged():
    """0.5s keeps the historical behaviour: a 1s window classified twice a
    second, i.e. the old 50%-overlapping hop."""
    detector = _detector()
    assert detector.detection_interval_seconds == 0.5
    assert detector._hop_samples(int(SAMPLE_RATE * 1.0)) == SAMPLE_RATE // 2


def test_a_longer_interval_moves_the_hop_not_the_window():
    detector = _detector(interval=0.2, window=1.0)
    assert detector.detection_interval_seconds == 0.2
    assert detector._hop_samples(int(SAMPLE_RATE * 1.0)) == int(SAMPLE_RATE * 0.2)


def test_hop_never_exceeds_the_window():
    """A hop longer than the window would leave uncovered audio between
    windows - a sound could fall entirely in the gap."""
    detector = _detector(interval=5.0, window=1.0)
    # Clamped to half a window by the constructor, so coverage is never lost.
    assert detector.detection_interval_seconds == 0.5
    chunk = int(SAMPLE_RATE * 1.0)
    assert detector._hop_samples(chunk) <= chunk

    short_window = _detector(interval=5.0, window=0.4)
    short_chunk = int(SAMPLE_RATE * 0.4)
    assert short_window._hop_samples(short_chunk) <= short_chunk


def test_garbage_interval_falls_back_to_the_default():
    for value in ('soon', None, [], object()):
        assert _detector(interval=value).detection_interval_seconds == 0.5


def test_interval_floor_is_kept():
    assert _detector(interval=0.0).detection_interval_seconds == 0.1
    assert _detector(interval=0.01).detection_interval_seconds == 0.1


# ─── The stored setting ───────────────────────────────────────────────────


def _sound_raw(interval) -> dict:
    payload = {'enabled': True, 'rules': [
        {'class': 'dog_bark', 'name': 'Dog Bark', 'enabled': True},
    ]}
    if interval is not None:
        payload['detection_interval_seconds'] = interval
    return payload


def test_settings_persist_the_interval():
    assert _normalize_camera_sound_settings(_sound_raw(0.2))['detection_interval_seconds'] == 0.2
    # Absent / unusable -> the historical cadence.
    assert _normalize_camera_sound_settings(_sound_raw(None))['detection_interval_seconds'] == 0.5
    assert _normalize_camera_sound_settings(_sound_raw('nope'))['detection_interval_seconds'] == 0.5
    # Clamped to the same bounds the detector enforces.
    assert _normalize_camera_sound_settings(_sound_raw(0.01))['detection_interval_seconds'] == 0.1
    assert _normalize_camera_sound_settings(_sound_raw(9))['detection_interval_seconds'] == 0.5


def test_interval_is_part_of_the_detector_fingerprint():
    """A running detector only picks up a new cadence when it is restarted,
    so the interval has to be part of the restart fingerprint."""
    rules = [{'class': 'dog_bark', 'enabled': True}]
    assert sound_monitor._sound_rules_fingerprint(rules, 0.5) != sound_monitor._sound_rules_fingerprint(rules, 0.2)
    assert sound_monitor._sound_rules_fingerprint(rules, 0.2) == sound_monitor._sound_rules_fingerprint(rules, 0.2)
    # Rule ORDER still must not matter (UI saves can permute it).
    reordered = [{'class': 'dog_bark', 'enabled': True}, {'class': 'doorbell', 'enabled': True}]
    assert sound_monitor._sound_rules_fingerprint(reordered, 0.2) == sound_monitor._sound_rules_fingerprint(
        list(reversed(reordered)), 0.2,
    )


# ─── Applying a changed interval ──────────────────────────────────────────


class _FakeDetector:
    def __init__(self, on_detect=None, rules=None, source='ingest',
                 sample_duration_seconds=1.0, audio_segment_provider=None,
                 detection_interval_seconds=0.5, **_ignored):
        self.rules = rules or []
        self.detection_interval_seconds = detection_interval_seconds
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


def _cam(cid: str, interval=None) -> dict:
    sound = {
        'enabled': True,
        'rules': [{'class': 'dog_bark', 'name': 'Dog Bark', 'enabled': True,
                   'confidence_threshold': 0.35, 'cooldown_seconds': 20,
                   'record_on_detect': True}],
    }
    if interval is not None:
        sound['detection_interval_seconds'] = interval
    return {
        'id': cid,
        'name': cid.upper(),
        'rtsp_url': f'rtsp://example/{cid}',
        'detection': {'sound': sound},
    }


def _setup(monkeypatch, cameras: list[dict]) -> None:
    monkeypatch.setattr(_state, 'cameras_config', cameras)
    monkeypatch.setattr(_state, '_sound_detectors', {})
    monkeypatch.setattr(_state, '_sound_statuses', {})
    monkeypatch.setattr(sound_monitor, 'SoundDetector', _FakeDetector)
    monkeypatch.setattr(sound_monitor, 'build_stream_url', lambda cam: cam.get('rtsp_url'))


def _seed(cid: str, cam: dict, *, running: bool = True) -> _FakeDetector:
    det = _FakeDetector()
    det.rules = [r for r in cam['detection']['sound']['rules'] if r.get('enabled')]
    det.sound_rules_fingerprint = sound_monitor._sound_rules_fingerprint(
        det.rules, cam['detection']['sound'].get('detection_interval_seconds', 0.5),
    )
    det._running = running
    _state._sound_detectors[cid] = det
    return det


def test_changed_interval_restarts_the_detector(monkeypatch):
    _setup(monkeypatch, [_cam('cam-1', interval=0.2)])
    stale = _seed('cam-1', _cam('cam-1', interval=0.5), running=True)

    sound_monitor.apply_sound_settings()

    # Same rules, new cadence: the old detector cannot honour it, so it is
    # replaced rather than left running on the previous interval.
    assert stale.stopped is True
    replacement = _state._sound_detectors['cam-1']
    assert replacement is not stale and replacement.started is True
    assert replacement.detection_interval_seconds == 0.2


def test_unchanged_interval_keeps_the_detector_running(monkeypatch):
    _setup(monkeypatch, [_cam('cam-1', interval=0.2)])
    kept = _seed('cam-1', _cam('cam-1', interval=0.2), running=True)

    sound_monitor.apply_sound_settings()

    assert _state._sound_detectors['cam-1'] is kept
    assert kept.stopped is False
    assert kept.started is False
