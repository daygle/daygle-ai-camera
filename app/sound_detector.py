from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Any, Callable

import numpy as np

logger = logging.getLogger('daygle.sound')

SAMPLE_RATE = 16000

# Each sound class is described by spectral fingerprints:
#   centroid_min/max  – expected spectral centroid range (Hz)
#   band_low/high     – frequency band that must contain most of the energy
#   energy_ratio_min  – minimum fraction of energy that must land in that band
#   zcr_min/max       – zero-crossing rate range (tonal vs. noisy)
#   w_centroid, w_energy, w_zcr – weights that sum to 1.0
SOUND_CLASSES: dict[str, dict[str, Any]] = {
    'cat_meow': {
        'label': 'Cat Meow',
        'description': 'Cat vocalizations and meowing',
        'centroid_min': 500,  'centroid_max': 2500,
        'band_low': 300,      'band_high': 3500,
        'energy_ratio_min': 0.35,
        'zcr_min': 0.01, 'zcr_max': 0.22,
        'w_centroid': 0.30, 'w_energy': 0.55, 'w_zcr': 0.15,
        'default_threshold': 0.60,
        'default_cooldown': 30,
    },
    'dog_bark': {
        'label': 'Dog Bark',
        'description': 'Dog barking or howling',
        'centroid_min': 150,  'centroid_max': 1500,
        'band_low': 100,      'band_high': 2500,
        'energy_ratio_min': 0.45,
        'zcr_min': 0.05, 'zcr_max': 0.35,
        'w_centroid': 0.35, 'w_energy': 0.45, 'w_zcr': 0.20,
        'default_threshold': 0.65,
        'default_cooldown': 20,
    },
    'glass_breaking': {
        'label': 'Glass Breaking',
        'description': 'Breaking glass, ceramic, or window',
        'centroid_min': 2000, 'centroid_max': 7000,
        'band_low': 1500,     'band_high': 8000,
        'energy_ratio_min': 0.40,
        'zcr_min': 0.18, 'zcr_max': 0.55,
        'w_centroid': 0.25, 'w_energy': 0.40, 'w_zcr': 0.35,
        'default_threshold': 0.65,
        'default_cooldown': 10,
    },
    'smoke_alarm': {
        'label': 'Smoke Alarm',
        'description': 'Smoke or carbon monoxide detector beeping',
        'centroid_min': 2700, 'centroid_max': 3600,
        'band_low': 2500,     'band_high': 4000,
        'energy_ratio_min': 0.50,
        'zcr_min': 0.02, 'zcr_max': 0.18,
        'w_centroid': 0.45, 'w_energy': 0.40, 'w_zcr': 0.15,
        'default_threshold': 0.60,
        'default_cooldown': 60,
    },
    'baby_crying': {
        'label': 'Baby Crying',
        'description': 'Infant or young child crying',
        'centroid_min': 350,  'centroid_max': 3000,
        'band_low': 250,      'band_high': 4000,
        'energy_ratio_min': 0.45,
        'zcr_min': 0.05, 'zcr_max': 0.28,
        'w_centroid': 0.30, 'w_energy': 0.50, 'w_zcr': 0.20,
        'default_threshold': 0.60,
        'default_cooldown': 30,
    },
    'doorbell': {
        'label': 'Doorbell',
        'description': 'Door bell or chime ringing',
        'centroid_min': 350,  'centroid_max': 1200,
        'band_low': 300,      'band_high': 1800,
        'energy_ratio_min': 0.55,
        'zcr_min': 0.01, 'zcr_max': 0.14,
        'w_centroid': 0.40, 'w_energy': 0.45, 'w_zcr': 0.15,
        'default_threshold': 0.65,
        'default_cooldown': 15,
    },
    'car_alarm': {
        'label': 'Car Alarm',
        'description': 'Vehicle alarm, horn, or siren',
        'centroid_min': 500,  'centroid_max': 2800,
        'band_low': 300,      'band_high': 3500,
        'energy_ratio_min': 0.45,
        'zcr_min': 0.03, 'zcr_max': 0.25,
        'w_centroid': 0.30, 'w_energy': 0.50, 'w_zcr': 0.20,
        'default_threshold': 0.65,
        'default_cooldown': 60,
    },
    'loud_bang': {
        'label': 'Loud Bang',
        'description': 'Gunshot, explosion, loud impact, or door slam',
        'centroid_min': 200,  'centroid_max': 5000,
        'band_low': 50,       'band_high': 8000,
        'energy_ratio_min': 0.75,
        'zcr_min': 0.00, 'zcr_max': 0.45,
        'w_centroid': 0.10, 'w_energy': 0.80, 'w_zcr': 0.10,
        'default_threshold': 0.70,
        'default_cooldown': 10,
    },
}

DEFAULT_RULES: list[dict[str, Any]] = [
    {
        'class': class_id,
        'name': meta['label'],
        'enabled': class_id == 'cat_meow',
        'confidence_threshold': meta['default_threshold'],
        'cooldown_seconds': meta['default_cooldown'],
    }
    for class_id, meta in SOUND_CLASSES.items()
]


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


def compute_class_confidence(audio: np.ndarray, class_id: str, sample_rate: int = SAMPLE_RATE) -> float:
    """
    Return a [0, 1] confidence score that ``audio`` matches the named sound class.

    Uses three spectral features weighted by the class's fingerprint:
    - spectral centroid (is energy centered in the expected frequency range?)
    - band energy ratio (is enough energy concentrated in the class's band?)
    - zero-crossing rate (is the tonal/noise character correct?)
    """
    cls = SOUND_CLASSES.get(class_id)
    if cls is None:
        return 0.0
    if len(audio) < 256:
        return 0.0

    audio = audio.astype(np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms < 5e-4:
        return 0.0

    fft_mag = np.abs(np.fft.rfft(audio))
    freqs = np.fft.rfftfreq(len(audio), d=1.0 / sample_rate)
    power = fft_mag ** 2
    total_power = float(np.sum(power))
    if total_power < 1e-12:
        return 0.0

    # Spectral centroid score
    centroid = float(np.dot(freqs, power) / total_power)
    c_min, c_max = float(cls['centroid_min']), float(cls['centroid_max'])
    centroid_score = 0.0
    if c_min <= centroid <= c_max:
        mid = (c_min + c_max) / 2.0
        half_span = (c_max - c_min) / 2.0
        centroid_score = max(0.0, 1.0 - abs(centroid - mid) / half_span)

    # Band energy ratio score
    band_mask = (freqs >= cls['band_low']) & (freqs <= cls['band_high'])
    band_ratio = float(np.sum(power[band_mask]) / total_power)
    energy_score = min(1.0, band_ratio / float(cls['energy_ratio_min']))

    # Zero-crossing rate score
    zcr = float(np.mean(np.abs(np.diff(np.sign(audio)))) / 2.0)
    z_min, z_max = float(cls['zcr_min']), float(cls['zcr_max'])
    zcr_score = 0.0
    if z_min <= zcr <= z_max:
        mid_z = (z_min + z_max) / 2.0
        half_z = (z_max - z_min) / 2.0
        zcr_score = max(0.0, 1.0 - abs(zcr - mid_z) / half_z)

    w_c, w_e, w_z = float(cls['w_centroid']), float(cls['w_energy']), float(cls['w_zcr'])
    confidence = w_c * centroid_score + w_e * energy_score + w_z * zcr_score
    return float(min(1.0, max(0.0, confidence)))


class SoundDetector:
    """
    Continuously listens for sounds matching configurable rules and fires a
    callback for each matching rule.

    Supports two audio sources:
    - ``'microphone'``: uses ``sounddevice`` to capture from a local input device
    - ``'rtsp'``: extracts audio from an RTSP camera stream via FFmpeg

    Each rule in ``rules`` is a dict with:
        class                – key into SOUND_CLASSES
        name                 – human-readable name used in alerts
        enabled              – bool
        confidence_threshold – [0, 1] minimum score to fire
        cooldown_seconds     – minimum seconds between consecutive alerts
    """

    def __init__(
        self,
        on_detect: Callable[[str, str, float, dict[str, Any]], None],
        rules: list[dict[str, Any]],
        source: str = 'microphone',
        device_index: int | None = None,
        rtsp_url: str | None = None,
        sample_duration_seconds: float = 1.0,
    ) -> None:
        self.on_detect = on_detect
        self.rules = [r for r in rules if r.get('enabled') and r.get('class') in SOUND_CLASSES]
        self.source = source
        self.device_index = device_index
        self.rtsp_url = rtsp_url
        self.sample_duration_seconds = sample_duration_seconds

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_triggered: dict[str, float] = {}
        self._last_confidences: dict[str, float] = {}
        self._status: str = 'stopped'
        self._status_lock = threading.Lock()

    @property
    def status(self) -> str:
        with self._status_lock:
            return self._status

    def last_confidences(self) -> dict[str, float]:
        with self._status_lock:
            return dict(self._last_confidences)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

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
        self._set_status('stopped')

    def _set_status(self, status: str) -> None:
        with self._status_lock:
            self._status = status

    def _handle_chunk(self, audio: np.ndarray) -> None:
        if not self.rules:
            return
        now = time.time()
        for rule in self.rules:
            class_id = str(rule.get('class') or '')
            confidence = compute_class_confidence(audio, class_id)
            with self._status_lock:
                self._last_confidences[class_id] = confidence
            threshold = float(rule.get('confidence_threshold', 0.60))
            if confidence < threshold:
                continue
            cooldown = float(rule.get('cooldown_seconds', 30))
            last = self._last_triggered.get(class_id, 0.0)
            if now - last < cooldown:
                continue
            self._last_triggered[class_id] = now
            self._set_status(f'detected:{class_id}')
            try:
                self.on_detect(class_id, str(rule.get('name') or SOUND_CLASSES[class_id]['label']), confidence, {'source': self.source})
            except Exception as exc:
                logger.error('Sound detection callback failed for %s: %s', class_id, exc)
        self._set_status('listening')

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
            n = min(len(flat), chunk_samples)
            with buffer_lock:
                nonlocal buffer
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
                enabled_classes = [r['class'] for r in self.rules]
                logger.info('Sound monitor started (microphone, device=%s, classes=%s)', self.device_index, enabled_classes)
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

        enabled_classes = [r['class'] for r in self.rules]
        logger.info('Sound monitor started (RTSP, classes=%s)', enabled_classes)
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


# Backwards-compatible alias
CatMeowDetector = SoundDetector
