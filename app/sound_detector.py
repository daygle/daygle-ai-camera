from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import time

from app.alerts import _now_hm_in_admin_tz
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlretrieve

import numpy as np

logger = logging.getLogger('daygle.sound')

SAMPLE_RATE = 16000

# Raised by app.recordings.audio_segments_after() when the per-camera ingest
# has learned the RTSP stream carries no audio track. Mirrored here instead
# of imported to avoid a circular dependency; the sound detector matches the
# substring rather than the exception class so consumers in any process can
# still identify this 'unavailable' state from the message alone.
NO_AUDIO_EXC_PREFIX = 'no audio track in stream'
NO_AUDIO_STATUS = f'unavailable: {NO_AUDIO_EXC_PREFIX}'

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

# Pre-compiled regex patterns for matching YAMNet AudioSet class names.
# These are derived solely from SOUND_CLASSES (immutable at module level)
# so they are built once and reused by every ``_build_class_indices`` call.
_YAMNET_MATCH_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    class_id: [
        re.compile(r'\b' + re.escape(t.lower()) + r'\b')
        for t in meta.get('yamnet_terms', [])
    ]
    for class_id, meta in SOUND_CLASSES.items()
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

    _METADATA_PATH = _MODELS_DIR / 'yamnet-metadata.json'
    _MANIFEST_URL = 'https://raw.githubusercontent.com/daygle/daygle-ai-camera/main/models/yamnet-manifest.json'

    def __init__(self) -> None:
        self._model: Any = None
        self._input_details: list[dict[str, Any]] = []
        self._output_details: list[dict[str, Any]] = []
        self._class_indices: dict[str, list[int]] = {}
        self._lock = threading.Lock()
        self._available: bool | None = None  # None = not yet attempted
        self._unavailable_reason: str | None = None
        self._dynamic_target_len: int | None = None
        self._installed_version: str | None = None
        self._installed_sha256: str | None = None

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
        try:
            urlretrieve(url, tmp_path)  # noqa: S310 - trusted model/class-map URLs controlled by the app
            tmp_path.replace(path)
        finally:
            # A failed transfer must not leave stale bytes that a later
            # retry could mistake for a completed download.
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _read_metadata() -> dict[str, Any]:
        p = _YamnetBackend._METADATA_PATH
        if p.exists():
            try:
                return json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                pass
        return {}

    @staticmethod
    def _write_metadata(data: dict[str, Any]) -> None:
        _YamnetBackend._METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        _YamnetBackend._METADATA_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')

    def _save_installed_version(self) -> None:
        """Record the installed model version and SHA-256 after a successful load."""
        if not _YAMNET_TFLITE_PATH.exists():
            return
        sha = self._sha256_file(_YAMNET_TFLITE_PATH)
        meta = self._read_metadata()
        meta['installed_sha256'] = sha
        meta['installed_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self._write_metadata(meta)
        self._installed_sha256 = sha

    def installed_info(self) -> dict[str, Any]:
        """Return information about the currently installed YAMNet model."""
        meta = self._read_metadata()
        sha = meta.get('installed_sha256', self._installed_sha256)
        if not sha and _YAMNET_TFLITE_PATH.exists():
            sha = self._sha256_file(_YAMNET_TFLITE_PATH)
        return {
            'available': bool(self._available),
            'sha256': sha,
            'installed_at': meta.get('installed_at'),
            'model_size': _YAMNET_TFLITE_PATH.stat().st_size if _YAMNET_TFLITE_PATH.exists() else 0,
            'class_map_size': _YAMNET_CLASS_MAP_PATH.stat().st_size if _YAMNET_CLASS_MAP_PATH.exists() else 0,
        }

    def check_for_update(self) -> dict[str, Any]:
        """Check if a newer YAMNet model is available by re-downloading and comparing SHA-256."""
        current_sha = self._read_metadata().get('installed_sha256', '')
        if not current_sha and _YAMNET_TFLITE_PATH.exists():
            current_sha = self._sha256_file(_YAMNET_TFLITE_PATH)
        tmp_path = _YAMNET_TFLITE_PATH.with_suffix('.tflite.check')
        try:
            tmp_path.unlink(missing_ok=True)
            urlretrieve(_YAMNET_TFLITE_URL, tmp_path)  # noqa: S310
            new_sha = self._sha256_file(tmp_path)
            new_size = tmp_path.stat().st_size
        except Exception as exc:
            logger.warning('YAMNet update check failed (%s).', type(exc).__name__)
            return {'update_available': False, 'error': 'Unable to check for model updates.'}
        finally:
            # A failed download, hash, or stat must not leave a misleading
            # check artifact beside the installed model. In particular, a
            # later check must not accidentally treat stale bytes as fresh.
            tmp_path.unlink(missing_ok=True)
        update_available = bool(current_sha and new_sha and current_sha != new_sha)
        return {
            'update_available': update_available,
            'current_sha256': current_sha,
            'latest_sha256': new_sha,
            'latest_size': new_size,
        }

    def reload(self) -> bool:
        """Thread-safe model reload. Downloads fresh assets and reinitialises the interpreter.

        Downloads to temp files first, then swaps atomically so a failed
        download never leaves the model unavailable.
        """
        with self._lock:
            had_working_model = self._available is True and self._model is not None
            previous_reason = self._unavailable_reason
            previous_metadata = self._read_metadata()
            asset_rollback_failed = False
            reload_failed_after_commit = False
            assets_committed = False
            try:
                Interpreter = self._interpreter_class()
                # Download to temp files first
                tmp_tflite = _YAMNET_TFLITE_PATH.with_suffix('.tflite.update')
                tmp_csv = _YAMNET_CLASS_MAP_PATH.with_suffix('.csv.update')
                rollback_tflite = _YAMNET_TFLITE_PATH.with_suffix('.tflite.rollback')
                rollback_csv = _YAMNET_CLASS_MAP_PATH.with_suffix('.csv.rollback')
                tflite_replaced = False
                csv_replaced = False
                had_tflite = _YAMNET_TFLITE_PATH.exists()
                had_csv = _YAMNET_CLASS_MAP_PATH.exists()
                try:
                    self._ensure_asset(tmp_tflite, _YAMNET_TFLITE_URL, 'YAMNet TFLite model (update)')
                    self._ensure_asset(tmp_csv, _YAMNET_CLASS_MAP_URL, 'YAMNet class map (update)')
                    class_names = self._load_class_names(tmp_csv)
                    interpreter = Interpreter(model_path=str(tmp_tflite), num_threads=1)
                    interpreter.allocate_tensors()
                    # Keep rollback copies while committing both assets. A
                    # successful replacement of the model followed by a
                    # failed class-map replacement must never leave mixed
                    # model/label versions on disk.
                    rollback_tflite.unlink(missing_ok=True)
                    rollback_csv.unlink(missing_ok=True)
                    if had_tflite:
                        shutil.copy2(_YAMNET_TFLITE_PATH, rollback_tflite)
                    if had_csv:
                        shutil.copy2(_YAMNET_CLASS_MAP_PATH, rollback_csv)
                    tmp_tflite.replace(_YAMNET_TFLITE_PATH)
                    tflite_replaced = True
                    tmp_csv.replace(_YAMNET_CLASS_MAP_PATH)
                    csv_replaced = True
                    assets_committed = True
                except Exception:
                    # Restore the previous pair if either commit step failed.
                    # ``replace`` is used rather than unlink+rename so readers
                    # never observe an intentional empty-file gap. If a restore
                    # itself fails, retain that backup for operator recovery and
                    # do not mask the original reload error.
                    rollback_failed = False
                    if tflite_replaced:
                        try:
                            if had_tflite and rollback_tflite.exists():
                                rollback_tflite.replace(_YAMNET_TFLITE_PATH)
                            else:
                                _YAMNET_TFLITE_PATH.unlink(missing_ok=True)
                        except OSError as restore_exc:
                            rollback_failed = True
                            logger.error('Could not restore YAMNet TFLite asset after failed reload: %s', restore_exc)
                    if csv_replaced:
                        try:
                            if had_csv and rollback_csv.exists():
                                rollback_csv.replace(_YAMNET_CLASS_MAP_PATH)
                            else:
                                _YAMNET_CLASS_MAP_PATH.unlink(missing_ok=True)
                        except OSError as restore_exc:
                            rollback_failed = True
                            logger.error('Could not restore YAMNet class map after failed reload: %s', restore_exc)
                    tmp_tflite.unlink(missing_ok=True)
                    tmp_csv.unlink(missing_ok=True)
                    if not rollback_failed:
                        rollback_tflite.unlink(missing_ok=True)
                        rollback_csv.unlink(missing_ok=True)
                    else:
                        asset_rollback_failed = True
                    raise
                self._model = interpreter
                self._input_details = interpreter.get_input_details()
                self._output_details = interpreter.get_output_details()
                self._class_indices = self._build_class_indices(class_names)
                self._available = True
                self._unavailable_reason = None
                self._dynamic_target_len = None
                self._save_installed_version()
                # The in-memory model and metadata are now committed; remove
                # rollback copies only after every post-swap initialization step
                # succeeds. This keeps recovery possible if metadata writing or
                # class-index setup fails after the two asset replacements.
                rollback_tflite.unlink(missing_ok=True)
                rollback_csv.unlink(missing_ok=True)
                logger.info('YAMNet TFLite reloaded successfully')
                return True
            except Exception as exc:
                failure_reason = f'YAMNet TFLite reload failed: {exc}'
                logger.warning('%s', failure_reason)
                if assets_committed:
                    # A failure after the pair was swapped must restore the
                    # previous on-disk pair before retaining the old in-memory
                    # interpreter. Otherwise a process restart could load a
                    # partially initialized or mixed-version asset set.
                    reload_failed_after_commit = True
                    try:
                        if had_tflite and rollback_tflite.exists():
                            rollback_tflite.replace(_YAMNET_TFLITE_PATH)
                        else:
                            _YAMNET_TFLITE_PATH.unlink(missing_ok=True)
                        if had_csv and rollback_csv.exists():
                            rollback_csv.replace(_YAMNET_CLASS_MAP_PATH)
                        else:
                            _YAMNET_CLASS_MAP_PATH.unlink(missing_ok=True)
                        rollback_tflite.unlink(missing_ok=True)
                        rollback_csv.unlink(missing_ok=True)
                        try:
                            self._write_metadata(previous_metadata)
                        except OSError as metadata_exc:
                            asset_rollback_failed = True
                            logger.error('Could not restore YAMNet metadata after post-commit reload failure: %s', metadata_exc)
                    except OSError as restore_exc:
                        asset_rollback_failed = True
                        logger.error('Could not restore YAMNet assets after post-commit reload failure: %s', restore_exc)
                if had_working_model and not asset_rollback_failed and not reload_failed_after_commit:
                    # Reload is a refresh operation, not a destructive reset:
                    # retain the already allocated interpreter when the new
                    # assets or interpreter cannot be prepared.
                    self._unavailable_reason = previous_reason
                    self._available = True
                    return True
                if had_working_model and (asset_rollback_failed or reload_failed_after_commit):
                    # The old in-memory interpreter remains usable, but the
                    # on-disk asset pair could not be restored consistently.
                    # Report reload failure so operators repair the assets
                    # before the next process restart.
                    self._unavailable_reason = failure_reason
                    self._available = True
                    return False
                self._unavailable_reason = failure_reason
                self._available = False
                return False

    @staticmethod
    def _load_class_names(path: Path) -> list[str]:
        with open(path, newline='', encoding='utf-8') as f:
            rows = sorted(csv.DictReader(f), key=lambda row: int(row['index']))
            return [row['display_name'] for row in rows]

    @staticmethod
    def _build_class_indices(class_names: list[str]) -> dict[str, list[int]]:
        indices: dict[str, list[int]] = {}
        lower_names = [name.lower() for name in class_names]
        for class_id, patterns in _YAMNET_MATCH_PATTERNS.items():
            indices[class_id] = [
                i for i, lname in enumerate(lower_names)
                if any(pat.search(lname) for pat in patterns)
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
                self._save_installed_version()
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
        # Preprocess audio before acquiring the lock - these are pure numpy
        # operations that do not touch model state, so they don't need
        # serialization and keeping them outside the lock reduces contention
        # between cameras sharing the singleton.
        try:
            waveform: np.ndarray = audio.astype(np.float32)
            if waveform.ndim > 1:
                waveform = waveform.mean(axis=1)
        except Exception as exc:
            logger.debug('YAMNet audio preprocessing error: %s', exc)
            return {}

        with self._lock:
            try:
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
                    if target_len != self._dynamic_target_len:
                        if input_signature.size == 1:
                            new_shape = [target_len]
                        else:
                            new_shape = [int(v) if int(v) > 0 else 1 for v in input_signature]
                            new_shape[-1] = target_len
                        self._model.resize_tensor_input(input_index, new_shape, strict=False)
                        self._model.allocate_tensors()
                        self._input_details = self._model.get_input_details()
                        self._output_details = self._model.get_output_details()
                        self._dynamic_target_len = target_len

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
                    mean_scores: np.ndarray = scores_array.copy()
                else:
                    mean_scores = scores_array.reshape(-1, scores_array.shape[-1]).mean(axis=0)
            except Exception as exc:
                logger.debug('YAMNet TFLite inference error: %s', exc)
                return {}

        # Build the result dict outside the lock - mean_scores is a local copy
        # and self._class_indices is immutable after _load().
        result: dict[str, float] = {}
        for class_id, idxs in self._class_indices.items():
            result[class_id] = float(mean_scores[idxs].max()) if idxs else 0.0
        return result

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
        class                - key into SOUND_CLASSES
        name                 - human-readable label used in alerts
        enabled              - bool
        confidence_threshold - minimum score to fire (YAMNet: 0.25-0.40 typical)
        cooldown_seconds     - minimum seconds between consecutive alerts for this class
    """

    def __init__(
        self,
        on_detect: Callable[[str, str, float, dict[str, Any]], None],
        rules: list[dict[str, Any]],
        source: str = 'microphone',
        device_index: int | None = None,
        rtsp_url: str | None = None,
        sample_duration_seconds: float = 1.0,
        audio_segment_provider: Callable[[float], list[tuple[Any, float]]] | None = None,
    ) -> None:
        self.on_detect = on_detect
        self.rules = [r for r in rules if r.get('enabled') and r.get('class') in SOUND_CLASSES]
        self.source = source
        self.device_index = device_index
        self.rtsp_url = rtsp_url
        self.sample_duration_seconds = sample_duration_seconds
        # For source='ingest': returns audio WAV segments (path, mtime) written
        # after the given timestamp by the shared per-camera ingest, so sound
        # detection reuses that single RTSP connection instead of opening its own.
        # Returning ``None`` (rather than ``[]``) is permitted for the producer
        # to signal "no new segments right now" without paying for an empty list
        # allocation per poll - the consumer coerces None to [] at the call
        # site so an early-returning provider can't crash the daemon thread.
        self.audio_segment_provider = audio_segment_provider

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

    def diagnostics(self) -> list[dict[str, Any]]:
        """Per-class snapshot of the latest scores against each rule's threshold
        and cooldown, used by the live status endpoint to explain why a heard
        sound did or didn't alert. Sorted by confidence, highest first."""
        now = time.time()
        with self._status_lock:
            confidences = dict(self._last_confidences)
            triggered = dict(self._last_triggered)
        out: list[dict[str, Any]] = []
        for rule in self.rules:
            class_id = str(rule.get('class') or '')
            if not class_id:
                continue
            threshold = float(rule.get('confidence_threshold', 0.35))
            cooldown = float(rule.get('cooldown_seconds', 30))
            last = triggered.get(class_id, 0.0)
            remaining = max(0.0, cooldown - (now - last)) if last else 0.0
            confidence = float(confidences.get(class_id, 0.0))
            out.append({
                'class': class_id,
                'label': SOUND_CLASSES.get(class_id, {}).get('label', class_id),
                'confidence': round(confidence, 3),
                'threshold': round(threshold, 3),
                'cooldown_seconds': cooldown,
                'cooldown_remaining': round(remaining, 1),
                'in_cooldown': remaining > 0,
            })
        out.sort(key=lambda d: d['confidence'], reverse=True)
        return out

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
        if self.source == 'microphone':
            target = self._run_microphone
        elif self.source == 'ingest':
            target = self._run_ingest
        else:
            target = self._run_rtsp
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

        new_confidences: dict[str, float] = {}
        for rule in self.rules:
            if not self._rule_active_now(rule):
                continue
            class_id = str(rule.get('class') or '')
            confidence = yamnet_scores.get(class_id, 0.0)
            new_confidences[class_id] = confidence

            threshold = float(rule.get('confidence_threshold', 0.35))
            if confidence < threshold:
                continue
            cooldown = float(rule.get('cooldown_seconds', 30))
            last = self._last_triggered.get(class_id, 0.0)
            if now - last < cooldown:
                continue

            with self._status_lock:
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
        if new_confidences:
            with self._status_lock:
                self._last_confidences.update(new_confidences)
        self._set_status('listening')

    @staticmethod
    def _rule_active_now(rule: dict[str, Any]) -> bool:
        start = rule.get('active_start')
        end = rule.get('active_end')
        if not start or not end:
            return True
        now = _now_hm_in_admin_tz()
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

        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            logger.warning('Sound monitor: ffmpeg not found; RTSP audio detection unavailable')
            self._set_status('unavailable: ffmpeg not found')
            return

        # Preload YAMNet before the FFmpeg pipe starts reading
        preload_thread = threading.Thread(target=_yamnet.preload, daemon=True, name='yamnet-preload')
        preload_thread.start()

        chunk_samples = int(SAMPLE_RATE * self.sample_duration_seconds)
        overlap_samples = chunk_samples // 2
        bytes_per_sample = 2  # s16le

        cmd = [
            ffmpeg, '-loglevel', 'error',
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
                raw_buf = bytearray()
                need_bytes = chunk_samples * bytes_per_sample
                advance_bytes = overlap_samples * bytes_per_sample

                while not self._stop_event.is_set() and proc.poll() is None:
                    chunk = proc.stdout.read(need_bytes - len(raw_buf))
                    if not chunk:
                        break
                    raw_buf.extend(chunk)
                    if len(raw_buf) >= need_bytes:
                        audio = (
                            np.frombuffer(raw_buf[:need_bytes], dtype=np.int16)
                            .astype(np.float32) / 32768.0
                        )
                        del raw_buf[:advance_bytes]
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

    def _run_ingest(self) -> None:
        """Consume PCM-WAV audio segments produced by the shared per-camera
        ingest, so sound detection adds no extra RTSP connection.

        Segments (16 kHz mono s16le) are appended to a rolling buffer and
        classified in 50%-overlapping windows - the same sliding-window scheme
        the microphone/RTSP paths use. Classifying each 1s segment in isolation
        (the previous behaviour) split short transients across the segment
        boundary, halving their energy in each window and missing barks, glass
        breaks, single doorbell chimes, etc. Per-class cooldowns in
        _handle_chunk prevent the overlap from double-alerting on one sound."""
        if self.audio_segment_provider is None:
            logger.warning('Sound monitor: ingest source selected but no audio segment provider')
            self._set_status('unavailable: no audio provider')
            return

        import wave  # noqa: F811 — stdlib; local import avoids load when ingest source is unused.

        preload_thread = threading.Thread(target=_yamnet.preload, daemon=True, name='yamnet-preload')
        preload_thread.start()

        chunk_samples = max(1, int(SAMPLE_RATE * self.sample_duration_seconds))
        overlap_samples = chunk_samples // 2
        advance_samples = max(1, chunk_samples - overlap_samples)
        # Cap the buffer so a detection stall / segment gap can't grow it without
        # bound; keep at most a few windows of recent audio.
        max_buffer_samples = chunk_samples * 4
        buffer = np.zeros(0, dtype=np.float32)

        enabled_classes = [r['class'] for r in self.rules]
        logger.info('Sound monitor started (ingest, classes=%s)', enabled_classes)
        self._set_status('listening')

        # Only process segments newer than startup so we don't replay stale audio.
        last_ts = time.time()
        # When the per-camera ingest discovers the stream has no audio,
        # ``audio_segments_after`` raises ``no audio track in stream``. Gate
        # further polling behind a 30s probe interval (re-armed on every
        # re-detection) so the sound idle-state has a single 'unavailable'
        # status and doesn't hammer the file system for a queue that can
        # never produce a chunk.
        no_audio_until = 0.0
        while not self._stop_event.is_set():
            if no_audio_until > 0:
                if time.time() < no_audio_until:
                    self._stop_event.wait(
                        min(2.0, max(0.0, no_audio_until - time.time()))
                    )
                    continue
                # Probe window elapsed: clear the gate so the next iteration
                # re-queries the provider. If the stream has gained audio
                # since the last probe, we fall through into the listening
                # path; otherwise the provider raises again and we re-arm.
                self._set_status('listening')
                no_audio_until = 0.0
            try:
                segments = self.audio_segment_provider(last_ts)
            except RuntimeError as exc:
                if NO_AUDIO_EXC_PREFIX in str(exc):
                    logger.info(
                        'Sound monitor: %s has no audio track; will retry in 30s.',
                        self.rtsp_url or self.device_index,
                    )
                    self._set_status(NO_AUDIO_STATUS)
                    no_audio_until = time.time() + 30.0
                    continue
                logger.error('Sound monitor ingest provider error: %s', exc)
                segments = []
            except Exception as exc:
                logger.error('Sound monitor ingest provider error: %s', exc)
                segments = []
            # The provider's contract documents that it MAY return ``None`` to
            # signal "nothing new" - coerce to ``[]`` so the for-loop never
            # unwraps a None and the daemon thread doesn't die on this very
            # line with ``TypeError: 'NoneType' object is not iterable``. A
            # buggy early-returning provider used to surface here as a fatal
            # background-thread crash and the sound monitor went silent
            # until the service was restarted.
            if segments is None:
                segments = []
            for path, mtime in segments:
                if self._stop_event.is_set():
                    break
                try:
                    with wave.open(str(path), 'rb') as wav:
                        frames = wav.readframes(wav.getnframes())
                except Exception as exc:
                    # Don't advance last_ts: the tail segment may still be mid-write
                    # by the ingest, so retry it next poll instead of dropping the
                    # second of audio permanently.
                    logger.debug('Sound monitor could not read audio segment %s: %s', path, exc)
                    continue
                if not frames:
                    # A header-only / zero-frame segment is usually a file still
                    # being written by the ingest. Don't advance last_ts: retry it
                    # next poll instead of dropping the second of audio permanently
                    # (mirrors the read-error branch above).
                    logger.debug('Sound monitor read an empty audio segment %s', path)
                    continue
                last_ts = max(last_ts, mtime)
                audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                if not audio.size:
                    continue
                buffer = np.concatenate((buffer, audio)) if buffer.size else audio
                # Classify overlapping windows that straddle segment boundaries.
                while buffer.size >= chunk_samples:
                    self._handle_chunk(buffer[:chunk_samples])
                    buffer = buffer[advance_samples:]
                if buffer.size > max_buffer_samples:
                    buffer = buffer[-chunk_samples:]
            # Segments arrive ~1/s; poll a little faster so detection stays prompt.
            self._stop_event.wait(0.5)
