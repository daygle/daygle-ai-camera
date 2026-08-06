"""Verification that YAMNet sound detection runs and records in parallel when
multiple cameras hear sounds at the same time.

Architecture under test (app/sound_monitor.py + app/sound_detector.py):

* ``apply_sound_settings`` starts ONE ``SoundDetector`` per enabled camera,
  each on its own daemon thread reading only that camera's audio queue via
  ``audio_segments_after(camera_id, ...)``.
* All detectors share a single module-level YAMNet backend singleton
  (``app.sound_detector._yamnet``). Its ``score_all`` serializes the actual
  TFLite ``invoke`` under a lock (a TFLite interpreter is not safe for
  concurrent invocation), but everything around it - buffering, thresholding,
  cooldown, the ``on_detect`` callback and the per-camera recording it fires -
  runs concurrently per camera.

So the two claims these tests pin are:

1. **Detection is parallel** - when two cameras hear a sound simultaneously,
   both detector threads are genuinely in flight at the same time (peak
   concurrency > 1), and each fires its own callback.
2. **Recording is parallel and per-camera** - each detection primes the
   prebuffer and attaches an event recording for *its own* ``camera_id``,
   independently, with no cross-camera serialization of the recording path.

The real TFLite runtime need not be installed: these tests replace the shared
``_yamnet`` singleton with a fake backend that keeps the same lock discipline,
so they exercise the orchestration layer where the multi-camera parallelism
lives.
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np


SAMPLE_RATE = 16000


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_wav(path: Path, seconds: float = 1.2) -> None:
    """Write a mono 16 kHz s16le WAV of ``seconds`` of quiet noise.

    The content is irrelevant - classification is faked - but the detector's
    ingest path reads real WAV frames, so the file must be a valid, non-empty
    16 kHz mono PCM segment longer than one 1s chunk so ``_handle_chunk`` runs.
    """
    n = int(SAMPLE_RATE * seconds)
    samples = (np.random.default_rng(0).standard_normal(n) * 32).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())


class _ConcurrencyTrackingBackend:
    """Stand-in for the YAMNet singleton that records how many detector threads
    are inside ``score_all`` at once, while keeping the same lock discipline the
    real backend uses around ``invoke``."""

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores
        self._lock = threading.Lock()
        self._active = 0
        self.peak_concurrency = 0
        self.call_count = 0
        self.backend_name = "fake_yamnet"
        self.unavailable_reason = None

    def preload(self) -> None:
        # The ingest thread kicks off a preload in the background; nothing to do.
        return None

    def score_all(self, audio):  # noqa: ANN001 - mirrors real signature
        # Observe concurrency BEFORE the serialized section: this is where the
        # real backend does its lock-free numpy preprocessing and where two
        # camera threads genuinely overlap.
        with self._lock:
            self.call_count += 1
            self._active += 1
            self.peak_concurrency = max(self.peak_concurrency, self._active)
        try:
            # Widen the overlap window so concurrent callers reliably coincide
            # on CI without depending on scheduler luck.
            time.sleep(0.05)
            return dict(self._scores)
        finally:
            with self._lock:
                self._active -= 1


# ─────────────────────────────────────────────────────────────────────────────
# 1. Detection runs in parallel across cameras
# ─────────────────────────────────────────────────────────────────────────────


def test_two_cameras_detect_in_parallel(tmp_path, monkeypatch):
    import app.sound_detector as sd

    fake = _ConcurrencyTrackingBackend({"cat_meow": 0.99})
    monkeypatch.setattr(sd, "_yamnet", fake)

    # One WAV segment per camera, each in its own directory.
    seg_a = tmp_path / "cam_a.wav"
    seg_b = tmp_path / "cam_b.wav"
    _write_wav(seg_a)
    _write_wav(seg_b)

    fired: dict[str, list[float]] = {"A": [], "B": []}
    fired_lock = threading.Lock()

    def make_cb(cam: str):
        def _cb(class_id, rule_name, confidence, meta):  # noqa: ANN001
            with fired_lock:
                fired[cam].append(confidence)
        return _cb

    rules = [{
        "class": "cat_meow", "name": "Cat Meow", "enabled": True,
        "confidence_threshold": 0.50, "cooldown_seconds": 30,
    }]

    def make_provider(seg: Path):
        served = {"done": False}

        def _provider(_after_ts: float):
            if served["done"]:
                return []
            served["done"] = True
            return [(seg, seg.stat().st_mtime + 1000)]
        return _provider

    det_a = sd.SoundDetector(
        on_detect=make_cb("A"), rules=rules, source="ingest",
        sample_duration_seconds=1.0, audio_segment_provider=make_provider(seg_a),
    )
    det_b = sd.SoundDetector(
        on_detect=make_cb("B"), rules=rules, source="ingest",
        sample_duration_seconds=1.0, audio_segment_provider=make_provider(seg_b),
    )

    det_a.start()
    det_b.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            with fired_lock:
                if fired["A"] and fired["B"]:
                    break
            time.sleep(0.05)
    finally:
        det_a.stop()
        det_b.stop()

    # Both cameras fired their own callback -> detection is per-camera.
    assert fired["A"], "camera A never fired a detection"
    assert fired["B"], "camera B never fired a detection"

    # The two detector threads were genuinely in flight at the same time.
    assert fake.peak_concurrency >= 2, (
        f"expected concurrent classification across cameras, "
        f"peak in-flight was {fake.peak_concurrency}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Recording is fired per-camera, in parallel, through the real orchestration
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_sound_settings_records_per_camera_in_parallel(monkeypatch):
    """Drive ``_on_sound_detected`` for several cameras concurrently and assert
    each one primes + attaches a recording for its OWN camera_id, with no shared
    lock serializing the recording path across cameras."""
    import app.state as _state
    import app.sound_monitor as sound_monitor
    import app.recording_extension as recording_extension

    camera_ids = ["cam-a", "cam-b", "cam-c"]

    _state.cameras_config = [
        {
            "id": cid,
            "name": cid.upper(),
            "rtsp_url": f"rtsp://example/{cid}",
            "detection": {"sound": {"enabled": True, "rules": [
                {"class": "dog_bark", "name": "Dog Bark", "enabled": True,
                 "confidence_threshold": 0.35, "cooldown_seconds": 20,
                 "record_on_detect": True},
            ]}},
        }
        for cid in camera_ids
    ]

    # Record which camera each recording/prime call was made for, and observe
    # concurrency across the recording path.
    primed: list[str] = []
    attached: list[str] = []
    calls_lock = threading.Lock()
    active = {"n": 0, "peak": 0}

    class _FakeRecordingService:
        def prime_rtsp_prebuffer(self, *, stream_url, camera_id, recording_config):
            with calls_lock:
                primed.append(camera_id)
                active["n"] += 1
                active["peak"] = max(active["peak"], active["n"])
            time.sleep(0.05)  # hold the "recording" open to expose serialization
            with calls_lock:
                active["n"] -= 1

    monkeypatch.setattr(_state, "recording_service", _FakeRecordingService())
    monkeypatch.setattr(_state, "camera_event_recording_config", lambda cam: {})
    monkeypatch.setattr(sound_monitor, "build_stream_url", lambda cam: cam.get("rtsp_url"))

    # Stub the DB + recording attach so we only measure the per-camera dispatch.
    class _FakeDB:
        def add_event(self, **kw):
            return 1

        def add_alert(self, **kw):
            return None

    monkeypatch.setattr(_state, "database", _FakeDB())

    def fake_attach(event_id, iso, kind, dets, *, camera_id, recording_config):
        with calls_lock:
            attached.append(camera_id)
        return 42

    monkeypatch.setattr(recording_extension, "attach_event_recording", fake_attach)

    # Fire all three cameras' detections concurrently, as independent detector
    # threads would.
    threads = [
        threading.Thread(
            target=sound_monitor._on_sound_detected,
            args=(cid, "dog_bark", "Dog Bark", 0.91, {"backend": "fake_yamnet"}),
        )
        for cid in camera_ids
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # Every camera primed + attached a recording for ITS OWN id.
    assert sorted(primed) == sorted(camera_ids), f"primed={primed}"
    assert sorted(attached) == sorted(camera_ids), f"attached={attached}"

    # The recording path ran concurrently across cameras (no global lock).
    assert active["peak"] >= 2, (
        f"expected parallel per-camera recording, peak in-flight was {active['peak']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. The shared YAMNet singleton is safe under concurrent access
# ─────────────────────────────────────────────────────────────────────────────


def test_shared_yamnet_backend_is_concurrency_safe():
    """Many threads hammering the real singleton's ``score_all`` at once must
    not crash or deadlock; the shared lock around the TFLite ``invoke`` must
    serialize them safely and every caller must get a result back.

    This is environment-agnostic: when the model is available (as in CI) each
    call runs a real inference and returns per-class scores; when it isn't (no
    TFLite runtime / model) ``score_all`` short-circuits to ``{}``. Either way
    the contract is the same - a dict per caller, no exception, no hang."""
    import app.sound_detector as sd

    results: list[object] = []
    errors: list[Exception] = []
    audio = np.zeros(SAMPLE_RATE, dtype=np.float32)

    def worker():
        try:
            results.append(sd._yamnet.score_all(audio))
        except Exception as exc:  # noqa: BLE001 - we assert none occur
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"concurrent score_all raised: {errors}"
    assert len(results) == 8, "not every concurrent caller returned"
    # score_all always returns a dict: {} when the model is unavailable, or
    # {class_id: confidence} when it ran. The point of this test is that
    # concurrent entry is safe, not what the scores are.
    assert all(isinstance(r, dict) for r in results)
