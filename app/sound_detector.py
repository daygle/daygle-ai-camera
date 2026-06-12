from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Any, Callable

import numpy as np

logger = logging.getLogger('daygle.sound')

SAMPLE_RATE = 16000
CHUNK_SAMPLES = SAMPLE_RATE  # 1 second windows

# Cat vocalization spectral fingerprint:
# Fundamental ~300-1500 Hz with harmonics up to ~4000 Hz
_CAT_BAND_LOW = 300.0
_CAT_BAND_HIGH = 3500.0
_CENTROID_MIN = 500.0
_CENTROID_MAX = 2500.0
_ENERGY_RATIO_THRESHOLD = 0.35  # fraction of energy that must land in cat band
_ZCR_LOW = 0.01                 # below this = hum/DC offset
_ZCR_HIGH = 0.22                # above this = white noise / hiss


def list_audio_devices() -> list[dict[str, Any]]:
    """Return available audio input devices (requires sounddevice)."""
    try:
        import sounddevice as sd
        result = []
        for i, dev in enumerate(sd.query_devices()):
            if dev['max_input_channels'] > 0:
                result.append({
                    'index': i,
                    'name': dev['name'],
                    'channels': int(dev['max_input_channels']),
                    'default_sample_rate': int(dev['default_samplerate']),
                })
        return result
    except ImportError:
        return []
    except Exception as exc:
        logger.debug('Failed to list audio devices: %s', exc)
        return []


def compute_meow_confidence(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float:
    """Return a [0, 1] confidence score that this audio chunk contains a cat meow."""
    if len(audio) < 256:
        return 0.0

    audio = audio.astype(np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms < 5e-4:  # silence gate ~-66 dBFS
        return 0.0

    fft_mag = np.abs(np.fft.rfft(audio))
    freqs = np.fft.rfftfreq(len(audio), d=1.0 / sample_rate)
    power = fft_mag ** 2
    total_power = float(np.sum(power))
    if total_power < 1e-12:
        return 0.0

    # Spectral centroid
    centroid = float(np.dot(freqs, power) / total_power)
    centroid_score = 0.0
    if _CENTROID_MIN <= centroid <= _CENTROID_MAX:
        mid = (_CENTROID_MIN + _CENTROID_MAX) / 2.0
        half_span = (_CENTROID_MAX - _CENTROID_MIN) / 2.0
        centroid_score = max(0.0, 1.0 - abs(centroid - mid) / half_span)

    # Energy concentration in cat-meow band
    band_mask = (freqs >= _CAT_BAND_LOW) & (freqs <= _CAT_BAND_HIGH)
    band_ratio = float(np.sum(power[band_mask]) / total_power)
    energy_score = min(1.0, band_ratio / _ENERGY_RATIO_THRESHOLD)

    # Zero-crossing rate — cat meows are tonal, not noisy
    zcr = float(np.mean(np.abs(np.diff(np.sign(audio)))) / 2.0)
    zcr_score = 0.0
    if _ZCR_LOW <= zcr <= _ZCR_HIGH:
        # Peak score around ZCR = 0.05–0.08 (typical voiced vocalization)
        if zcr <= 0.07:
            zcr_score = (zcr - _ZCR_LOW) / (0.07 - _ZCR_LOW)
        else:
            zcr_score = max(0.0, 1.0 - (zcr - 0.07) / (_ZCR_HIGH - 0.07))

    confidence = 0.30 * centroid_score + 0.55 * energy_score + 0.15 * zcr_score
    return float(min(1.0, max(0.0, confidence)))


class CatMeowDetector:
    """
    Continuously listens for cat meow sounds and fires a callback when detected.

    Supports two audio sources:
    - ``'microphone'``: uses ``sounddevice`` to capture from a local input device
    - ``'rtsp'``: extracts audio from an RTSP camera stream via FFmpeg
    """

    def __init__(
        self,
        on_detect: Callable[[float, dict[str, Any]], None],
        source: str = 'microphone',
        device_index: int | None = None,
        rtsp_url: str | None = None,
        confidence_threshold: float = 0.60,
        sample_duration_seconds: float = 1.0,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self.on_detect = on_detect
        self.source = source
        self.device_index = device_index
        self.rtsp_url = rtsp_url
        self.confidence_threshold = confidence_threshold
        self.sample_duration_seconds = sample_duration_seconds
        self.cooldown_seconds = cooldown_seconds

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_triggered: float = 0.0
        self._last_confidence: float = 0.0
        self._status: str = 'stopped'
        self._status_lock = threading.Lock()

    @property
    def status(self) -> str:
        with self._status_lock:
            return self._status

    @property
    def last_confidence(self) -> float:
        with self._status_lock:
            return self._last_confidence

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        target = self._run_microphone if self.source == 'microphone' else self._run_rtsp
        self._thread = threading.Thread(target=target, name='sound-monitor', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        with self._status_lock:
            self._status = 'stopped'

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _set_status(self, status: str) -> None:
        with self._status_lock:
            self._status = status

    def _handle_chunk(self, audio: np.ndarray) -> None:
        confidence = compute_meow_confidence(audio)
        with self._status_lock:
            self._last_confidence = confidence
        if confidence < self.confidence_threshold:
            self._set_status('listening')
            return
        now = time.time()
        if now - self._last_triggered < self.cooldown_seconds:
            return
        self._last_triggered = now
        self._set_status('detected')
        try:
            self.on_detect(confidence, {'source': self.source})
        except Exception as exc:
            logger.error('Sound detection callback failed: %s', exc)

    def _run_microphone(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            logger.warning(
                'sounddevice is not installed; microphone sound detection unavailable. '
                'Install it with: pip install sounddevice'
            )
            self._set_status('unavailable: sounddevice not installed')
            return

        chunk_samples = int(SAMPLE_RATE * self.sample_duration_seconds)
        overlap = chunk_samples // 2
        buffer = np.zeros(chunk_samples, dtype=np.float32)
        buffer_lock = threading.Lock()

        def _callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            if status:
                logger.debug('sounddevice status: %s', status)
            flat = indata[:, 0] if indata.ndim > 1 else indata.flatten()
            with buffer_lock:
                nonlocal buffer
                n = min(len(flat), chunk_samples)
                buffer = np.roll(buffer, -n)
                buffer[-n:] = flat[-n:]

        self._set_status('listening')
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype='float32',
                blocksize=overlap,
                device=self.device_index,
                callback=_callback,
            ):
                logger.info(
                    'Sound monitor started (microphone, device=%s, threshold=%.2f)',
                    self.device_index, self.confidence_threshold,
                )
                while not self._stop_event.is_set():
                    self._stop_event.wait(self.sample_duration_seconds / 2)
                    with buffer_lock:
                        chunk = buffer.copy()
                    self._handle_chunk(chunk)
        except Exception as exc:
            logger.error('Sound monitor microphone error: %s', exc)
            self._set_status(f'error: {exc}')

    def _run_rtsp(self) -> None:
        if not self.rtsp_url:
            logger.warning('Sound monitor: RTSP source selected but no URL configured')
            self._set_status('unavailable: no RTSP URL')
            return

        chunk_samples = int(SAMPLE_RATE * self.sample_duration_seconds)
        overlap_samples = chunk_samples // 2
        bytes_per_sample = 2  # s16le

        cmd = [
            'ffmpeg', '-loglevel', 'error',
            '-rtsp_transport', 'tcp',
            '-i', self.rtsp_url,
            '-vn',
            '-acodec', 'pcm_s16le',
            '-ar', str(SAMPLE_RATE),
            '-ac', '1',
            '-f', 's16le',
            'pipe:1',
        ]

        logger.info(
            'Sound monitor started (RTSP, threshold=%.2f)',
            self.confidence_threshold,
        )
        self._set_status('listening')

        while not self._stop_event.is_set():
            proc: subprocess.Popen | None = None
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                raw_buf = b''
                need_bytes = chunk_samples * bytes_per_sample
                advance_bytes = overlap_samples * bytes_per_sample

                while not self._stop_event.is_set() and proc.poll() is None:
                    chunk = proc.stdout.read(need_bytes - len(raw_buf))
                    if not chunk:
                        break
                    raw_buf += chunk
                    if len(raw_buf) >= need_bytes:
                        audio = (
                            np.frombuffer(raw_buf[:need_bytes], dtype=np.int16)
                            .astype(np.float32) / 32768.0
                        )
                        raw_buf = raw_buf[advance_bytes:]
                        self._handle_chunk(audio)
            except Exception as exc:
                logger.error('Sound monitor RTSP error: %s', exc)
                self._set_status(f'error: {exc}')
            finally:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            if not self._stop_event.is_set():
                self._stop_event.wait(5.0)
