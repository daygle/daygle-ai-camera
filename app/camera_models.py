"""Per-camera YOLO model assignment helpers.

A camera may carry its own object-detector override inside its ``detection``
block: ``model_path`` (an ONNX file inside ``models/``) plus an optional
``labels_path``. When no override is set the camera runs the global primary
detector (``ai.model_path``) exactly as before.

Cluster membership:

- ``normalize_camera_model_path`` / ``normalize_camera_labels_path`` -
  canonicalise the stored override values for settings-schema hygiene. The
  strict variants raise ``HTTPException(400)`` on invalid input so an API
  caller sees a clear error instead of a silent clear.
- ``camera_model_assignment`` - the effective ``(model_path, labels_path)``
  pair for one camera, or ``None`` when it follows the global default.
- ``camera_detector`` - the detector instance a camera's live cycle should
  use. Cameras without an override share ``state.detector``; cameras with an
  override share a cached per-model ``OnnxYoloDetector`` so several cameras
  can run different YOLO models concurrently while cameras that share a
  model share one ONNX session.
- ``clear_camera_model_cache`` / ``evict_camera_model_cache`` - cache
  lifecycle hooks called when the global detector is reloaded and when a
  model file is deleted.
- ``list_assignable_models`` - the installed model rows offered by the
  ``/camera-models`` assignment UI.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.ai_settings import (
    BASE_DIR,
    MODELS_DIR,
    YOLO_MODELS,
    _canonical_models_path,
    is_face_family_model,
)

DEFAULT_LABELS_PATH = 'models/coco.names'

# Per-model detector cache: ``(model_path, labels_path) -> OnnxYoloDetector``.
# Built lazily on the first cycle of the first camera using that model; plain
# dict reads are atomic so the hot path only takes the lock on a cache miss.
_detector_cache: dict[tuple[str, str], Any] = {}
_cache_lock = threading.Lock()

# Resolution-variant exports are named ``yolo11n-416.onnx`` (see
# ``model_management._resolution_filename``); tolerate a ``_416`` form too.
_VARIANT_SUFFIX_RE = re.compile(r'[-_](\d{3,4})$')


def normalize_camera_model_path(value: Any, *, strict: bool = False) -> str | None:
    """Canonicalise a per-camera ``model_path`` override.

    Empty/blank input means "no assignment" and returns ``None``. A non-empty
    value must point inside ``models/``, end in ``.onnx``, and be an
    object-detection model: face-family models run in the separate face pass
    (``ai.face_model_path``) and are rejected here so a face model can never
    be wired into the object pipeline as a camera's primary detector. Invalid
    input raises ``HTTPException(400)`` in strict mode and returns ``None``
    otherwise (settings-schema hygiene on read).
    """
    # Windows-style separators are normalised first so a path saved by a
    # Windows browser stays canonical on a Linux host.
    text = str(value or '').strip().replace('\\', '/')
    if not text:
        return None

    def _reject(detail: str) -> None:
        if strict:
            raise HTTPException(status_code=400, detail=detail)

    try:
        canonical = _canonical_models_path(text, 'model_path')
    except HTTPException:
        if strict:
            raise
        return None
    if not canonical.lower().endswith('.onnx'):
        _reject('model_path must point to a .onnx file inside the models/ directory.')
        return None
    if is_face_family_model(canonical):
        _reject(
            'Face models run in the separate face-detection pass; '
            'assign an object-detection model instead.'
        )
        return None
    return canonical


def normalize_camera_labels_path(value: Any, *, strict: bool = False) -> str | None:
    """Canonicalise a per-camera ``labels_path`` override.

    Same contract as :func:`normalize_camera_model_path`: empty input returns
    ``None`` (the caller falls back to ``models/coco.names``), anything else
    must stay inside ``models/``.    Face label files are rejected in strict mode
    for the same reason face models are.
    """
    text = str(value or '').strip().replace('\\', '/')
    if not text:
        return None
    try:
        canonical = _canonical_models_path(text, 'labels_path')
    except HTTPException:
        if strict:
            raise
        return None
    if is_face_family_model('', canonical):
        if strict:
            raise HTTPException(
                status_code=400,
                detail='Face labels belong to the face-detection pass; assign object labels instead.',
            )
        return None
    return canonical


def camera_model_assignment(camera_settings: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(model_path, labels_path)`` for a camera, or ``None``.

    ``None`` means the camera follows the global default detector. The labels
    path defaults to ``models/coco.names`` when only a model is assigned.
    """
    detection = camera_settings.get('detection')
    if not isinstance(detection, dict):
        return None
    model_path = normalize_camera_model_path(detection.get('model_path'))
    if model_path is None:
        return None
    labels_path = normalize_camera_labels_path(detection.get('labels_path')) or DEFAULT_LABELS_PATH
    return (model_path, labels_path)


def _catalog_entry_for(model_path: str) -> dict[str, Any] | None:
    """Match a model filename (or a ``_416``-style resolution variant) to its
    ``YOLO_MODELS`` catalog entry."""
    name = Path(str(model_path or '')).name
    stem = name[:-5] if name.lower().endswith('.onnx') else name
    base = _VARIANT_SUFFIX_RE.sub('', stem)
    for info in YOLO_MODELS.values():
        onnx_stem = Path(str(info.get('onnx') or '')).stem
        if stem == onnx_stem or base == onnx_stem:
            return info
    return None


def _model_label(model_path: str) -> str:
    """Human-readable label for a model file, preferring catalog metadata."""
    name = Path(str(model_path or '')).name
    info = _catalog_entry_for(model_path)
    if info is None:
        return name[:-5] if name.lower().endswith('.onnx') else name
    label = str(info.get('label') or name)
    match = _VARIANT_SUFFIX_RE.search(Path(name).stem)
    if match:
        size = match.group(1)
        return f'{label} · {size}×{size}'
    return label


def _model_input_size(model_path: str) -> int:
    info = _catalog_entry_for(model_path)
    try:
        return int(info.get('input_size')) if info else 640
    except (TypeError, ValueError):
        return 640


def camera_detector(camera_settings: dict[str, Any], ai_config: dict[str, Any] | None = None) -> Any:
    """Return the detector this camera's live cycle should use.

    No override (or an override naming the same model the global detector
    already runs) returns ``state.detector`` so nothing changes for the common
    case. Otherwise a cached per-model detector is returned, constructed with
    the global runtime tuning (device, precision, threads) but re-derived
    model-specific knobs (NMS-free head detection, input size) so the global
    model's head settings never leak onto a different model.
    """
    assignment = camera_model_assignment(camera_settings)
    if assignment is None:
        return _state.detector
    if ai_config is None:
        from app.config_facades import effective_ai_config
        ai_config = effective_ai_config()
    model_path, labels_path = assignment
    from app.model_management import _normalise_model_path, _same_model_path
    global_model = _normalise_model_path(ai_config.get('model_path'))
    global_labels = _normalise_model_path(ai_config.get('labels_path') or DEFAULT_LABELS_PATH)
    if _same_model_path(global_model, model_path) and _same_model_path(global_labels, labels_path):
        return _state.detector
    key = (model_path, labels_path)
    cached = _detector_cache.get(key)
    if cached is not None:
        return cached
    from app.detector import create_detector
    detector = create_detector({
        **ai_config,
        'backend': 'onnx',
        'model_path': model_path,
        'labels_path': labels_path,
        # Re-derived per model: NMS-free auto-detects from the filename, the
        # input size comes from the catalog when known, and keypoint heads
        # belong to face models (excluded from assignment).
        'categories': [],
        'nms_free': None,
        'keypoint_count': 0,
        'input_size': _model_input_size(model_path),
    })
    with _cache_lock:
        # Insert-if-absent: a concurrent cycle may have built the same model
        # first; keep whichever landed so cameras always share one session.
        return _detector_cache.setdefault(key, detector)


def clear_camera_model_cache() -> None:
    """Drop every cached per-camera detector (global detector reload)."""
    with _cache_lock:
        _detector_cache.clear()


def evict_camera_model_cache(model_path: Any) -> None:
    """Drop cached detectors running ``model_path`` (model file deleted)."""
    from app.model_management import _normalise_model_path, _same_model_path
    target = _normalise_model_path(model_path)
    with _cache_lock:
        for key in [k for k in _detector_cache if _same_model_path(k[0], target)]:
            _detector_cache.pop(key, None)


def list_assignable_models() -> list[dict[str, Any]]:
    """Installed ``models/*.onnx`` files offered by the assignment UI.

    Face-family models are listed (so the UI can explain why they are not
    assignable) but marked ``assignable: false``; they run in the separate
    face-detection pass.
    """
    rows: list[dict[str, Any]] = []
    try:
        candidates = sorted(MODELS_DIR.glob('*.onnx'))
    except OSError:
        candidates = []
    for path in candidates:
        if not path.is_file():
            continue
        rel = path.relative_to(BASE_DIR).as_posix()
        face = is_face_family_model(rel)
        try:
            size_bytes: int | None = path.stat().st_size
        except OSError:
            size_bytes = None
        rows.append({
            'path': rel,
            'label': _model_label(rel),
            'family': 'face' if face else 'object',
            'assignable': not face,
            'input_size': _model_input_size(rel),
            'size_bytes': size_bytes,
        })
    rows.sort(key=lambda row: (row['family'] != 'object', row['label'].lower()))
    return rows


def camera_model_row(
    camera_settings: dict[str, Any],
    ai_config: dict[str, Any],
) -> dict[str, Any]:
    """One ``cameras`` row for the GET /api/camera-models payload."""
    assignment = camera_model_assignment(camera_settings)
    default_model = str(ai_config.get('model_path') or '')
    default_labels = str(ai_config.get('labels_path') or DEFAULT_LABELS_PATH)
    if assignment is None:
        model_path, labels_path = None, None
        source = 'default'
    else:
        model_path, labels_path = assignment
        source = 'assigned'
    effective_model = model_path or default_model
    effective_labels = labels_path or default_labels
    return {
        'id': str(camera_settings.get('id') or ''),
        'name': str(camera_settings.get('name') or ''),
        'model_path': model_path,
        'labels_path': labels_path,
        'source': source,
        'model_name': _model_label(effective_model) if effective_model else None,
        'effective_model_path': effective_model or None,
        'effective_labels_path': effective_labels,
        'model_exists': bool(effective_model) and (BASE_DIR / effective_model).is_file(),
    }
