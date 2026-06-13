from __future__ import annotations

import csv
import logging
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlretrieve

import numpy as np

logger = logging.getLogger('daygle.sound')

SAMPLE_RATE = 16000

# Store the CPU-only YAMNet TFLite assets alongside other app models.
_MODELS_DIR = Path(__file__).resolve().parents[1] / 'models'
_MODELS_DIR.mkdir(exist_ok=True)
_YAMNET_TFLITE_PATH = _MODELS_DIR / 'yamnet.tflite'
_YAMNET_CLASS_MAP_PATH = _MODELS_DIR / 'yamnet_class_map.csv'
_YAMNET_TFLITE_URL = 'https://tfhub.dev/google/lite-model/yamnet/tflite/1?lite-format=tflite'
_YAMNET_CLASS_MAP_URL = 'https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv'


# ─── Sound class catalogue ────────────────────────────────────────────────────
#
# yamnet_terms: matched against AudioSet display names using word-boundary
#   matching - a YAMNet class is included when ANY term appears as a whole
#   word in its name (e.g. 'cat' matches 'Cat' but not 'Cattle, bovinae').
#   YAMNet default_threshold values are calibrated around neural model
#   probabilities (0.0-1.0), not hand-written audio heuristics.

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
    """Lazy-loaded CPU-only YAMNet TensorFlow Lite backend."""

    def __init__(self) -> None:
        self._model: Any = None
        self._input_details: list[dict[str, Any]] = []
        self._output_details: list[dict[str, Any]] = []
        self._class_indices: dict[str, list[int]] = {}
        self._lock = threading.Lock()
        self._available: bool | None = None  # None = not yet attempted
        self._unavailable_reason: str | None = None

    # ------------------------------------------------------------------
    @staticmethod
    def _interpreter_class() -> Any:
        try:
            from ai_edge_litert.interpreter import Interpreter  # type: ignore[import]
            return Interpreter
        except Exception:
            pass
        try:
            from tflite_runtime.interpreter import Interpreter  # type: ignore[import]
            return Interpreter
        except Exception as exc:
            raise RuntimeError(
                'TensorFlow Lite runtime is not installed. Install ai-edge-litert or tflite-runtime.'
            ) from exc

    @staticmethod
    def _ensure_asset(path: Path, url: str, label: str) -> None:
        if path.exists() and path.stat().st_size > 0:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + '.download')
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        logger.info('Downloading %s to %s', label, path)
        urlretrieve(url, tmp_path)  # noqa: S310 - trusted model/class-map URLs controlled by the app
        tmp_path.replace(path)

    @staticmethod
    def _load_class_names(path: Path) -> list[str]:
        with open(path, newline='', encoding='utf-8') as f:
            rows = sorted(csv.DictReader(f), key=lambda row: int(row['index']))
            return [row['display_name'] for row in rows]

    @staticmethod
    def _build_class_indices(class_names: list[str]) -> dict[str, list[int]]:
        indices: dict[str, list[int]] = {}
        for class_id, meta in SOUND_CLASSES.items():
            patterns = [
                re.compile(r'\b' + re.escape(t.lower()) + r'\b')
                for t in meta.get('yamnet_terms', [])
            ]
            indices[class_id] = [
                i for i, name in enumerate(class_names)
                if any(pat.search(name.lower()) for pat in patterns)
            ]
            logger.debug(
                'YAMNet TFLite %s maps to %d AudioSet classes: %s',
                class_id, len(indices[class_id]), [class_names[i] for i in indices[class_id]],
            )
        return indices

    def _load(self) -> bool:
        with self._lock:
            if self._available is not None:
                return self._available
            try:
                Interpreter = self._interpreter_class()
                self._ensure_asset(_YAMNET_TFLITE_PATH, _YAMNET_TFLITE_URL, 'YAMNet TFLite model')
                self._ensure_asset(_YAMNET_CLASS_MAP_PATH, _YAMNET_CLASS_MAP_URL, 'YAMNet class map')
                class_names = self._load_class_names(_YAMNET_CLASS_MAP_PATH)
                interpreter = Interpreter(model_path=str(_YAMNET_TFLITE_PATH), num_threads=1)
                interpreter.allocate_tensors()

                self._model = interpreter
                self._input_details = interpreter.get_input_details()
                self._output_details = interpreter.get_output_details()
                self._class_indices = self._build_class_indices(class_names)
                self._available = True
                self._unavailable_reason = None
                logger.info('YAMNet TFLite ready - classifying against %d AudioSet classes', len(class_names))
            except Exception as exc:
                self._unavailable_reason = f'YAMNet TFLite unavailable: {exc}'
                logger.warning('YAMNet TFLite unavailable: %s', exc)
                self._available = False
        return bool(self._available)

    # ------------------------------------------------------------------
    def score_all(self, audio: np.ndarray) -> dict[str, float]:
        """
        Run YAMNet TFLite on ``audio`` and return ``{class_id: confidence}``
        for every configured sound class. Returns ``{}`` if YAMNet is unavailable.
        """
        if not self._load():
            return {}
        try:
            if self._model is None or not self._input_details:
                return {}
            waveform = audio.astype(np.float32)
            if waveform.ndim > 1:
                waveform = waveform.mean(axis=1)

            input_detail = self._input_details[0]
            input_index = int(input_detail['index'])
            raw_shape = input_detail.get('shape')
            raw_signature = input_detail.get('shape_signature')
            input_shape = np.array(raw_shape if raw_shape is not None else [], dtype=np.int32)
            input_signature = np.array(raw_signature if raw_signature is not None else [], dtype=np.int32)
            target_len = int(input_shape[-1]) if input_shape.size else len(waveform)
            if target_len <= 0:
                target_len = len(waveform)
            if input_signature.size and int(input_signature[-1]) == -1:
                target_len = max(len(waveform), 1)
                if input_signature.size == 1:
                    new_shape = [target_len]
                else:
                    new_shape = [int(v) if int(v) > 0 else 1 for v in input_signature]
                    new_shape[-1] = target_len
                self._model.resize_tensor_input(input_index, new_shape, strict=False)
                self._model.allocate_tensors()
                self._input_details = self._model.get_input_details()
                self._output_details = self._model.get_output_details()

            if len(waveform) < target_len:
                waveform = np.pad(waveform, (0, target_len - len(waveform)))
            elif len(waveform) > target_len:
                waveform = waveform[:target_len]

            expected_shape = tuple(int(v) for v in self._input_details[0]['shape'])
            self._model.set_tensor(input_index, waveform.reshape(expected_shape).astype(np.float32))
            self._model.invoke()

            scores_array: np.ndarray | None = None
            for output in self._output_details:
                arr = np.asarray(self._model.get_tensor(int(output['index'])))
                if arr.ndim >= 1 and int(arr.shape[-1]) >= 521:
                    scores_array = arr
                    break
            if scores_array is None:
                raise RuntimeError('YAMNet TFLite scores output was not found.')

            if scores_array.ndim == 1:
                mean_scores = scores_array
            else:
                mean_scores = scores_array.reshape(-1, scores_array.shape[-1]).mean(axis=0)
            result: dict[str, float] = {}
            for class_id, idxs in self._class_indices.items():
                result[class_id] = float(mean_scores[idxs].max()) if idxs else 0.0
            return result
        except Exception as exc:
            logger.debug('YAMNet TFLite inference error: %s', exc)
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
            return 'yamnet_tflite'
        if self._available is False:
            return 'unavailable'
        return 'loading'

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason


# Module-level singleton shared across all SoundDetector instances
_yamnet = _YamnetBackend()


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
      1. YAMNet TFLite (Google's pretrained neural audio classifier, 521
         AudioSet classes) runs on CPU and extracts scores for all configured
         sound classes in a single call.
      2. If the TFLite runtime or model is unavailable, no sound detections are
         emitted. There is intentionally no heuristic fallback.

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

    @property
    def backend_reason(self) -> str | None:
        return _yamnet.unavailable_reason

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

        # Run YAMNet TFLite once for all classes. If the CPU-only runtime or
        # model is unavailable, do not emit detections from a noisy fallback.
        yamnet_scores = _yamnet.score_all(audio)
        if not yamnet_scores:
            return

        for rule in self.rules:
            if not self._rule_active_now(rule):
                continue
            class_id = str(rule.get('class') or '')
            confidence = yamnet_scores.get(class_id, 0.0)

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
                        'backend': _yamnet.backend_name,
                    },
                )
            except Exception as exc:
                logger.error('Sound detection callback failed for %s: %s', class_id, exc)
        self._set_status('listening')

    @staticmethod
    def _rule_active_now(rule: dict[str, Any]) -> bool:
        start = rule.get('active_start')
        end = rule.get('active_end')
        if not start or not end:
            return True
        now = datetime.now().strftime('%H:%M')
        start_text = str(start)
        end_text = str(end)
        if start_text <= end_text:
            return start_text <= now <= end_text
        return now >= start_text or now <= end_text

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
