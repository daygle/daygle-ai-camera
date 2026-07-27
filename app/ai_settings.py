"""AI subsystem helpers extracted from ``app/main.py`` (Phase-20).

The 3 helpers shipped here cluster around the **AI model subsystem**:
validating incoming AI settings payloads, computing the AI subsystem's
status payload (model loaded / available / onnx-runtime / mode / errors / model_input_size),
and shaping the per-request detector status response that the
``/api/status/ai`` endpoint and the admin dashboard surface.

Like the prior-cluster extractions (``app/auth_gates.py`` Phase-16,
``app/config_facades.py`` Phase-17, ``app/camera_config.py`` Phase-18,
``app/recording_settings.py`` Phase-19), these are extracted using the
**hybrid-pattern template**:

- Cluster functions reach ``main.<attr>`` at *call time* (NOT import
  time) for their cross-module dependencies, so they continue to work
  seamlessly when ``app/main.py`` is partially loaded during the
  Pool A rebind loop.
- The Pool A from-import rebinds live at the TOP of ``app/main.py``
  (alphabetically sorted, in the existing rebind section) so that the
  eagar-evaluation order at module load has ``main.<name>`` wired
  correctly before any sibling-body references it as a bare name.

Cluster membership:

- ``ai_status_payload`` -- computes the AI status dict (active_backend,
  configured_backend, mode, model_loaded, detector_loaded, model_path,
  model_name, labels_path, model_exists, onnx_runtime_installed,
  inference_available, error, last_detector_error,
  active_config_source). The single source of truth for AI
  subsystem health surfaced by ``/api/status/ai``.

- ``detector_status`` -- wraps ``ai_status_payload`` with the
  per-request label discovery (``load_labels``), categories from the
  AI config, and a flat shape with ``available``/``available_labels``
  keys for the dashboard UI. (Internal callers in main.py reference
  both ``ai_status_payload`` and ``detector_status`` as bare names; the
  top-of-file Pool A rebind fires before any of those bodies evaluates.)

- ``validate_ai_settings`` -- the settings-router payload validator.
  Enforces the allowed-keys allow-list, coerces types (str -> bool
  for ``enabled``; float-range 0-1 for ``confidence`` /
  ``iou_threshold``; allowed ``device`` values; non-negative int for
  ``gpu_mem_limit``), raises
  ``fastapi.HTTPException(400, ...)`` for any out-of-range input. Used
  by ``/api/settings/ai`` (POST) and ``/api/settings/ai/download``
  preflight (via the internal ``_do_download_model`` helper in main.py).

Pool C reach sites (resolved via ``main.<attr>`` at call time):

- ``main.effective_ai_config`` (Phase-17) -- ``ai_status_payload`` and
  ``validate_ai_settings`` both default to the active AI config when
  the caller passes ``None`` / a payload-dict respectively.
- ``main.detector``, ``main.YOLO_MODELS``, ``main.last_detector_error``
  -- AI subsystem singletons read by ``ai_status_payload``.
- ``main.detector_loaded_for``, ``main.onnx_runtime_installed``,
  ``main.model_exists``, ``main.active_ai_config_source`` -- AI
  subsystem helpers read by ``ai_status_payload``.
- ``main.config`` -- backend's intial AI settings (categories fallback)
  read by ``detector_status``.
- ``main.load_labels`` -- AI labels loader read by ``detector_status``.
- ``main.HTTPException`` -- raised by ``validate_ai_settings`` for
  400-level validation errors (imported from ``fastapi`` in main.py).
"""

from __future__ import annotations

import logging
import importlib.util
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.config_facades import effective_ai_config
from app.detector import load_labels
from app.settings import config_file_path

YOLO_MODELS: dict[str, dict[str, Any]] = {
    # YOLOv8 series - Traditional NMS-based detection
    'yolov8n': {'pt': 'yolov8n.pt', 'onnx': 'yolov8n.onnx', 'label': 'YOLOv8n · Nano', 'approx_mb': 6, 'description': 'Fastest inference, lowest accuracy. Best for low-power or embedded hardware.'},
    'yolov8s': {'pt': 'yolov8s.pt', 'onnx': 'yolov8s.onnx', 'label': 'YOLOv8s · Small', 'approx_mb': 22, 'description': 'Good balance of speed and accuracy for most systems.'},
    'yolov8m': {'pt': 'yolov8m.pt', 'onnx': 'yolov8m.onnx', 'label': 'YOLOv8m · Medium', 'approx_mb': 52, 'description': 'Significantly better accuracy. Recommended for IR or night-vision cameras.'},
    'yolov8l': {'pt': 'yolov8l.pt', 'onnx': 'yolov8l.onnx', 'label': 'YOLOv8l · Large', 'approx_mb': 87, 'description': 'High accuracy. Requires a capable CPU or GPU.'},
    'yolov8x': {'pt': 'yolov8x.pt', 'onnx': 'yolov8x.onnx', 'label': 'YOLOv8x · Extra Large', 'approx_mb': 131, 'description': 'Best possible accuracy. GPU strongly recommended.'},
    # YOLO11 series - Refined backbone/neck, 22% fewer params than YOLOv8 with better accuracy
    'yolo11n': {'pt': 'yolo11n.pt', 'onnx': 'yolo11n.onnx', 'label': 'YOLO11n · Nano', 'approx_mb': 5, 'description': 'Latest Ultralytics architecture. Faster than YOLOv8n with improved accuracy.'},
    'yolo11s': {'pt': 'yolo11s.pt', 'onnx': 'yolo11s.onnx', 'label': 'YOLO11s · Small', 'approx_mb': 20, 'description': 'Enhanced small model with better accuracy-latency tradeoff than YOLOv8s.'},
    'yolo11m': {'pt': 'yolo11m.pt', 'onnx': 'yolo11m.onnx', 'label': 'YOLO11m · Medium', 'approx_mb': 46, 'description': 'Best mid-range model. 22% fewer parameters than YOLOv8m with higher mAP.'},
    'yolo11l': {'pt': 'yolo11l.pt', 'onnx': 'yolo11l.onnx', 'label': 'YOLO11l · Large', 'approx_mb': 78, 'description': 'High accuracy for demanding applications. Improved over YOLOv8l.'},
    'yolo11x': {'pt': 'yolo11x.pt', 'onnx': 'yolo11x.onnx', 'label': 'YOLO11x · Extra Large', 'approx_mb': 119, 'description': 'Maximum accuracy YOLO11 variant. GPU recommended.'},
    # YOLO26 series - NMS-free end-to-end detection, up to 43% faster CPU inference
    'yolo26n': {'pt': 'yolo26n.pt', 'onnx': 'yolo26n.onnx', 'label': 'YOLO26n · Nano', 'approx_mb': 5, 'nms_free': True, 'description': 'End-to-end NMS-free detection. Fastest CPU inference with modern architecture.'},
    'yolo26s': {'pt': 'yolo26s.pt', 'onnx': 'yolo26s.onnx', 'label': 'YOLO26s · Small', 'approx_mb': 18, 'nms_free': True, 'description': 'NMS-free small model. Great speed-accuracy balance for edge deployment.'},
    'yolo26m': {'pt': 'yolo26m.pt', 'onnx': 'yolo26m.onnx', 'label': 'YOLO26m · Medium', 'approx_mb': 42, 'nms_free': True, 'description': 'Mid-range NMS-free model with excellent accuracy.'},
    'yolo26l': {'pt': 'yolo26l.pt', 'onnx': 'yolo26l.onnx', 'label': 'YOLO26l · Large', 'approx_mb': 72, 'nms_free': True, 'description': 'High accuracy NMS-free detection. Advanced ProgLoss + STAL training.'},
    'yolo26x': {'pt': 'yolo26x.pt', 'onnx': 'yolo26x.onnx', 'label': 'YOLO26x · Extra Large', 'approx_mb': 112, 'nms_free': True, 'description': 'Ultimate accuracy with NMS-free inference. MuSGD optimizer for best convergence.'},
}

logger = logging.getLogger('daygle.ai')

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / 'models'


def _canonical_models_path(raw: Any, field: str) -> str:
    """Validate that a model / labels path stays inside ``models/``.

    Accepts a project-relative (``models/yolov8n.onnx``) or absolute path,
    rejects anything that escapes the models directory (``..`` traversal,
    absolute paths pointing elsewhere, or the directory itself with no
    filename), and returns the canonical project-relative string. Raises
    ``HTTPException(400)`` on rejection so a typo or a malicious path in the
    free-text Model Path / Labels Path field surfaces a clean error instead
    of being persisted and silently disabling detection.
    """
    text = str(raw or '').strip()
    if not text:
        raise HTTPException(status_code=400, detail=f'{field} must not be empty.')
    candidate = Path(text)
    resolved = (candidate if candidate.is_absolute() else BASE_DIR / candidate).resolve()
    models_root = MODELS_DIR.resolve()
    if resolved == models_root or not resolved.is_relative_to(models_root):
        raise HTTPException(
            status_code=400,
            detail=f'{field} must point to a file inside the models/ directory.',
        )
    return str(resolved.relative_to(BASE_DIR))


def active_ai_config_source() -> str:
    if _state.database.has_setting('ai'):
        return 'database'
    if config_file_path().exists():
        return 'config.yaml'
    return 'default'


def onnx_runtime_installed() -> bool:
    return importlib.util.find_spec('onnxruntime') is not None


def model_exists(ai_settings: dict[str, Any]) -> bool:
    model_path = str(ai_settings.get('model_path') or '')
    return bool(model_path) and Path(model_path).exists()


def detector_loaded_for(settings: dict[str, Any]) -> bool:
    configured_backend = str(settings.get('backend', 'onnx')).lower()
    active_backend = getattr(_state.detector, 'backend', 'unknown')
    if configured_backend == 'onnx':
        return active_backend == 'onnx' and bool(getattr(_state.detector, 'available', False))
    return False


def log_detector_initialization(context: str = 'startup') -> None:
    ai_status = ai_status_payload()
    active_providers = getattr(_state.detector, 'active_providers', None)
    providers_str = ','.join(active_providers) if active_providers else '<none>'
    logger.info(
        'AI detector %s: active_backend=%s configured_backend=%s model_loaded=%s inference_available=%s providers=%s model_path=%s labels_path=%s error=%s',
        context, ai_status['active_backend'], ai_status['configured_backend'], ai_status['model_loaded'],
        ai_status['inference_available'], providers_str, ai_status['model_path'] or '<none>',
        ai_status['labels_path'] or '<none>', ai_status['error'] or '<none>',
    )


def ai_status_payload(
    ai_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = ai_settings or effective_ai_config()
    active_backend = getattr(_state.detector, 'backend', 'unknown')
    configured_backend = str(settings.get('backend', 'onnx')).lower()
    detector_loaded = detector_loaded_for(settings)
    model_loaded = bool(
        configured_backend == 'onnx'
        and active_backend == 'onnx'
        and getattr(_state.detector, 'available', False)
    )
    runtime_installed = onnx_runtime_installed()
    exists = model_exists(settings)
    detector_reason = getattr(_state.detector, 'unavailable_reason', None)
    error = _state.last_detector_error or detector_reason
    if configured_backend == 'onnx' and (not exists):
        mode = 'MODEL MISSING'
        error = error or f"ONNX model not found: {settings.get('model_path')}"
    elif configured_backend == 'onnx' and (not model_loaded):
        mode = 'MODEL FAILED'
    elif configured_backend == 'onnx':
        mode = 'ONNX ACTIVE'
        error = detector_reason
    else:
        mode = 'MODEL FAILED'
    inference_available = detector_loaded
    model_path_str = str(settings.get('model_path') or '')
    model_filename = Path(model_path_str).name if model_path_str else ''
    model_label = next(
        (
            info['label']
            for info in YOLO_MODELS.values()
            if info['onnx'] == model_filename
        ),
        None,
    )
    # Read the model's actual input dimensions from the detector.
    # The detector overrides configured input_size with the model's
    # real shape at load time, so this is the ground truth.
    detector_input_w = getattr(_state.detector, 'input_width', None)
    detector_input_h = getattr(_state.detector, 'input_height', None)
    model_input_size = None
    if model_loaded and detector_input_w and detector_input_h:
        model_input_size = f'{detector_input_w}\u00d7{detector_input_h}'
    # The precision actually running (after any INT8/FP16 fallback), so the
    # Status panel can show when a requested int8/fp16 silently ran as fp32.
    active_precision = getattr(_state.detector, 'active_precision', None) if model_loaded else None
    return {
        'active_backend': active_backend,
        'configured_backend': configured_backend,
        'mode': mode,
        'model_loaded': model_loaded,
        'detector_loaded': detector_loaded,
        'model_path': model_path_str,
        'model_name': model_label,
        'labels_path': str(settings.get('labels_path') or ''),
        'model_exists': exists,
        'onnx_runtime_installed': runtime_installed,
        'inference_available': inference_available,
        'error': error,
        'last_detector_error': error,
        'active_config_source': active_ai_config_source(),
        'model_input_size': model_input_size,
        'active_precision': active_precision,
    }


def detector_status(ai_settings: dict[str, Any]) -> dict[str, Any]:
    ai_status = ai_status_payload(ai_settings)
    categories = ai_settings.get(
        'categories',
        _state.config.get('ai', {}).get('categories', []),
    )
    labels = load_labels(
        ai_settings.get('labels_path'), categories,
    ) or list(categories)
    return {
        **ai_settings,
        'active_backend': ai_status['active_backend'],
        'configured_backend': ai_status['configured_backend'],
        'mode': ai_status['mode'],
        'available': ai_status['inference_available'],
        'model_loaded': ai_status['model_loaded'],
        'detector_loaded': ai_status['detector_loaded'],
        'model_exists': ai_status['model_exists'],
        'onnx_runtime_installed': ai_status['onnx_runtime_installed'],
        'active_config_source': ai_status['active_config_source'],
        'error': ai_status['error'],
        'last_detector_error': ai_status['last_detector_error'],
        'model_input_size': ai_status.get('model_input_size'),
        'active_precision': ai_status.get('active_precision'),
        'model_name': ai_status.get('model_name'),
        # Surface the normalised tri-state so the settings form's NMS-dedupe
        # select reflects the persisted value (defaulting to 'auto') rather
        # than a raw legacy bool or a missing key.
        'confidence_only_nms': _normalize_confidence_only_nms(
            ai_settings.get('confidence_only_nms')
        ),
        'categories': categories,
        'available_labels': labels,
    }


def _normalize_confidence_only_nms(value: Any) -> str:
    """Normalise the tri-state ``confidence_only_nms`` setting to a string.

    Returns one of ``'auto'`` | ``'on'`` | ``'off'``. Legacy persisted bools
    map to ``'on'`` / ``'off'``; anything unrecognised (including a missing
    value) normalises to ``'auto'`` so the detector applies its model-aware
    default (skip the redundant NMS for NMS-free YOLO26 heads).
    """
    if isinstance(value, bool):
        return 'on' if value else 'off'
    text = str(value or '').strip().lower()
    if text in {'on', '1', 'true', 'yes'}:
        return 'on'
    if text in {'off', '0', 'false', 'no'}:
        return 'off'
    return 'auto'


def validate_ai_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_ai_config()
    allowed = {
        'enabled',
        'backend',
        'confidence',
        'iou_threshold',
        'model_path',
        'labels_path',
        'device',
        'gpu_mem_limit',
        'inference_threads',
        'max_concurrent_inferences',
        'execution_mode',
        'confidence_only_nms',
        'precision',
        'use_io_binding',
    }
    updated = {key: current.get(key) for key in allowed if key in current}
    for key, value in payload.items():
        if key in allowed:
            updated[key] = value
    enabled_value = updated.get('enabled', True)
    if isinstance(enabled_value, str):
        updated['enabled'] = enabled_value.lower() in {'1', 'true', 'yes', 'on'}
    else:
        updated['enabled'] = bool(enabled_value)
    backend = str(updated.get('backend', 'onnx')).lower()
    if backend != 'onnx':
        raise HTTPException(status_code=400, detail='AI backend must be onnx.')
    updated['backend'] = backend
    for field in ('confidence', 'iou_threshold'):
        try:
            updated[field] = float(updated.get(field, 0.45))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f'{field} must be a number.') from exc
        if not 0 <= updated[field] <= 1:
            raise HTTPException(status_code=400, detail=f'{field} must be between 0 and 1.')
    device = str(updated.get('device', 'auto')).lower()
    if device not in ('auto', 'cpu', 'cuda'):
        raise HTTPException(status_code=400, detail="device must be 'auto', 'cpu', or 'cuda'.")
    updated['device'] = device
    # ORT executor toggle (CPU path only -- CUDA EP manages its own parallelism).
    # ``parallel`` is the ORT default and matches prior behavior; ``sequential``
    # is an opt-in A/B lever that some YOLO graphs run faster under.
    execution_mode = str(updated.get('execution_mode', 'parallel')).lower()
    if execution_mode not in ('parallel', 'sequential'):
        raise HTTPException(
            status_code=400,
            detail="execution_mode must be 'parallel' or 'sequential'.",
        )
    updated['execution_mode'] = execution_mode
    # ``precision`` selects the inference path the detector will run.
    # Validator normalises case but does NOT enforce the precision/device
    # cross-product (e.g. ``precision='fp16' + device='cpu'``) -- that's
    # the detector's responsibility: it logs a warning and falls back to
    # fp32 so a config moved between hosts (CUDA -> CPU) still loads.
    precision = str(updated.get('precision', 'fp32')).strip().lower()
    if precision not in ('fp32', 'fp16', 'int8'):
        raise HTTPException(
            status_code=400,
            detail="precision must be 'fp32', 'fp16', or 'int8'.",
        )
    updated['precision'] = precision
    # ``use_io_binding`` toggles ORT's direct CUDA memory path. Same
    # permissive coercion as ``confidence_only_nms`` -- absent or empty
    # string leaves the key out of the output so the detector default
    # (False) wins.
    if 'use_io_binding' in updated:
        val = updated['use_io_binding']
        if isinstance(val, str):
            updated['use_io_binding'] = val.strip().lower() in {'1', 'true', 'yes', 'on'}
        else:
            updated['use_io_binding'] = bool(val)
    # ``confidence_only_nms`` is tri-state: 'auto' | 'on' | 'off'. 'auto'
    # (the default when the key is absent) lets the detector apply its
    # model-aware default -- skip the redundant class-aware NMS for NMS-free
    # YOLO26 heads, keep it for grid heads. 'on'/'off' are explicit overrides.
    # Persisting a tri-state (rather than a bare bool from a checkbox) is what
    # lets the settings form save 'auto' without clobbering the smart default.
    # Only normalise when present so an absent key stays absent -> 'auto'.
    if 'confidence_only_nms' in updated:
        updated['confidence_only_nms'] = _normalize_confidence_only_nms(
            updated['confidence_only_nms']
        )
    if 'gpu_mem_limit' in payload:
        gpu_mem_limit = payload['gpu_mem_limit']
        if gpu_mem_limit is not None and gpu_mem_limit != '':
            try:
                gpu_mem_limit = int(gpu_mem_limit)
                if gpu_mem_limit < 0:
                    raise ValueError
                updated['gpu_mem_limit'] = gpu_mem_limit
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail='gpu_mem_limit must be a non-negative integer (bytes), or 0 for unlimited.',
                ) from exc
        else:
            updated['gpu_mem_limit'] = 0
    for field, min_val, max_val in (('inference_threads', 1, 32), ('max_concurrent_inferences', 1, 16)):
        raw = payload.get(field)
        if raw is not None and raw != '':
            try:
                val = int(raw)
                if not min_val <= val <= max_val:
                    raise ValueError
                updated[field] = val
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f'{field} must be an integer between {min_val} and {max_val}.',
                ) from exc
        else:
            updated.pop(field, None)
    raw_model_path = updated.get('model_path') or current.get('model_path') or 'models/yolo11n.onnx'
    model_path = _canonical_models_path(raw_model_path, 'model_path')
    # Existence guard, but only when the caller explicitly supplied a *new*
    # non-empty model_path (typo protection on the settings form / API).
    # Re-saving the current path, or leaving a not-yet-downloaded default in
    # place while editing other fields, must still succeed so the UI can show
    # the MODEL MISSING state rather than blocking the save.
    if 'model_path' in payload and str(payload.get('model_path') or '').strip():
        current_canon = ''
        if current.get('model_path'):
            try:
                current_canon = _canonical_models_path(current['model_path'], 'model_path')
            except HTTPException:
                current_canon = ''
        if model_path != current_canon and not (BASE_DIR / model_path).exists():
            raise HTTPException(
                status_code=400,
                detail=(
                    f'ONNX model file not found: {model_path}. '
                    'Download the model first, or choose an installed model.'
                ),
            )
    updated['model_path'] = model_path
    raw_labels_path = updated.get('labels_path') or current.get('labels_path') or 'models/coco.names'
    updated['labels_path'] = _canonical_models_path(raw_labels_path, 'labels_path')
    # nms_free is intentionally NOT a persisted setting: the detector derives
    # the head format at load time from the model filename and, decisively,
    # from the actual ONNX output shape (see OnnxYoloDetector._postprocess).
    # Persisting it here only risks a stale flag surviving a model switch, so
    # it is dropped from the allow-list above.
    return updated
