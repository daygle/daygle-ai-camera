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
import threading
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.config_facades import effective_ai_config
from app.detector import load_labels
from app.settings import config_file_path

# Each entry describes one downloadable model. Required keys: ``pt`` (source
# weight name), ``onnx`` (exported filename), ``label``, ``approx_mb``,
# ``input_size``, ``description``. Optional keys:
#   ``nms_free``       - YOLO26 end-to-end head (exported with ``end2end=True``).
#   ``labels``         - project-relative labels file for a non-COCO model
#                        (default ``models/coco.names``). A face detector ships
#                        ``models/face.names``; the download flow binds it to the
#                        active AI settings automatically.
#   ``keypoint_count`` - pose/keypoint head width (e.g. ``5`` for a YOLO-face
#                        landmark head) so the detector reads class scores from
#                        the right columns instead of the landmark columns.
_DEFAULT_MODEL = 'yolo11n'
#   ``weights_url``    - explicit https source for weights Ultralytics can't
#                        resolve by name (third-party face models). The
#                        ``yolo11*-face`` entries below use all three optional
#                        keys; see ``docs/ai-detection.md`` for their source and
#                        the licensing note that goes with them.
YOLO_MODELS: dict[str, dict[str, Any]] = {
    # YOLOv8 series - Traditional NMS-based detection
    'yolov8n': {'pt': 'yolov8n.pt', 'onnx': 'yolov8n.onnx', 'label': 'YOLOv8n · Nano', 'approx_mb': 6, 'input_size': 640, 'description': 'Fastest inference, lowest accuracy. Best for low-power or embedded hardware.'},
    'yolov8s': {'pt': 'yolov8s.pt', 'onnx': 'yolov8s.onnx', 'label': 'YOLOv8s · Small', 'approx_mb': 22, 'input_size': 640, 'description': 'Good balance of speed and accuracy for most systems.'},
    'yolov8m': {'pt': 'yolov8m.pt', 'onnx': 'yolov8m.onnx', 'label': 'YOLOv8m · Medium', 'approx_mb': 52, 'input_size': 640, 'description': 'Significantly better accuracy. Recommended for IR or night-vision cameras.'},
    'yolov8l': {'pt': 'yolov8l.pt', 'onnx': 'yolov8l.onnx', 'label': 'YOLOv8l · Large', 'approx_mb': 87, 'input_size': 640, 'description': 'High accuracy. Requires a capable CPU or GPU.'},
    'yolov8x': {'pt': 'yolov8x.pt', 'onnx': 'yolov8x.onnx', 'label': 'YOLOv8x · Extra Large', 'approx_mb': 131, 'input_size': 640, 'description': 'Best possible accuracy. GPU strongly recommended.'},
    # YOLO11 series - Refined backbone/neck, 22% fewer params than YOLOv8 with better accuracy
    'yolo11n': {'pt': 'yolo11n.pt', 'onnx': 'yolo11n.onnx', 'label': 'YOLO11n · Nano', 'approx_mb': 5, 'input_size': 640, 'description': 'Latest Ultralytics architecture. Faster than YOLOv8n with improved accuracy.'},
    'yolo11s': {'pt': 'yolo11s.pt', 'onnx': 'yolo11s.onnx', 'label': 'YOLO11s · Small', 'approx_mb': 20, 'input_size': 640, 'description': 'Enhanced small model with better accuracy-latency tradeoff than YOLOv8s.'},
    'yolo11m': {'pt': 'yolo11m.pt', 'onnx': 'yolo11m.onnx', 'label': 'YOLO11m · Medium', 'approx_mb': 46, 'input_size': 640, 'description': 'Best mid-range model. 22% fewer parameters than YOLOv8m with higher mAP.'},
    'yolo11l': {'pt': 'yolo11l.pt', 'onnx': 'yolo11l.onnx', 'label': 'YOLO11l · Large', 'approx_mb': 78, 'input_size': 640, 'description': 'High accuracy for demanding applications. Improved over YOLOv8l.'},
    'yolo11x': {'pt': 'yolo11x.pt', 'onnx': 'yolo11x.onnx', 'label': 'YOLO11x · Extra Large', 'approx_mb': 119, 'input_size': 640, 'description': 'Maximum accuracy YOLO11 variant. GPU recommended.'},
    # YOLO26 series - NMS-free end-to-end detection, up to 43% faster CPU inference
    'yolo26n': {'pt': 'yolo26n.pt', 'onnx': 'yolo26n.onnx', 'label': 'YOLO26n · Nano', 'approx_mb': 5, 'input_size': 768, 'nms_free': True, 'description': 'End-to-end NMS-free detection. Fastest CPU inference with modern architecture.'},
    'yolo26s': {'pt': 'yolo26s.pt', 'onnx': 'yolo26s.onnx', 'label': 'YOLO26s · Small', 'approx_mb': 18, 'input_size': 768, 'nms_free': True, 'description': 'NMS-free small model. Great speed-accuracy balance for edge deployment.'},
    'yolo26m': {'pt': 'yolo26m.pt', 'onnx': 'yolo26m.onnx', 'label': 'YOLO26m · Medium', 'approx_mb': 42, 'input_size': 768, 'nms_free': True, 'description': 'Mid-range NMS-free model with excellent accuracy.'},
    'yolo26l': {'pt': 'yolo26l.pt', 'onnx': 'yolo26l.onnx', 'label': 'YOLO26l · Large', 'approx_mb': 72, 'input_size': 768, 'nms_free': True, 'description': 'High accuracy NMS-free detection. Advanced ProgLoss + STAL training.'},
    'yolo26x': {'pt': 'yolo26x.pt', 'onnx': 'yolo26x.onnx', 'label': 'YOLO26x · Extra Large', 'approx_mb': 112, 'input_size': 768, 'nms_free': True, 'description': 'Ultimate accuracy with NMS-free inference. MuSGD optimizer for best convergence.'},
    # YOLO11-Face series - single-class face detection (a `face` label), not
    # recognition. These are YOLO11-pose models with a 5-point facial-landmark
    # head (keypoint_count=5) and their own labels file (models/face.names); the
    # download flow binds both to the active AI settings automatically. Source
    # weights: https://github.com/YapaLab/yolo-face (release 1.0.0), licensed
    # GPL-3.0 and exported through Ultralytics like every other catalog model.
    # Ultralytics cannot resolve these names from its own asset set, so each
    # entry names an explicit ``weights_url``.
    'yolo11n-face': {'pt': 'yolov11n-face.pt', 'onnx': 'yolov11n-face.onnx', 'label': 'YOLO11n · Face', 'approx_mb': 6, 'input_size': 640, 'labels': 'models/face.names', 'keypoint_count': 5, 'weights_url': 'https://github.com/YapaLab/yolo-face/releases/download/1.0.0/yolov11n-face.pt', 'description': 'Fastest face detection. Detects faces only (single "face" label), not who they are.'},
    'yolo11s-face': {'pt': 'yolov11s-face.pt', 'onnx': 'yolov11s-face.onnx', 'label': 'YOLO11s · Face', 'approx_mb': 19, 'input_size': 640, 'labels': 'models/face.names', 'keypoint_count': 5, 'weights_url': 'https://github.com/YapaLab/yolo-face/releases/download/1.0.0/yolov11s-face.pt', 'description': 'Balanced face detection for most systems. Detects faces only, not identity.'},
    'yolo11m-face': {'pt': 'yolov11m-face.pt', 'onnx': 'yolov11m-face.onnx', 'label': 'YOLO11m · Face', 'approx_mb': 40, 'input_size': 640, 'labels': 'models/face.names', 'keypoint_count': 5, 'weights_url': 'https://github.com/YapaLab/yolo-face/releases/download/1.0.0/yolov11m-face.pt', 'description': 'Higher-accuracy face detection. Recommended for IR or night-vision cameras.'},
    'yolo11l-face': {'pt': 'yolov11l-face.pt', 'onnx': 'yolov11l-face.onnx', 'label': 'YOLO11l · Face', 'approx_mb': 51, 'input_size': 640, 'labels': 'models/face.names', 'keypoint_count': 5, 'weights_url': 'https://github.com/YapaLab/yolo-face/releases/download/1.0.0/yolov11l-face.pt', 'description': 'High-accuracy face detection. Requires a capable CPU or GPU.'},
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
    return resolved.relative_to(BASE_DIR).as_posix()


def is_face_family_model(model_path: Any, labels_path: Any = None) -> bool:
    """Return ``True`` when ``model_path`` looks like a face-detection model.

    Face-family ONNX exports are recognised by a ``face`` marker in the
    filename (``yolov11n-face.onnx``, ``...-face-640.onnx``) or by shipping
    the face labels file (``models/face.names``). This mirrors the frontend's
    ``/face/i.test(path)`` heuristic and does not depend on the catalog:
    legacy installs may hold a face file that predates the family split.
    """
    name = Path(str(model_path or '')).name.lower()
    labels = Path(str(labels_path or '')).name.lower()
    return 'face' in name or labels == 'face.names'


def find_installed_object_model() -> str | None:
    """Find the best installed non-face ONNX to restore as the PRIMARY model.

    Preference order: exact catalog object-model filenames (nano variants
    first, matching the catalog declaration order), then any remaining
    non-face ONNX smallest-first so the restored default favours fast
    inference. Returns a project-relative posix path, or ``None`` when no
    object model is installed (the caller falls back to the downloadable
    default).
    """
    try:
        installed = [p for p in sorted(MODELS_DIR.glob('*.onnx')) if 'face' not in p.name.lower()]
    except OSError:
        return None
    if not installed:
        return None
    by_name = {p.name: p for p in installed}
    catalog_order = [
        Path(info['onnx']).name
        for info in YOLO_MODELS.values()
        if not info.get('labels')
    ]
    for filename in catalog_order:
        candidate = by_name.get(filename)
        if candidate is not None:
            return candidate.as_posix()
    smallest = min(installed, key=lambda p: p.stat().st_size)
    return smallest.as_posix()


def heal_legacy_face_primary() -> dict[str, Any] | None:
    """Repair settings where a FACE model was saved as the PRIMARY detector.

    Before the parallel face pass existed, downloading/activating a face
    model replaced the active object model (``model_path`` became e.g.
    ``models/yolov11n-face.onnx`` with ``labels_path=models/face.names``).
    Such a deployment detects faces ONLY: the Objects page shows no objects,
    and no object recordings are produced. The parallel architecture expects
    an object model as PRIMARY plus the face model in the secondary
    ``face_model_path`` slot -- so this migration moves the legacy face model
    into the secondary slot, restores an object model as PRIMARY, persists
    the healed settings, and (when no object model is installed) kicks off a
    background download of the default one.

    Returns the healed settings dict, or ``None`` when nothing needed fixing.
    Called once at startup before the detectors are constructed; the next
    ``effective_ai_config()`` then sees the repaired state.
    """
    settings = effective_ai_config()
    enabled = settings.get('enabled', True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {'1', 'true', 'yes', 'on'}
    if not enabled:
        return None
    if not is_face_family_model(settings.get('model_path'), settings.get('labels_path')):
        return None

    legacy_face_path = _canonical_models_path(settings.get('model_path'), 'model_path')
    object_rel = find_installed_object_model() or 'models/yolo11n.onnx'
    payload: dict[str, Any] = {
        **settings,
        # Legacy face model -> secondary face pass (runs ALONGSIDE objects).
        'face_enabled': True,
        'face_model_path': legacy_face_path,
        'face_labels_path': str(settings.get('face_labels_path') or 'models/face.names'),
        'face_keypoint_count': int(settings.get('face_keypoint_count') or 5),
        # Restore a COCO object model as PRIMARY so person/car/... detection
        # (and therefore object recordings) work again.
        'model_path': object_rel,
        'labels_path': 'models/coco.names',
        'keypoint_count': 0,
    }
    try:
        updated = validate_ai_settings(payload)
    except HTTPException:
        # validate_ai_settings refuses a not-yet-downloaded model_path (its
        # typo guard). This migration is a trusted internal repair, so fall
        # back to the hand-built payload rather than leaving the operator
        # stuck with a face-only detector until they install a model.
        updated = dict(payload)
    from app.auth import utc_now

    _state.database.set_setting('ai', updated, utc_now())
    logger.warning(
        'Healed legacy AI settings: face model %s moved to the secondary Face '
        'Detection pass; primary object model restored to %s.',
        legacy_face_path, object_rel,
    )
    if not (BASE_DIR / object_rel).exists():
        # No object model installed (a face model was the only ONNX on disk):
        # download + activate the default in the background so detection
        # recovers without operator action. ``switch_active=True`` also
        # triggers the detector reload once the export lands.
        def _download_default_object_model() -> None:
            try:
                from app.model_management import _do_download_model

                _do_download_model(_DEFAULT_MODEL, switch_active=True, imgsz=640)
                logger.info('Auto-download of replacement object model %s completed.', _DEFAULT_MODEL)
            except Exception as exc:  # pragma: no cover - best-effort recovery
                logger.warning(
                    'Auto-download of replacement object model failed: %s. '
                    'Download an object model from the Models tab.', exc,
                )

        threading.Thread(
            target=_download_default_object_model,
            name='object-model-recovery-download',
            daemon=True,
        ).start()
    return updated


def active_ai_config_source() -> str:
    if _state.database.has_setting('ai'):
        return 'database'
    if config_file_path().exists():
        return 'config.yaml'
    return 'default'


def onnx_runtime_installed() -> bool:
    return importlib.util.find_spec('onnxruntime') is not None


def model_exists(ai_settings: dict[str, Any]) -> bool:
    model_path = str(ai_settings.get('model_path') or '').strip()
    if not model_path:
        return False
    path = Path(model_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.is_file()


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
        'AI detector %s: active_backend=%s configured_backend=%s model_loaded=%s inference_available=%s active_precision=%s requested_precision=%s providers=%s model_path=%s labels_path=%s error=%s',
        context, ai_status['active_backend'], ai_status['configured_backend'], ai_status['model_loaded'],
        ai_status['inference_available'], ai_status.get('active_precision') or '<none>',
        str(ai_status.get('precision') or '<none>').lower(), providers_str,
        ai_status['model_path'] or '<none>', ai_status['labels_path'] or '<none>',
        ai_status['error'] or '<none>',
    )


def ai_status_payload(
    ai_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = ai_settings or effective_ai_config()
    active_backend = getattr(_state.detector, 'backend', 'unknown')
    configured_backend = str(settings.get('backend', 'onnx')).lower()
    # ``enabled`` is the master toggle for object detection. Coerce legacy
    # string forms (config YAML / old payloads) the same way
    # ``validate_ai_settings`` does, so a 'false' string reliably reports
    # AI DISABLED rather than being truthy.
    enabled = settings.get('enabled', True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {'1', 'true', 'yes', 'on'}
    else:
        enabled = bool(enabled)
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
    if not enabled:
        mode = 'AI DISABLED'
    elif configured_backend == 'onnx' and (not exists):
        mode = 'MODEL MISSING'
        error = error or f"ONNX model not found: {settings.get('model_path')}"
    elif configured_backend == 'onnx' and (not model_loaded):
        mode = 'MODEL FAILED'
    elif configured_backend == 'onnx':
        mode = 'ONNX ACTIVE'
        error = detector_reason
    else:
        mode = 'MODEL FAILED'
    # A disabled master toggle means no inference is available even when the
    # detector session is resident (it is only rebuilt on reload).
    inference_available = bool(enabled and detector_loaded)
    model_path_str = str(settings.get('model_path') or '')
    model_filename = Path(model_path_str).name if model_path_str else ''
    model_label = next(
        (
            info['label']
            for info in YOLO_MODELS.values()
            if (
                info['onnx'] == model_filename
                or model_filename.startswith(f"{Path(info['onnx']).stem}-")
            )
        ),
        None,
    )
    # Same catalog-name lookup for the secondary face model so the ONNX status
    # card can render ``Face Model`` exactly like the primary ``Model`` row
    # (bold catalog name + muted path) instead of a bare path.
    face_model_path_str = str(settings.get('face_model_path') or '')
    face_model_filename = Path(face_model_path_str).name if face_model_path_str else ''
    face_model_label = next(
        (
            info['label']
            for info in YOLO_MODELS.values()
            if (
                info['onnx'] == face_model_filename
                or face_model_filename.startswith(f"{Path(info['onnx']).stem}-")
            )
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
    # The device actually running inference, resolved from ORT's live provider
    # list (so ``Device: Auto`` shows which way it landed, and a CUDA request
    # that fell back to CPU is visible). Drives the Status panel's "Active
    # Device" row and tells operators which half of the Advanced settings --
    # the CPU group or the GPU group -- actually applies to their host.
    active_providers = getattr(_state.detector, 'active_providers', None) if model_loaded else None
    if active_providers:
        active_device = 'CUDA (GPU)' if 'CUDAExecutionProvider' in active_providers else 'CPU'
    else:
        active_device = None
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
        'active_device': active_device,
        # Keep the persisted request beside the runtime result so operators
        # can distinguish an intentional FP32 fallback from an FP32 setting.
        'precision': str(settings.get('precision') or 'fp32').strip().lower(),
        # Secondary face detector status (optional parallel pass).
        'face_enabled': bool(settings.get('face_enabled')),
        'face_model_path': str(settings.get('face_model_path') or ''),
        'face_model_name': face_model_label,
        'face_model_loaded': bool(
            getattr(_state.face_detector, 'available', False)
        ),
        # Legacy-state flag: a face-family file in the PRIMARY slot means only
        # faces are detected (no objects). The startup heal repairs this, but
        # surfaces still show the warning while it persists.
        'primary_is_face_model': is_face_family_model(
            model_path_str, str(settings.get('labels_path') or '')
        ),
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
    # The secondary face detector contributes its own label(s). Merge them so
    # the Zones / Objects pages can write rules for ``face`` even while a COCO
    # object model is the active primary detector.
    if ai_settings.get('face_enabled'):
        face_labels = load_labels(ai_settings.get('face_labels_path') or 'models/face.names', ['face'])
        labels = labels + [label for label in face_labels if label and label not in labels]
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
        'active_device': ai_status.get('active_device'),
        'model_name': ai_status.get('model_name'),
        # Secondary face detector runtime state. These are COMPUTED fields
        # (from ``_state.face_detector``), not persisted settings, so they must
        # be passed through explicitly -- ``**ai_settings`` only carries stored
        # keys, which previously dropped ``face_model_loaded`` from this payload
        # and left the ONNX status card stuck on "Not loaded" even while the
        # face detector was running.
        'face_enabled': ai_status['face_enabled'],
        'face_model_path': ai_status['face_model_path'],
        'face_model_name': ai_status.get('face_model_name'),
        'face_model_loaded': ai_status['face_model_loaded'],
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
        'input_size',
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
        'keypoint_count',
        # Secondary face detector: runs a dedicated face model alongside the
        # primary object model so COCO objects and faces are detected in
        # parallel on the same frame.
        'face_enabled',
        'face_model_path',
        'face_labels_path',
        'face_keypoint_count',
        'face_confidence',
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
    raw_input_size = updated.get('input_size', 640)
    if isinstance(raw_input_size, bool):
        raise HTTPException(status_code=400, detail='input_size must be an integer between 32 and 2048.')
    try:
        input_size = int(raw_input_size)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='input_size must be an integer between 32 and 2048.') from exc
    if not 32 <= input_size <= 2048:
        raise HTTPException(status_code=400, detail='input_size must be an integer between 32 and 2048.')
    updated['input_size'] = input_size
    # ``keypoint_count`` marks a pose/keypoint detection head (e.g. a YOLO-face
    # export with a 5-point landmark head). It is normally set for the operator
    # by the model download flow from the catalog entry, not typed by hand, but
    # is validated here so a hand-edited API payload can't smuggle a bad value
    # onto the detector. ``0`` (the default) is a plain detection head.
    raw_keypoint_count = updated.get('keypoint_count', 0)
    if isinstance(raw_keypoint_count, bool):
        raise HTTPException(status_code=400, detail='keypoint_count must be a non-negative integer.')
    try:
        keypoint_count = int(raw_keypoint_count) if raw_keypoint_count not in (None, '') else 0
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='keypoint_count must be a non-negative integer.') from exc
    if not 0 <= keypoint_count <= 32:
        raise HTTPException(status_code=400, detail='keypoint_count must be between 0 and 32.')
    updated['keypoint_count'] = keypoint_count
    # ---- Secondary face detector settings ---------------------------------
    face_enabled = updated.get('face_enabled', False)
    if isinstance(face_enabled, str):
        face_enabled = face_enabled.strip().lower() in {'1', 'true', 'yes', 'on'}
    else:
        face_enabled = bool(face_enabled)
    updated['face_enabled'] = face_enabled
    raw_face_model = str(updated.get('face_model_path') or '').strip()
    if raw_face_model:
        face_model_path = _canonical_models_path(raw_face_model, 'face_model_path')
        # Typo protection mirrors model_path: only when the caller explicitly
        # supplied a NEW non-empty face model path.
        if 'face_model_path' in payload and str(payload.get('face_model_path') or '').strip():
            current_face_canon = ''
            if current.get('face_model_path'):
                try:
                    current_face_canon = _canonical_models_path(current['face_model_path'], 'face_model_path')
                except HTTPException:
                    current_face_canon = ''
            if face_model_path != current_face_canon and not (BASE_DIR / face_model_path).exists():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f'Face ONNX model file not found: {face_model_path}. '
                        'Download a face model first, or choose an installed model.'
                    ),
                )
        updated['face_model_path'] = face_model_path
    else:
        updated['face_model_path'] = current.get('face_model_path') or ''
        updated['face_enabled'] = False if not updated['face_model_path'] else face_enabled
    raw_face_labels = updated.get('face_labels_path') or 'models/face.names'
    updated['face_labels_path'] = _canonical_models_path(raw_face_labels, 'face_labels_path')
    raw_face_keypoints = updated.get('face_keypoint_count', 5)
    if isinstance(raw_face_keypoints, bool):
        raise HTTPException(status_code=400, detail='face_keypoint_count must be a non-negative integer.')
    try:
        face_keypoint_count = int(raw_face_keypoints) if raw_face_keypoints not in (None, '') else 5
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='face_keypoint_count must be a non-negative integer.') from exc
    if not 0 <= face_keypoint_count <= 32:
        raise HTTPException(status_code=400, detail='face_keypoint_count must be between 0 and 32.')
    updated['face_keypoint_count'] = face_keypoint_count
    raw_face_conf = updated.get('face_confidence')
    if raw_face_conf in (None, ''):
        # Blank = the default of 0.45 (matches the global Min Confidence default).
        updated['face_confidence'] = 0.45
    else:
        try:
            face_confidence = float(raw_face_conf)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='face_confidence must be a number.') from exc
        if not 0 <= face_confidence <= 1:
            raise HTTPException(status_code=400, detail='face_confidence must be between 0 and 1.')
        updated['face_confidence'] = face_confidence
    raw_model_path = updated.get('model_path') or current.get('model_path') or 'models/yolo11n.onnx'
    model_path = _canonical_models_path(raw_model_path, 'model_path')
    # Parallel-detector invariant: the PRIMARY slot must stay an object model.
    # A face-family model here detects ONLY faces -- objects vanish from the
    # Objects page and object recordings stop. Face models belong in the
    # secondary Face Detection pass (``face_model_path``), which runs
    # alongside the object model on the same frames. Checked BEFORE any
    # existence guard so the rejection never depends on download state.
    if is_face_family_model(model_path):
        raise HTTPException(
            status_code=400,
            detail=(
                f'{model_path} is a face-detection model and cannot be the '
                'primary object model: it would disable object detection '
                'entirely. Choose an object model (YOLOv8/YOLO11/YOLO26) as '
                'the Model Path; face models are selected under Face Model '
                'so both detectors run in parallel.'
            ),
        )
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
