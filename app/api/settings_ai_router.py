"""Settings / AI APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

import urllib.error

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.ai_settings import YOLO_MODELS, ai_status_payload, detector_status, validate_ai_settings
from app.auth import utc_now
from app.auth_gates import require_admin
from app.config_facades import effective_ai_config
from app.deps import get_database
from app.detector import DetectorUnavailableError
from app.model_management import (
    BASE_DIR,
    _do_download_model,
    _fetch_models_manifest,
    _parse_semver,
    _read_installed_models,
)
from app.request_helpers import write_audit_log
from app.main import (
    ONE_PIXEL_PNG,
    detector,
    reload_detector,
)

router = APIRouter()


@router.get('/api/settings/ai')
def get_ai_settings():
    return detector_status(effective_ai_config())


@router.put('/api/settings/ai')
async def update_ai_settings(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await request.json()
    new_settings = validate_ai_settings(payload)
    db.set_setting('ai', new_settings, utc_now())
    reloaded, error = reload_detector(new_settings)
    response = detector_status(new_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
    write_audit_log(request, db, 'update', 'settings.ai', details={
        'model_path': new_settings.get('model_path'),
        'backend': new_settings.get('backend'),
    })
    return response


@router.post('/api/settings/ai/reload')
def reload_ai_detector():
    ai_settings = effective_ai_config()
    reloaded, error = reload_detector(ai_settings)
    response = detector_status(ai_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
    if not reloaded:
        return JSONResponse(response, status_code=400)
    return response


@router.post('/api/settings/ai/check-model')
def check_ai_model():
    return ai_status_payload(effective_ai_config())


@router.get('/api/settings/ai/models')
def list_ai_models():
    models_dir = BASE_DIR / 'models'
    active_path = str(effective_ai_config().get('model_path') or '')
    installed_meta = _read_installed_models()
    result = []
    for model_id, info in YOLO_MODELS.items():
        onnx_path = models_dir / info['onnx']
        rel_path = str((models_dir / info['onnx']).relative_to(BASE_DIR))
        installed = onnx_path.exists()
        meta = installed_meta.get(model_id, {})
        result.append({
            'id': model_id,
            'label': info['label'],
            'description': info['description'],
            'approx_mb': info['approx_mb'],
            'path': rel_path,
            'installed': installed,
            'active': active_path == rel_path,
            'size_bytes': onnx_path.stat().st_size if installed else None,
            'installed_version': meta.get('version') if installed else None,
        })
    return result


@router.post('/api/settings/ai/download-model')
async def download_ai_model(request: Request):
    body = await request.json()
    return _do_download_model(str(body.get('model') or '').strip().lower())


@router.post('/api/settings/ai/download-yolov8n')
def download_yolov8n_model():
    return _do_download_model('yolov8n')


@router.get('/api/settings/ai/check-model-updates')
def check_model_updates(request: Request):
    require_admin(request)
    installed_meta = _read_installed_models()
    models_dir = BASE_DIR / 'models'
    try:
        manifest = _fetch_models_manifest()
    except urllib.error.HTTPError as exc:
        return {'error': f'Manifest fetch error {exc.code}: {exc.reason}', 'models': [], 'any_updates': False}
    except Exception as exc:
        return {'error': str(exc), 'models': [], 'any_updates': False}
    manifest_models = manifest.get('models', {})
    result = []
    for model_id, info in YOLO_MODELS.items():
        onnx_path = models_dir / info['onnx']
        in_meta = model_id in installed_meta
        if not in_meta and not onnx_path.exists():
            continue
        meta = installed_meta.get(model_id, {})
        installed_version = meta.get('version') or 'unknown'
        remote_version = manifest_models.get(model_id, {}).get('version')
        update_available = bool(
            remote_version
            and (
                installed_version == 'unknown'
                or _parse_semver(remote_version) > _parse_semver(installed_version)
            )
        )
        result.append({
            'id': model_id,
            'installed_version': installed_version,
            'latest_version': remote_version,
            'update_available': update_available,
        })
    return {
        'manifest_updated_at': manifest.get('updated_at'),
        'version_source': manifest.get('source'),
        'models': result,
        'any_updates': any(m['update_available'] for m in result),
    }


@router.post('/api/settings/ai/update-model')
async def update_ai_model(request: Request):
    require_admin(request)
    body = await request.json()
    model_name = str(body.get('model') or '').strip().lower()
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'.")
    return _do_download_model(model_name, switch_active=False)


@router.post('/api/settings/ai/test-detector')
def test_ai_detector():
    ai_settings = effective_ai_config()
    ai_state = ai_status_payload(ai_settings)
    ai_error: str | None = None
    detections: list = []
    if not hasattr(detector, 'detect_image'):
        ai_error = 'Configured detector cannot run image inference.'
    else:
        try:
            detections = detector.detect_image(ONE_PIXEL_PNG)
        except DetectorUnavailableError as exc:
            ai_error = str(exc) or ai_state.get('last_detector_error') or 'Detector unavailable.'
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        'ok': ai_error is None,
        'backend_used': ai_state['configured_backend'],
        'detections': detections,
        'status': ai_state,
        'ai_error': ai_error,
    }
