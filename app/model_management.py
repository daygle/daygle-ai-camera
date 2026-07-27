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
import subprocess
import sys
import threading
import urllib.error
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
    INT8 is a runtime concern handled by ``app.quantization.quantize_int8_dynamic``.
    """
    parts = ["format='onnx'", f"end2end={'True' if nms_free else 'False'}", 'opset=13']
    if precision == 'fp16' and device.lower() == 'cuda':
        from app.quantization import onnxruntime_gpu_available
        if onnxruntime_gpu_available():
            parts.append('half=True')
        else:
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
from app.ai_settings import YOLO_MODELS, ai_status_payload, detector_status, validate_ai_settings
from app.auth import utc_now
from app.config_facades import effective_ai_config

logger = logging.getLogger('daygle.ai')

BASE_DIR = Path(__file__).resolve().parent.parent
PYPI_ULTRALYTICS_URL = 'https://pypi.org/pypi/ultralytics/json'
_installed_models_lock = threading.Lock()


MODELS_DIR: Path = BASE_DIR / 'models'


def _safe_within_models_dir(filename: str) -> Path:
    """Resolve ``filename`` against ``MODELS_DIR`` and refuse traversal.

    M4 (round-7) defence-in-depth for any code path that takes a
    user/operator-supplied filename and joins it onto the models directory:

    - Strips any directory components (``pathlib.PurePosixPath(...).name``).
    - Rejects empty / dot-only / leading-dot names so a misconfigured
      ``YOLO_MODELS[model_name]['onnx'] = ''`` cannot resolve to MODELS_DIR
      itself and ``.bashrc`` / ``..`` cannot pass through.
    - Confirms the resolved path stays inside ``MODELS_DIR``.

    Raises ``RuntimeError`` on rejection so the existing
    ``_do_download_model`` HTTP-422 wrapper surfaces a clean 502 to the
    caller without leaking ``str(filename)`` contents into the response
    body. ``RuntimeError`` is also caught / mapped by ``export_yolo_onnx``
    callers, which already return ``HTTPException(502, …)`` on the same
    shape, so the new check composes cleanly.

    The check is deliberately tighter than ``PurePath.is_relative_to``:
    a filename like ``yolov8n.pt`` resolves identically to its origin
    so ``is_relative_to(MODELS_DIR)`` returns True; a filename like
    ``../etc/passwd`` becomes ``/etc/passwd`` after stripping and
    ``is_relative_to(MODELS_DIR)`` returns False. Both branches covered.
    """
    if not isinstance(filename, str) or not filename.strip():
        raise RuntimeError('Model filename must be a non-empty string.')
    stripped = filename.strip()
    name = Path(stripped).name  # strips any path separator + parent refs
    if not name or name in {'.', '..'} or name.startswith('.'):
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
    # concern handled by ``app.quantization.quantize_int8_dynamic``.
    export_kwargs = _export_kwargs(nms_free, precision=precision, device=device)
    export_script = (
        "import sys\n"
        "from ultralytics import YOLO\n"
        f"YOLO(sys.argv[1]).export({export_kwargs}, imgsz=int(sys.argv[2]))\n"
    )
    command = [sys.executable, '-c', export_script, pt_name, str(imgsz)]
    result = subprocess.run(command, cwd=destination.parent, capture_output=True, text=True, timeout=600, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(details or f'Ultralytics export exited with status {result.returncode}.')
    exported = destination.parent / info['onnx']
    if exported != destination and exported.exists():
        exported.replace(destination)
    if not destination.exists():
        details = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(details or f'Ultralytics export did not create {destination.name}.')
    if destination.stat().st_size <= 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError('Exported model file is empty.')
    return destination.stat().st_size


def delete_model(model_name: str) -> dict[str, Any]:
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
    onnx_path = _safe_within_models_dir(info['onnx'])

    if not onnx_path.exists():
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' is not installed.")

    # Check if this model is currently active
    ai_settings = effective_ai_config()
    active_path = str(ai_settings.get('model_path') or '')
    rel_path = str(onnx_path.relative_to(BASE_DIR))
    if active_path == rel_path:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete '{model_name}' because it is the active model. Switch to another model first."
        )

    # Delete the ONNX file
    onnx_path.unlink(missing_ok=True)

    # Remove from installed.json metadata
    with _installed_models_lock:
        installed_meta = _read_installed_models()
        if model_name in installed_meta:
            del installed_meta[model_name]
            _write_installed_models(installed_meta)

    return {
        'ok': True,
        'message': f"Deleted {info['label']} model.",
        'deleted': model_name,
    }


def _do_download_model(model_name: str, switch_active: bool = True, imgsz: int = 640) -> dict[str, Any]:
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'. Available: {', '.join(YOLO_MODELS)}")
    info = YOLO_MODELS[model_name]
    # M4: route operator-supplied ``info['onnx']`` through the path
    # canonicaliser so a misconfigured entry like
    # ``YOLO_MODELS[model_name]['onnx'] = '../etc/passwd'`` cannot be
    # used as the export destination. ``_safe_within_models_dir`` raises
    # ``RuntimeError`` which is mapped to ``HTTPException(502, …)``
    # below by the same try/except already wrapping ``export_yolo_onnx``,
    # so no new error-path code is needed.
    destination = _safe_within_models_dir(info['onnx'])
    # Honour the active ``precision`` / ``device`` so re-downloading a model
    # reuses the user's chosen export knobs. INT8 is intentionally excluded
    # from this path -- Ultralytics export can't satisfy it without
    # calibration data; INT8 is handled at detector load time by
    # ``app.quantization.quantize_int8_dynamic``.
    ai_settings_for_export = effective_ai_config()
    precision = str(ai_settings_for_export.get('precision', 'fp32') or 'fp32').strip().lower()
    if precision not in ('fp32', 'fp16'):
        precision = 'fp32'
    device = str(ai_settings_for_export.get('device', 'auto') or 'auto').strip().lower()
    try:
        exported_bytes = export_yolo_onnx(model_name, destination, imgsz=imgsz, precision=precision, device=device)
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
        installed_meta[model_name] = {'version': installed_version, 'installed_at': utc_now(), 'sha256': _sha256_file(destination), 'imgsz': imgsz}
        _write_installed_models(installed_meta)
    ai_settings = effective_ai_config()
    rel_path = str(destination.relative_to(BASE_DIR))
    is_active = ai_settings.get('model_path') == rel_path
    if switch_active or is_active:
        updated = validate_ai_settings({**ai_settings, 'model_path': rel_path})
        _state.database.set_setting('ai', updated, utc_now())
        reloaded, error = _state.reload_detector(updated)
    else:
        updated = ai_settings
        reloaded = False
        error = None
    return {'ok': True, 'message': f"Exported {info['label']} ONNX to {destination.relative_to(BASE_DIR)}.", 'model_path': rel_path, 'bytes': exported_bytes, 'reload_succeeded': reloaded, 'reload_error': error, 'status': detector_status(updated)}
