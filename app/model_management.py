"""YOLO model download and metadata helpers extracted from ``app/main.py`` (Phase-I).

Cluster membership:
- ``_installed_models_lock`` - threading.Lock guarding installed-models JSON I/O
- ``PYPI_ULTRALYTICS_URL`` - PyPI endpoint for Ultralytics version checks
- ``BASE_DIR`` - project root (same as ``main.BASE_DIR``)
- ``_installed_models_path()`` - path to models/installed.json
- ``_read_installed_models()`` - read installed-models JSON
- ``_write_installed_models(data)`` - write installed-models JSON
- ``_sha256_file(path)`` - SHA-256 digest of a file
- ``_installed_package_version(package)`` - importlib.metadata version lookup
- ``_fetch_ultralytics_version()`` - fetch latest Ultralytics version from PyPI
- ``_parse_semver(v)`` - parse a semantic version string to a tuple
- ``_fetch_models_manifest()`` - build the remote YOLO export-version manifest
- ``export_yolo_onnx(model_name, destination)`` - run Ultralytics YOLO export
- ``_do_download_model(model_name, switch_active)`` - full export + persist + reload flow

Pool-C reach (resolved lazily via lazy imports inside function bodies):
- ``app.main.reload_detector`` (``_do_download_model``)
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import shutil
import urllib.request
from pathlib import Path
from typing import Any


def _onnxsim_available() -> bool:
    """Return ``True`` iff the ``onnxsim`` package is importable.

    Ultralytics' ``YOLO(...).export(simplify=True)`` requires ``onnxsim`` at
    runtime; without it the export raises. We detect availability up front so
    hosts that have not installed ``onnxsim`` (e.g. the export feature was
    never their concern) still get a working ONNX file: we just skip the
    constant-folding pass instead of failing the download with a 502.
    """
    return importlib.util.find_spec('onnxsim') is not None


def _export_kwargs(nms_free: bool, precision: str = 'fp32', device: str = 'auto') -> str:
    """Build the ``YOLO(...).export(...)`` kwargs string for the model variant.

    Adds ``simplify=True`` when ``onnxsim`` is importable; otherwise omits it
    silently so the export still succeeds on minimal installs. YOLO26 models
    additionally need ``end2end=True`` to fold NMS into the graph and emit the
    ``[N, 6]`` output ``_postprocess_nms_free`` decodes.

    ``opset=13`` is always set so the newer ONNX graph-fusion passes in ORT
    are usable on the exported model (the Ultralytics default opset often
    lands below 13 and skips some CPU-friendly fusions).

    ``precision='fp16'`` emits ``half=True`` only when ``device='cuda'`` AND
    ``onnxruntime-gpu`` is registered on the host. On CPU-only hosts (the
    default deployment) we deliberately drop ``half=True`` so the export
    doesn't crash trying to use FP16 CUDA tensors that the local Ultralytics
    install can't emit -- an FP32 export is still a valid ORT input.
    ``precision='int8'`` is NOT honored at export time (Ultralytics would
    require a calibration dataset which the download flow doesn't carry);
    INT8 is a runtime concern handled by ``app.quantization.quantize_int8``.
    """
    parts = ["format='onnx'", f"end2end={'True' if nms_free else 'False'}", 'opset=13']
    # Delegate the precision rule to its single source of truth in
    # ``app.quantization.precision_export_kwargs``: fp16 emits ``half=True``
    # only when ``device='cuda'`` AND onnxruntime-gpu is registered; fp32 and
    # int8 never contribute export kwargs here (INT8 is a runtime concern
    # handled by ``quantize_int8`` at detector load time).
    from app.quantization import onnxruntime_gpu_available, precision_export_kwargs
    precision_kwargs = precision_export_kwargs(
        precision, device, onnxruntime_gpu_available()
    )
    if precision_kwargs:
        parts.append(precision_kwargs)
    elif precision == 'fp16' and device.lower() == 'cuda':
        # Keep the operator-visible warning for the fp16+cuda-without-GPU
        # case so the silent FP32 export is not hidden.
        import logging
        logging.getLogger('daygle.ai').warning(
            'precision=fp16 requested but onnxruntime-gpu is not installed; '
            'exporting FP32. Install onnxruntime-gpu + CUDA drivers and re-export.',
        )
    if _onnxsim_available():
        parts.append('simplify=True')
    return ', '.join(parts)

from fastapi import HTTPException

import app.state as _state
from app.ai_settings import YOLO_MODELS, detector_status, validate_ai_settings
from app.auth import utc_now
from app.config_facades import effective_ai_config

logger = logging.getLogger('daygle.ai')

BASE_DIR = Path(__file__).resolve().parent.parent
PYPI_ULTRALYTICS_URL = 'https://pypi.org/pypi/ultralytics/json'
_installed_models_lock = threading.Lock()
# Ultralytics exports and weight-cache updates are process-local and must not
# overlap: two resolutions of the same model can otherwise race on the source
# .pt cache or the temporary export lifecycle.
_model_export_lock = threading.Lock()


MODELS_DIR: Path = BASE_DIR / 'models'


def _relative_model_path(path: Path) -> str:
    """Return a stable forward-slash path for settings/API payloads."""
    return path.relative_to(BASE_DIR).as_posix()


def _normalise_model_path(value: Any) -> str:
    return str(value or '').replace('\\', '/')


def _same_model_path(left: Any, right: Any) -> bool:
    """Compare project-relative/absolute model paths canonically."""
    def resolve(value: Any) -> Path:
        path = Path(_normalise_model_path(value))
        return (path if path.is_absolute() else BASE_DIR / path).resolve()

    return bool(left) and resolve(left) == resolve(right)


def _resolution_filename(model_name: str, imgsz: int) -> str:
    """Return the stable filename for one model/export resolution."""
    info = YOLO_MODELS[model_name]
    stem = Path(info['onnx']).stem
    return f'{stem}-{int(imgsz)}.onnx'


def _resolution_path(model_name: str, imgsz: int) -> Path:
    return _safe_within_models_dir(_resolution_filename(model_name, imgsz))


def _model_variants(model_name: str, installed_meta: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Collect installed resolution variants, including legacy flat metadata."""
    info = YOLO_MODELS[model_name]
    meta = installed_meta if installed_meta is not None else _read_installed_models()
    raw = meta.get(model_name, {})
    variants: dict[str, dict[str, Any]] = {}
    stored_variants = raw.get('variants') if isinstance(raw, dict) else None
    if isinstance(stored_variants, dict):
        for key, value in stored_variants.items():
            if isinstance(value, dict):
                variants[str(key)] = dict(value)
    elif isinstance(raw, dict) and raw:
        # Metadata written before resolution-specific artifacts existed. Keep
        # its original fixed filename when that file is still present; older
        # installs may have exported the legacy file at a non-default size.
        size = raw.get('imgsz', info.get('input_size', 640))
        variants[str(size)] = dict(raw)
        if (MODELS_DIR / info['onnx']).is_file():
            variants[str(size)].setdefault('path', _relative_model_path(MODELS_DIR / info['onnx']))

    # Discover files created by an earlier/newer process even if metadata was
    # interrupted. Only the exact model stem and numeric suffix are accepted.
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(info['onnx']).stem
    for path in MODELS_DIR.glob(f'{stem}-*.onnx'):
        match = re.fullmatch(re.escape(stem) + r'-(\d+)\.onnx', path.name)
        if match:
            variants.setdefault(match.group(1), {'imgsz': int(match.group(1))})

    # Preserve support for the original fixed filename. A resolution-specific
    # artifact takes precedence for the same size, but the legacy file is never
    # deleted or overwritten by the new flow.
    legacy = MODELS_DIR / info['onnx']
    legacy_size = str(info.get('input_size', 640))
    has_flat_metadata = (
        isinstance(raw, dict)
        and bool(raw)
        and not isinstance(raw.get('variants'), dict)
    )
    legacy_rel = _relative_model_path(legacy) if legacy.is_file() else ''
    legacy_already_represented = any(
        _normalise_model_path(value.get('path')) == legacy_rel
        for value in variants.values()
        if isinstance(value, dict)
    )
    if legacy.is_file() and not has_flat_metadata and not legacy_already_represented:
        variants.setdefault(legacy_size, {'imgsz': int(legacy_size), 'path': legacy_rel})
    for key, value in variants.items():
        value.setdefault('imgsz', int(key))
        if value.get('path'):
            value['path'] = _normalise_model_path(value['path'])
        if not value.get('path'):
            size = int(value['imgsz'])
            value['path'] = _relative_model_path(
                legacy if size == int(legacy_size) and legacy.is_file() else _resolution_path(model_name, size)
            )
    # Do not advertise metadata-only artifacts that disappeared or were
    # manually removed. Also reject tampered metadata paths that escape the
    # models directory before they can reach the API/UI.
    models_root = MODELS_DIR.resolve()
    valid_variants: dict[str, dict[str, Any]] = {}
    for key, value in variants.items():
        candidate = (BASE_DIR / _normalise_model_path(value['path'])).resolve()
        if candidate.is_relative_to(models_root) and candidate.is_file():
            valid_variants[key] = value
    return valid_variants


def _safe_within_models_dir(filename: str) -> Path:
    """Resolve ``filename`` against ``MODELS_DIR`` and refuse traversal.

    M4 (round-7) defence-in-depth for any code path that takes a
    user/operator-supplied filename and joins it onto the models directory:

    - Requires a plain basename; absolute paths, separators, and parent
      components are rejected instead of being silently stripped.
    - Rejects empty / dot-only / leading-dot names so a misconfigured
      ``YOLO_MODELS[model_name]['onnx'] = ''`` cannot resolve to MODELS_DIR
      itself and ``.bashrc`` / ``..`` cannot pass through.
    - Confirms the resolved path stays inside ``MODELS_DIR``.

    Raises ``RuntimeError`` on rejection. Download flows map this to a
    generic HTTP 502 response so malformed internal registry data cannot
    become an unhandled server error or leak path details.

    The check is deliberately tighter than ``PurePath.is_relative_to``:
    a filename like ``yolov8n.pt`` resolves inside MODELS_DIR, while
    ``../etc/passwd`` and ``models/yolov8n.pt`` are rejected before
    resolution because they are not plain basenames.
    """
    if not isinstance(filename, str) or not filename.strip():
        raise RuntimeError('Model filename must be a non-empty string.')
    name = filename.strip()
    # Do not normalize away path components: callers of this helper are
    # expected to provide a model filename, not a path. Reject both POSIX and
    # Windows separators so this remains safe if a config is moved between
    # platforms.
    if (
        not name
        or name in {'.', '..'}
        or name.startswith('.')
        or '/' in name
        or '\\' in name
        or Path(name).is_absolute()
        or any(part == '..' for part in Path(name).parts)
    ):
        raise RuntimeError('Model filename must be a non-hidden basename.')
    resolved = (MODELS_DIR / name).resolve()
    if not resolved.is_relative_to(MODELS_DIR.resolve()):
        raise RuntimeError('Model filename resolved outside the models directory.')
    return resolved


def _installed_models_path() -> Path:
    # Use the M4 helper so this function is robust to any future caller
    # that tries to re-use it with a derived / templated name. Today it's
    # only called with the literal ``'installed.json'`` (a basename that
    # trivially passes the check), but routing through the helper keeps
    # the invariant in one place.
    resolved = _safe_within_models_dir('installed.json')
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _read_installed_models() -> dict[str, Any]:
    p = _installed_models_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception as exc:
            logger.warning('Failed to read settings file: %s', exc)
            return {}
    return {}


def _write_installed_models(data: dict[str, Any]) -> None:
    p = _installed_models_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding='utf-8')


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _installed_package_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'


def _fetch_ultralytics_version() -> str:
    req = urllib.request.Request(PYPI_ULTRALYTICS_URL, headers={'User-Agent': 'daygle-ai-camera-updater/1.0'})
    with urllib.request.urlopen(req, timeout=10) as response:
        payload = json.loads(response.read())
    version = str(payload.get('info', {}).get('version') or '').strip()
    if not version:
        raise RuntimeError('PyPI ultralytics response did not include a version.')
    return version


def _parse_semver(v: str) -> tuple[int, ...]:
    try:
        return tuple((int(x) for x in v.split('.')))
    except ValueError:
        return (0,)


def _fetch_models_manifest() -> dict[str, Any]:
    """Return remote YOLO export versions from the upstream Ultralytics package.

    The app exports ONNX files from Ultralytics YOLO weights, so the remote
    version that matters for update checks is the latest Ultralytics release,
    not a Daygle-maintained model manifest version.

    The effective version is capped at the locally installed package version:
    if PyPI has a newer release but the local package hasn't been upgraded yet,
    re-exporting the model would produce the same file, so no update is shown.
    """
    remote_version = _fetch_ultralytics_version()
    local_version = _installed_package_version('ultralytics')
    if local_version != 'unknown' and _parse_semver(local_version) < _parse_semver(remote_version):
        effective_version = local_version
    else:
        effective_version = remote_version
    return {'updated_at': None, 'source': 'pypi:ultralytics', 'models': {model_id: {'version': effective_version} for model_id in YOLO_MODELS}}


def export_yolo_onnx(model_name: str, destination: Path, imgsz: int = 640, precision: str = 'fp32', device: str = 'auto') -> int:
    if model_name not in YOLO_MODELS:
        raise ValueError(f"Unknown model '{model_name}'. Available: {', '.join(YOLO_MODELS)}")
    info = YOLO_MODELS[model_name]
    pt_name = info['pt']
    nms_free = info.get('nms_free', False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Pass the weights name and image size as argv rather than interpolating
    # into the ``python -c`` source.  Defence-in-depth against injection.
    #
    # YOLO26 models need end2end=True for NMS-free export;
    # YOLOv8/YOLO11 models use standard export (NMS handled at runtime).
    # ``simplify=True`` is added when onnxsim is importable so the constant-
    # folding pass runs without ever breaking the export on minimal installs.
    # ``opset=13`` unlocks the newer ORT graph-fusion passes (the default
    # Ultralytics picks is often ``opset=12`` which skips some fusions that
    # matter on the per-camera hot path).
    # ``precision='fp16'`` emits ``half=True`` only when the host has
    # onnxruntime-gpu -- on CPU-only deployments it silently drops so the
    # export still succeeds. INT8 is NOT honored here; it's a runtime
    #    concern handled by ``app.quantization.quantize_int8``.
    export_kwargs = _export_kwargs(nms_free, precision=precision, device=device)
    cached_weights = MODELS_DIR / pt_name
    weights_arg = pt_name
    export_script = (
        "import sys\n"
        "from ultralytics import YOLO\n"
        f"YOLO(sys.argv[1]).export({export_kwargs}, imgsz=int(sys.argv[2]))\n"
    )
    command = [sys.executable, '-c', export_script, pt_name, str(imgsz)]
    # Ultralytics always emits ``<weights-stem>.onnx``. Export resolution
    # variants in an isolated directory so producing ``yolo11n-1024.onnx``
    # cannot overwrite the legacy ``yolo11n.onnx`` or another variant.
    export_dir = destination.parent
    temporary_export_dir: tempfile.TemporaryDirectory[str] | None = None
    if destination.name != info['onnx']:
        temporary_export_dir = tempfile.TemporaryDirectory(prefix='daygle-onnx-', dir=str(destination.parent))
        export_dir = Path(temporary_export_dir.name)
        # Keep Ultralytics' relative-weight lookup/cache behavior while
        # isolating the export output. A cached local weight is copied into the
        # temporary directory; otherwise Ultralytics may download it there.
        if cached_weights.is_file():
            shutil.copy2(cached_weights, export_dir / pt_name)
    try:
        command = [sys.executable, '-c', export_script, weights_arg, str(imgsz)]
        result = subprocess.run(command, cwd=export_dir, capture_output=True, text=True, timeout=600, check=False)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or '').strip()
            raise RuntimeError(details or f'Ultralytics export exited with status {result.returncode}.')
        exported = export_dir / info['onnx']
        if exported != destination and exported.exists():
            exported.replace(destination)
        if not destination.exists():
            details = (result.stderr or result.stdout or '').strip()
            raise RuntimeError(details or f'Ultralytics export did not create {destination.name}.')
        if destination.stat().st_size <= 0:
            destination.unlink(missing_ok=True)
            raise RuntimeError('Exported model file is empty.')
        return destination.stat().st_size
    finally:
        if temporary_export_dir is not None:
            # Ultralytics may download the source weights into its cwd when
            # they were not already cached. Preserve that download before the
            # isolated directory is removed, so the next resolution reuses it.
            downloaded_weights = export_dir / pt_name
            if downloaded_weights.is_file() and not cached_weights.is_file():
                cached_weights.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(downloaded_weights, cached_weights)
            temporary_export_dir.cleanup()


def delete_model(model_name: str, imgsz: int | None = None) -> dict[str, Any]:
    """Delete an installed ONNX model file and its metadata.

    Safety checks:
    - Model must be in YOLO_MODELS whitelist
    - Model must be currently installed (ONNX file exists)
    - Model must NOT be the active model (in use by detector)

    Returns a dict with deletion status and updated model list.
    Raises HTTPException on validation errors.
    """
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'.")

    info = YOLO_MODELS[model_name]
    installed_meta = _read_installed_models()
    variants = _model_variants(model_name, installed_meta)
    if imgsz is not None:
        try:
            imgsz = int(imgsz)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='imgsz must be an integer.') from exc
        if not 32 <= imgsz <= 1280:
            raise HTTPException(status_code=400, detail='imgsz must be between 32 and 1280.')
    if imgsz is None:
        if len(variants) > 1:
            raise HTTPException(status_code=400, detail=f"Specify a resolution when deleting '{model_name}'.")
        if variants:
            imgsz = int(next(iter(variants.values())).get('imgsz', info.get('input_size', 640)))
        else:
            imgsz = int(info.get('input_size', 640))
    try:
        variant = variants.get(str(int(imgsz)), {})
        stored_path = variant.get('path') if isinstance(variant, dict) else None
        if isinstance(stored_path, str) and stored_path.strip():
            candidate = (BASE_DIR / _normalise_model_path(stored_path)).resolve()
            models_root = MODELS_DIR.resolve()
            if candidate.is_relative_to(models_root):
                onnx_path = candidate
            else:
                raise RuntimeError('Stored model variant path is outside the models directory.')
        else:
            onnx_path = _resolution_path(model_name, int(imgsz))
        legacy_path = _safe_within_models_dir(info['onnx'])
        if not onnx_path.exists() and int(imgsz) == int(info.get('input_size', 640)) and legacy_path.exists():
            onnx_path = legacy_path
    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Model path is invalid for {info['label']}.",
        ) from exc

    if not onnx_path.exists():
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' is not installed.")

    # Check if this model is currently active
    ai_settings = effective_ai_config()
    active_path = str(ai_settings.get('model_path') or '')
    rel_path = _relative_model_path(onnx_path)
    if _same_model_path(active_path, rel_path):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete '{model_name}' because it is the active model. Switch to another model first."
        )

    # Delete the source together with its INT8 cache under the same advisory
    # lock used by quantization. This prevents a concurrent worker from
    # publishing a cache for a model that is being deleted.
    #
    # Also remove the sibling INT8 quantization cache (``*.int8.onnx``) if one
    # was produced for this model, so precision=int8 deployments don't leave an
    # orphaned quantized copy behind after the source model is deleted.
    try:
        from app.quantization import (
            _int8_cache_metadata_path,
            _int8_process_lock,
            int8_cache_path,
        )
        int8_path = int8_cache_path(onnx_path)
        with _int8_process_lock(int8_path):
            # Remove derived artifacts first. If a later source unlink fails,
            # the model remains usable and no stale INT8 artifact can be
            # mistaken for a complete deployment.
            int8_path.unlink(missing_ok=True)
            _int8_cache_metadata_path(onnx_path).unlink(missing_ok=True)
            onnx_path.unlink(missing_ok=True)
    except Exception as exc:  # pragma: no cover - lock/filesystem failure is environment-specific
        # Never report success when the source could not be deleted. Leaving
        # the model intact is safer than a partial deletion with an orphaned
        # cache that can later be mistaken for a complete deployment.
        raise HTTPException(
            status_code=503,
            detail=f"Could not safely delete '{info['label']}'. Retry after the model is no longer in use.",
        ) from exc

    # Remove only this resolution's metadata. Other model resolutions remain
    # installed and available for switching.
    with _installed_models_lock:
        installed_meta = _read_installed_models()
        raw = installed_meta.get(model_name, {})
        stored = raw.get('variants') if isinstance(raw, dict) else None
        if isinstance(stored, dict):
            stored.pop(str(int(imgsz)), None)
            if stored:
                # Keep the flat summary pointed at a real remaining variant;
                # otherwise a later update with no explicit resolution would
                # target the variant just deleted.
                remaining_key, remaining = max(
                    stored.items(),
                    key=lambda item: int(item[1].get('imgsz', item[0])),
                )
                raw['variants'] = stored
                for field in ('version', 'installed_at', 'sha256', 'imgsz', 'path'):
                    if field in remaining:
                        raw[field] = remaining[field]
                installed_meta[model_name] = raw
            else:
                installed_meta.pop(model_name, None)
        elif model_name in installed_meta:
            installed_meta.pop(model_name, None)
        _write_installed_models(installed_meta)

    return {
        'ok': True,
        'message': f"Deleted {info['label']} model.",
        'deleted': model_name,
    }


_DEFAULT_MODEL = 'yolo11n'


def auto_download_default_model() -> None:
    """Download the default YOLO model on first startup if no model exists.

    On a clean install the ``models/`` directory has no ONNX file, so the
    detector reports ``MODEL MISSING`` and object detection is completely
    inert until the operator manually navigates to the Models tab and
    clicks Download.  This helper checks whether *any* model variant is
    already present; if none are, it exports the default (``yolo11n``)
    in a background thread so the server can finish starting while the
    ~5 MB export + Ultralytics weight download happens.

    Failures are logged at WARNING level and intentionally swallowed -
    a clean-install host that lacks network or is missing export
    dependencies still starts normally, just without detection.
    """
    # If any ONNX file already exists in the models directory, the operator
    # has already set up detection (or a prior startup downloaded it).
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        if any(MODELS_DIR.glob('*.onnx')):
            return
    except Exception:
        return
    info = YOLO_MODELS.get(_DEFAULT_MODEL)
    if info is None:
        return
    def _background_download() -> None:
        try:
            logger.info(
                'No ONNX model found - auto-downloading %s (first install).',
                info['label'],
            )
            _do_download_model(_DEFAULT_MODEL, switch_active=True, imgsz=info.get('input_size', 640))
            logger.info('Auto-download of %s completed successfully.', info['label'])
        except Exception as exc:
            logger.warning(
                'Auto-download of %s failed: %s. You can download the model manually from the Models tab.',
                info['label'], exc,
            )
    threading.Thread(target=_background_download, name='model-auto-download', daemon=True).start()


def _do_download_model(model_name: str, switch_active: bool = True, imgsz: int = 640) -> dict[str, Any]:
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'. Available: {', '.join(YOLO_MODELS)}")
    info = YOLO_MODELS[model_name]
    try:
        imgsz = int(imgsz)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='imgsz must be an integer.') from exc
    if not 32 <= imgsz <= 1280:
        raise HTTPException(status_code=400, detail='imgsz must be between 32 and 1280.')
    # Route the resolution-specific destination through the strict basename
    # canonicaliser. The legacy fixed filename remains untouched.
    try:
        destination = _resolution_path(model_name, imgsz)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Model export destination is invalid for {info['label']}.") from exc
    # Route the registry filename through the strict basename
    # canonicaliser so a malformed entry cannot become an export destination
    # outside the models directory. Rejections are mapped to a generic 502
    # below rather than exposing internal path details.
    # Honour the active ``precision`` / ``device`` so re-downloading a model
    # reuses the user's chosen export knobs. INT8 is intentionally excluded
    # from this path -- Ultralytics export can't satisfy it without
    # calibration data; INT8 is handled at detector load time by
    # ``app.quantization.quantize_int8``.
    ai_settings_for_export = effective_ai_config()
    precision = str(ai_settings_for_export.get('precision', 'fp32') or 'fp32').strip().lower()
    if precision not in ('fp32', 'fp16'):
        precision = 'fp32'
    device = str(ai_settings_for_export.get('device', 'auto') or 'auto').strip().lower()
    try:
        with _model_export_lock:
            exported_bytes = export_yolo_onnx(
                model_name,
                destination,
                imgsz=imgsz,
                precision=precision,
                device=device,
            )
    except ModuleNotFoundError as exc:
        # ``torch.onnx._internal.exporter._core`` imports ``onnxscript`` at
        # module-load time (and ``onnx`` itself does not pull it in), so a
        # missing install surfaces as ``ModuleNotFoundError: No module named
        # 'onnxscript'`` rather than a generic ``RuntimeError``. Surface the
        # exact missing module + a copy-pasteable install command so the
        # operator can fix it without reading tracebacks.
        #
        # Status code: 503 (Service Unavailable) is the right HTTP
        # semantic for "we are missing a dependency in our own venv";
        # 502 (Bad Gateway) implies an upstream/downstream issue that is
        # not the case here. Tests that already wired against 502 should
        # be updated to match the new status (round-9 + audit-finding-2
        # contract: missing-pkg-in-local-venv == 503).
        missing = getattr(exc, 'name', None) or 'the missing module'
        raise HTTPException(
            status_code=503,
            detail=(
                f"Failed to export {info['label']} ONNX model: missing Python "
                f"dependency `{missing}`. Install export dependencies with "
                f"`pip install onnx onnxscript ultralytics`, then retry. "
                f"Details: {exc}"
            ),
        ) from exc
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=502, detail=f"Failed to export {info['label']} ONNX model. Install export dependencies with `pip install ultralytics onnx`, then retry. Details: {exc}") from exc
    installed_version = _installed_package_version('ultralytics')
    with _installed_models_lock:
        installed_meta = _read_installed_models()
        existing_variants = _model_variants(model_name, installed_meta)
        variants = {key: dict(value) for key, value in existing_variants.items()}
        variants[str(imgsz)] = {
            'version': installed_version,
            'installed_at': utc_now(),
            'sha256': _sha256_file(destination),
            'imgsz': imgsz,
            'path': _relative_model_path(destination),
        }
        installed_meta[model_name] = {
            'version': installed_version,
            'installed_at': variants[str(imgsz)]['installed_at'],
            'sha256': variants[str(imgsz)]['sha256'],
            'imgsz': imgsz,
            'path': _relative_model_path(destination),
            'variants': variants,
        }
        _write_installed_models(installed_meta)
    ai_settings = effective_ai_config()
    rel_path = _relative_model_path(destination)
    is_active = _same_model_path(ai_settings.get('model_path'), rel_path)
    if switch_active or is_active:
        # Persist the resolution actually used for this export. The model
        # endpoint allows an operator-selected ``imgsz``; using the catalog's
        # nominal size here made dynamic-input models run at a different
        # resolution than the one just exported until the next reload.
        updated = validate_ai_settings({**ai_settings, 'model_path': rel_path, 'input_size': imgsz})
        _state.database.set_setting('ai', updated, utc_now())
        reloaded, error = _state.reload_detector(updated)
    else:
        updated = ai_settings
        reloaded = False
        error = None
    return {'ok': True, 'message': f"Exported {info['label']} ONNX to {_relative_model_path(destination)}.", 'model_path': rel_path, 'bytes': exported_bytes, 'reload_succeeded': reloaded, 'reload_error': error, 'status': detector_status(updated)}
