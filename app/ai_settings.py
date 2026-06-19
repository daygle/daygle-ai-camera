"""AI subsystem helpers extracted from ``app/main.py`` (Phase-20).

The 3 helpers shipped here cluster around the **AI model subsystem**:
validating incoming AI settings payloads, computing the AI subsystem's
status payload (model loaded / available / onnx-runtime / mode / errors),
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
  ``iou_threshold``; int 32-2048 for ``input_size``; allowed ``device``
  values; non-negative int for ``gpu_mem_limit``), raises
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

from pathlib import Path
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.config_facades import effective_ai_config
from app.detector import load_labels


def ai_status_payload(
    ai_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.main import active_ai_config_source, detector_loaded_for, model_exists, onnx_runtime_installed, YOLO_MODELS
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
        'categories': categories,
        'available_labels': labels,
    }


def validate_ai_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_ai_config()
    allowed = {
        'enabled',
        'backend',
        'confidence',
        'iou_threshold',
        'input_size',
        'model_path',
        'labels_path',
        'device',
        'gpu_mem_limit',
        'inference_threads',
        'max_concurrent_inferences',
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
    try:
        updated['input_size'] = int(updated.get('input_size', 640))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='input_size must be an integer.') from exc
    if updated['input_size'] < 32 or updated['input_size'] > 2048:
        raise HTTPException(status_code=400, detail='input_size must be between 32 and 2048.')
    device = str(updated.get('device', 'auto')).lower()
    if device not in ('auto', 'cpu', 'cuda'):
        raise HTTPException(status_code=400, detail="device must be 'auto', 'cpu', or 'cuda'.")
    updated['device'] = device
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
    updated['model_path'] = str(
        updated.get('model_path') or current.get('model_path') or 'models/yolov8n.onnx'
    )
    updated['labels_path'] = str(
        updated.get('labels_path') or current.get('labels_path') or 'models/coco.names'
    )
    return updated
