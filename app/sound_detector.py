from __future__ import annotations

import csv
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

logger = logging.getLogger('daygle.sound')

SAMPLE_RATE = 16000

# Suppress TensorFlow startup noise and disable GPU (not needed for audio)
os.environ.setdefault('CUDA_VISIBLE_DEVICES', '')
os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')

# Cache the downloaded YAMNet model alongside other app models
_MODELS_DIR = Path(__file__).parent / 'models'
_MODELS_DIR.mkdir(exist_ok=True)
os.environ.setdefault('TFHUB_CACHE_DIR', str(_MODELS_DIR / 'yamnet_cache'))

_YAMNET_HUB_URL = 'https://tfhub.dev/google/yamnet/1'


# ─── Sound class catalogue ────────────────────────────────────────────────────
#
# yamnet_terms: matched against AudioSet display names using word-boundary
#   matching — a YAMNet class is included when ANY term appears as a whole
#   word in its name (e.g. 'cat' matches 'Cat' but not 'Cattle, bovinae').
#   YAMNet default_threshold values are lower than spectral ones because
#   YAMNet outputs calibrated probabilities (0.0–1.0 from a trained model)
#   rather than the hand-crafted heuristic scores used by the spectral fallback.
#
# Spectral fallback fields (centroid_*, band_*, zcr_*, w_*) are used only
# when tensorflow-hub is not installed.

SOUND_CLASSES: dict[str, dict[str, Any]] = {
    'cat_meow': {
        'label': 'Cat Meow',
        'description': 'Cat vocalizations and meowing',
        'yamnet_terms': ['cat', 'meow', 'purr', 'caterwaul'],
        'centroid_min': 500,  'centroid_max': 2500,
        'band_low': 300,      'band_high': 3500,
        'energy_ratio_min': 0.35,
        'zcr_min': 0.01, 'zcr_max': 0.22,
        'w_centroid': 0.30, 'w_energy': 0.55, 'w_zcr': 0.15,
        'default_threshold': 0.50,
        'default_cooldown': 30,
    },
    'dog_bark': {
        'label': 'Dog Bark',
        'description': 'Dog barking or howling',
        'yamnet_terms': ['dog', 'bark', 'bow-wow', 'howl', 'yip', 'growling'],
        'centroid_min': 150,  'centroid_max': 1500,
        'band_low': 100,      'band_high': 2500,
        'energy_ratio_min': 0.45,
        'zcr_min': 0.05, 'zcr_max': 0.35,
        'w_centroid': 0.35, 'w_energy': 0.45, 'w_zcr': 0.20,
        'default_threshold': 0.35,
        'default_cooldown': 20,
    },
    'glass_breaking': {
        'label': 'Glass Breaking',
        'description': 'Breaking glass, ceramic, or window',
        'yamnet_terms': ['glass', 'shatter', 'smash'],
        'centroid_min': 2000, 'centroid_max': 7000,
        'band_low': 1500,     'band_high': 8000,
        'energy_ratio_min': 0.40,
        'zcr_min': 0.18, 'zcr_max': 0.55,
        'w_centroid': 0.25, 'w_energy': 0.40, 'w_zcr': 0.35,
        'default_threshold': 0.25,
        'default_cooldown': 10,
    },
    'smoke_alarm': {
        'label': 'Smoke Alarm',
        'description': 'Smoke or carbon monoxide detector beeping',
        'yamnet_terms': ['smoke detector', 'smoke alarm', 'fire alarm', 'carbon monoxide'],
        'centroid_min': 2700, 'centroid_max': 3600,
        'band_low': 2500,     'band_high': 4000,
        'energy_ratio_min': 0.50,
        'zcr_min': 0.02, 'zcr_max': 0.18,
        'w_centroid': 0.45, 'w_energy': 0.40, 'w_zcr': 0.15,
        'default_threshold': 0.25,
        'default_cooldown': 60,
    },
    'baby_crying': {
        'label': 'Baby Crying',
        'description': 'Infant or young child crying',
        'yamnet_terms': ['baby cry', 'infant cry', 'crying, sobbing'],
        'centroid_min': 350,  'centroid_max': 3000,
        'band_low': 250,      'band_high': 4000,
        'energy_ratio_min': 0.45,
        'zcr_min': 0.05, 'zcr_max': 0.28,
        'w_centroid': 0.30, 'w_energy': 0.50, 'w_zcr': 0.20,
        'default_threshold': 0.30,
        'default_cooldown': 30,
    },
    'doorbell': {
        'label': 'Doorbell',
        'description': 'Door bell or chime ringing',
        'yamnet_terms': ['doorbell'],
        'centroid_min': 350,  'centroid_max': 1200,
        'band_low': 300,      'band_high': 1800,
        'energy_ratio_min': 0.55,
        'zcr_min': 0.01, 'zcr_max': 0.14,
        'w_centroid': 0.40, 'w_energy': 0.45, 'w_zcr': 0.15,
        'default_threshold': 0.30,
        'default_cooldown': 15,
    },
    'car_alarm': {
        'label': 'Car Alarm',
        'description': 'Vehicle alarm, horn, or siren',
        'yamnet_terms': ['car alarm', 'vehicle horn', 'car horn', 'honking', 'siren'],
        'centroid_min': 500,  'centroid_max': 2800,
        'band_low': 300,      'band_high': 3500,
        'energy_ratio_min': 0.45,
        'zcr_min': 0.03, 'zcr_max': 0.25,
        'w_centroid': 0.30, 'w_energy': 0.50, 'w_zcr': 0.20,
        'default_threshold': 0.30,
        'default_cooldown': 60,
    },
    'loud_bang': {
        'label': 'Loud Bang',
        'description': 'Gunshot, explosion, loud impact, or door slam',
        'yamnet_terms': ['gunshot', 'gunfire', 'explosion', 'boom', 'bang', 'slam', 'blast'],
        'centroid_min': 200,  'centroid_max': 5000,
        'band_low': 50,       'band_high': 8000,
        'energy_ratio_min': 0.75,
        'zcr_min': 0.00, 'zcr_max': 0.45,
        'w_centroid': 0.10, 'w_energy': 0.80, 'w_zcr': 0.10,
        'default_threshold': 0.25,
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


# ─── YAMNet backend ───────────────────────────────────────────────────────────

class _YamnetBackend:
    """
    Lazy-loaded singleton wrapping the YAMNet TF Hub model.

    On the first call to ``score_all()`` the model is downloaded from TF Hub
    (cached to disk on subsequent runs) and the AudioSet class list is indexed
    against our sound class definitions. Falls back gracefully when
    ``tensorflow-hub`` is not installed.
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._class_indices: dict[str, list[int]] = {}
        self._lock = threading.Lock()
        self._available: bool | None = None  # None = not yet attempted

    # ------------------------------------------------------------------
    def _load(self) -> bool:
        with self._lock:
            if self._available is not None:
                return self._available
            # Probe TensorFlow in a subprocess before importing it in-process.
            # On CPUs without AVX2 support the prebuilt TF wheels raise SIGILL
            # inside native libraries, which would kill the entire main process.
            # Running the probe in a child process contains the crash.
            try:
                probe = subprocess.run(
                    [sys.executable, '-c', 'import tensorflow'],
                    capture_output=True,
                    timeout=60,
                    env={**os.environ, 'TF_CPP_MIN_LOG_LEVEL': '3', 'CUDA_VISIBLE_DEVICES': ''},
                )
                if probe.returncode != 0:
                    logger.warning(
                        'TensorFlow unavailable on this CPU (probe exited %d) — '
                        'using spectral fallback for sound detection. '
                        'Install tensorflow-cpu built for your CPU architecture to enable YAMNet.',
                        probe.returncode,
                    )
                    self._available = False
                    return False
            except Exception as exc:
                logger.warning('TensorFlow CPU probe failed: %s — using spectral fallback.', exc)
                self._available = False
                return False
            try:
                import tensorflow_hub as hub  # type: ignore[import]
                logger.info('Loading YAMNet from TF Hub (first run downloads ~13 MB)…')
                model = hub.load(_YAMNET_HUB_URL)

                # Read the AudioSet class map that ships with the model
                class_map_path = model.class_map_path().numpy().decode('utf-8')
                with open(class_map_path, newline='') as f:
                    class_names = [row['display_name'] for row in csv.DictReader(f)]

                # Build index lists: for each of our classes, find every
                # AudioSet class whose display_name matches one of our terms
                # using word-boundary matching to prevent false positives
                # (e.g. 'cat' must not match 'Cattle, bovinae').
                indices: dict[str, list[int]] = {}
                for class_id, meta in SOUND_CLASSES.items():
                    patterns = [
                        re.compile(r'\b' + re.escape(t.lower()) + r'\b')
                        for t in meta.get('yamnet_terms', [])
                    ]
                    matched = [
                        i for i, name in enumerate(class_names)
                        if any(pat.search(name.lower()) for pat in patterns)
                    ]
                    indices[class_id] = matched
                    logger.debug(
                        'YAMNet %s → %d AudioSet classes: %s',
                        class_id, len(matched),
                        [class_names[i] for i in matched],
                    )

                self._model = model
                self._class_indices = indices
                self._available = True
                logger.info(
                    'YAMNet ready — classifying against %d AudioSet classes',
                    len(class_names),
                )
            except Exception as exc:
                logger.warning(
                    'YAMNet unavailable (pip install tensorflow-hub to enable): %s', exc,
                )
                self._available = False
        return bool(self._available)

    # ------------------------------------------------------------------
    def score_all(self, audio: np.ndarray) -> dict[str, float]:
        """
        Run YAMNet on ``audio`` and return ``{class_id: confidence}`` for every
        sound class.  Runs a single forward pass so all classes are scored with
        one model call.  Returns ``{}`` if YAMNet is unavailable.
        """
        if not self._load():
            return {}
        try:
            waveform = audio.astype(np.float32)
            if waveform.ndim > 1:
                waveform = waveform.mean(axis=1)
            # scores shape: [num_frames, 521] — YAMNet slices audio into
            # 0.96 s patches with 50 % overlap; we average across frames.
            scores, _, _ = self._model(waveform)
            mean_scores: np.ndarray = scores.numpy().mean(axis=0)
            result: dict[str, float] = {}
            for class_id, idxs in self._class_indices.items():
                result[class_id] = float(mean_scores[idxs].max()) if idxs else 0.0
            return result
        except Exception as exc:
            logger.debug('YAMNet inference error: %s', exc)
            return {}

    # ------------------------------------------------------------------
    def preload(self) -> None:
        """Trigger model loading in the current thread (call from a background thread)."""
        self._load()

    @property
    def available(self) -> bool | None:
        """True / False once probed, None while not yet attempted."""
        return self._available

    @property
    def backend_name(self) -> str:
        if self._available is True:
            return 'yamnet'
        if self._available is False:
            return 'spectral'
        return 'loading'


# Module-level singleton shared across all SoundDetector instances
_yamnet = _YamnetBackend()


# ─── Spectral heuristic (fallback) ───────────────────────────────────────────

def compute_class_confidence(audio: np.ndarray, class_id: str, sample_rate: int = SAMPLE_RATE) -> float:
    """
    Spectral-heuristic confidence score for ``audio`` matching ``class_id``.

    Used as a fallback when YAMNet is unavailable.  Combines three features:
    spectral centroid, band energy ratio, and zero-crossing rate — each
    weighted by the class fingerprint.
    """
    cls = SOUND_CLASSES.get(class_id)
    if cls is None or len(audio) < 256:
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

    centroid = float(np.dot(freqs, power) / total_power)
    c_min, c_max = float(cls['centroid_min']), float(cls['centroid_max'])
    centroid_score = 0.0
    if c_min <= centroid <= c_max:
        mid = (c_min + c_max) / 2.0
        half_span = (c_max - c_min) / 2.0
        centroid_score = max(0.0, 1.0 - abs(centroid - mid) / half_span)

    band_mask = (freqs >= cls['band_low']) & (freqs <= cls['band_high'])
    band_ratio = float(np.sum(power[band_mask]) / total_power)
    energy_score = min(1.0, band_ratio / float(cls['energy_ratio_min']))

    zcr = float(np.mean(np.abs(np.diff(np.sign(audio)))) / 2.0)
    z_min, z_max = float(cls['zcr_min']), float(cls['zcr_max'])
    zcr_score = 0.0
    if z_min <= zcr <= z_max:
        mid_z = (z_min + z_max) / 2.0
        half_z = (z_max - z_min) / 2.0
        zcr_score = max(0.0, 1.0 - abs(zcr - mid_z) / half_z)

    w_c = float(cls['w_centroid'])
    w_e = float(cls['w_energy'])
    w_z = float(cls['w_zcr'])
    return float(min(1.0, max(0.0, w_c * centroid_score + w_e * energy_score + w_z * zcr_score)))


# ─── Audio device enumeration ─────────────────────────────────────────────────

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


# ─── Sound detector ───────────────────────────────────────────────────────────

class SoundDetector:
    """
    Continuously listens for sounds matching configurable rules and fires a
    callback for each matching rule.

    Classification pipeline:
      1. YAMNet (Google's pretrained neural audio classifier, 521 AudioSet
         classes) — runs one forward pass per audio chunk and extracts scores
         for all our sound classes in a single call.
      2. Spectral heuristic fallback — used when ``tensorflow-hub`` is not
         installed; scores each class individually using FFT-based features.

    Supports two audio sources:
      - ``'microphone'``: captures via ``sounddevice``
      - ``'rtsp'``: pipes audio from an RTSP stream through FFmpeg

    Each rule dict:
        class                – key into SOUND_CLASSES
        name                 – human-readable label used in alerts
        enabled              – bool
        confidence_threshold – minimum score to fire (YAMNet: 0.25–0.40 typical)
        cooldown_seconds     – minimum seconds between consecutive alerts for this class
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

    # ------------------------------------------------------------------
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

    @property
    def backend(self) -> str:
        return _yamnet.backend_name

    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    def _set_status(self, status: str) -> None:
        with self._status_lock:
            self._status = status

    def _handle_chunk(self, audio: np.ndarray) -> None:
        if not self.rules:
            return
        now = time.time()

        # Run YAMNet once for all classes (single forward pass).
        # Falls back to per-class spectral scoring when YAMNet is unavailable.
        yamnet_scores = _yamnet.score_all(audio)
        using_yamnet = bool(yamnet_scores)

        for rule in self.rules:
            class_id = str(rule.get('class') or '')
            if using_yamnet:
                confidence = yamnet_scores.get(class_id, 0.0)
            else:
                confidence = compute_class_confidence(audio, class_id)

            with self._status_lock:
                self._last_confidences[class_id] = confidence

            threshold = float(rule.get('confidence_threshold', 0.35))
            if confidence < threshold:
                continue
            cooldown = float(rule.get('cooldown_seconds', 30))
            last = self._last_triggered.get(class_id, 0.0)
            if now - last < cooldown:
                continue

            self._last_triggered[class_id] = now
            self._set_status(f'detected:{class_id}')
            try:
                self.on_detect(
                    class_id,
                    str(rule.get('name') or SOUND_CLASSES[class_id]['label']),
                    confidence,
                    {
                        'source': self.source,
                        'backend': 'yamnet' if using_yamnet else 'spectral',
                    },
                )
            except Exception as exc:
                logger.error('Sound detection callback failed for %s: %s', class_id, exc)
        self._set_status('listening')

    # ------------------------------------------------------------------
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

        # Preload YAMNet while we wait for the first audio callback so the
        # first real chunk is not delayed by model initialisation.
        preload_thread = threading.Thread(target=_yamnet.preload, daemon=True, name='yamnet-preload')
        preload_thread.start()

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
        enabled_classes = [r['class'] for r in self.rules]
        logger.info(
            'Sound monitor started (microphone, device=%s, classes=%s)',
            self.device_index, enabled_classes,
        )
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype='float32',
                blocksize=overlap,
                device=self.device_index,
                callback=_callback,
            ):
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

        # Preload YAMNet before the FFmpeg pipe starts reading
        preload_thread = threading.Thread(target=_yamnet.preload, daemon=True, name='yamnet-preload')
        preload_thread.start()

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
