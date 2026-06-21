"""YOLO model download and metadata helpers extracted from ``app/main.py`` (Phase-I).

Cluster membership:
- ``_installed_models_lock`` — threading.Lock guarding installed-models JSON I/O
- ``PYPI_ULTRALYTICS_URL`` — PyPI endpoint for Ultralytics version checks
- ``BASE_DIR`` — project root (same as ``main.BASE_DIR``)
- ``_installed_models_path()`` — path to models/installed.json
- ``_read_installed_models()`` — read installed-models JSON
- ``_write_installed_models(data)`` — write installed-models JSON
- ``_sha256_file(path)`` — SHA-256 digest of a file
- ``_installed_package_version(package)`` — importlib.metadata version lookup
- ``_fetch_ultralytics_version()`` — fetch latest Ultralytics version from PyPI
- ``_parse_semver(v)`` — parse a semantic version string to a tuple
- ``_fetch_models_manifest()`` — build the remote YOLO export-version manifest
- ``export_yolo_onnx(model_name, destination)`` — run Ultralytics YOLO export
- ``_do_download_model(model_name, switch_active)`` — full export + persist + reload flow

Pool-C reach (resolved lazily via lazy imports inside function bodies):
- ``app.main.reload_detector`` (``_do_download_model``)
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.ai_settings import YOLO_MODELS, ai_status_payload, detector_status, validate_ai_settings
from app.auth import utc_now
from app.config_facades import effective_ai_config

logger = logging.getLogger('daygle.ai')

BASE_DIR = Path(__file__).resolve().parent.parent
PYPI_ULTRALYTICS_URL = 'https://pypi.org/pypi/ultralytics/json'
_installed_models_lock = threading.Lock()


def _installed_models_path() -> Path:
    return BASE_DIR / 'models' / 'installed.json'


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


def export_yolo_onnx(model_name: str, destination: Path) -> int:
    if model_name not in YOLO_MODELS:
        raise ValueError(f"Unknown model '{model_name}'. Available: {', '.join(YOLO_MODELS)}")
    info = YOLO_MODELS[model_name]
    pt_name = info['pt']
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-c', f"from ultralytics import YOLO\nmodel = YOLO('{pt_name}')\nmodel.export(format='onnx')\n"]
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


def _do_download_model(model_name: str, switch_active: bool = True) -> dict[str, Any]:
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'. Available: {', '.join(YOLO_MODELS)}")
    info = YOLO_MODELS[model_name]
    destination = BASE_DIR / 'models' / info['onnx']
    try:
        exported_bytes = export_yolo_onnx(model_name, destination)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=502, detail=f"Failed to export {info['label']} ONNX model. Install export dependencies with `pip install ultralytics onnx`, then retry. Details: {exc}") from exc
    installed_version = _installed_package_version('ultralytics')
    with _installed_models_lock:
        installed_meta = _read_installed_models()
        installed_meta[model_name] = {'version': installed_version, 'installed_at': utc_now(), 'sha256': _sha256_file(destination)}
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
