"""Settings / AI APIRouter.

Extracted from ``app/main.py`` lines 5148-5400 (Phase 2 of the hybrid-pattern
router split). Same template as ``app/api/sound_router.py``: ``import app.main
as main`` at module level, every global / helper / test-referenced symbol read
through ``main.<name>`` *inside* handler bodies.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.reload_detector`` — hot module-level ``global detector`` swap,
  referenced by both this router and ``process_live_stream_alerts``.
- ``main.export_yolo_onnx`` — invoked from ``tests/test_api.py`` as
  ``main.export_yolo_onnx(...)`` at L383. Per the rule in
  ``app/api/__init__.py``, anything tests reference as ``main.<attr>`` must
  stay defined on ``app.main``.
- ``main._do_download_model`` — used by ``download_ai_model``,
  ``download_yolov8n_model``, and ``update_ai_model``.
- ``main.ai_status_payload``, ``main.detector_status``,
  ``main.effective_ai_config``, ``main.validate_ai_settings``, ``main.utc_now``,
  ``main.write_audit_log``, ``main.require_admin``,
  ``main._read_installed_models``, ``main._fetch_models_manifest``,
  ``main._parse_semver``, ``main.YOLO_MODELS``, ``main.BASE_DIR``,
  ``main.ONE_PIXEL_PNG``, ``main.detector``, ``main.database`` — all read-only
  globals accessed inside handler bodies.

See ``app/api/__init__.py`` for the full hybrid-pattern rules and the
invariant test (``tests/test_api_router_split_invariants.py``) that
mechanically enforces them, including the routes-coverage assertion that would
have caught the e365ec5 over-deletion regression.
"""

from __future__ import annotations

import urllib.error

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.detector import DetectorUnavailableError

import app.main as main

router = APIRouter()


@router.get('/api/settings/ai')
def get_ai_settings():
    return main.detector_status(main.effective_ai_config())


@router.put('/api/settings/ai')
async def update_ai_settings(request: Request):
    main.require_admin(request)
    payload = await request.json()
    new_settings = main.validate_ai_settings(payload)
    main.database.set_setting('ai', new_settings, main.utc_now())
    reloaded, error = main.reload_detector(new_settings)
    response = main.detector_status(new_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
    main.write_audit_log(request, 'update', 'settings.ai', details={
        'model_path': new_settings.get('model_path'),
        'backend': new_settings.get('backend'),
    })
    return response


@router.post('/api/settings/ai/reload')
def reload_ai_detector():
    ai_settings = main.effective_ai_config()
    reloaded, error = main.reload_detector(ai_settings)
    response = main.detector_status(ai_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
    if not reloaded:
        return JSONResponse(response, status_code=400)
    return response


@router.post('/api/settings/ai/check-model')
def check_ai_model():
    return main.ai_status_payload(main.effective_ai_config())


@router.get('/api/settings/ai/models')
def list_ai_models():
    models_dir = main.BASE_DIR / 'models'
    active_path = str(main.effective_ai_config().get('model_path') or '')
    installed_meta = main._read_installed_models()
    result = []
    for model_id, info in main.YOLO_MODELS.items():
        onnx_path = models_dir / info['onnx']
        rel_path = str((models_dir / info['onnx']).relative_to(main.BASE_DIR))
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
    return main._do_download_model(str(body.get('model') or '').strip().lower())


@router.post('/api/settings/ai/download-yolov8n')
def download_yolov8n_model():
    return main._do_download_model('yolov8n')


@router.get('/api/settings/ai/check-model-updates')
def check_model_updates(request: Request):
    main.require_admin(request)
    installed_meta = main._read_installed_models()
    models_dir = main.BASE_DIR / 'models'
    try:
        manifest = main._fetch_models_manifest()
    except urllib.error.HTTPError as exc:
        return {'error': f'Manifest fetch error {exc.code}: {exc.reason}', 'models': [], 'any_updates': False}
    except Exception as exc:
        return {'error': str(exc), 'models': [], 'any_updates': False}
    manifest_models = manifest.get('models', {})
    result = []
    for model_id, info in main.YOLO_MODELS.items():
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
                or main._parse_semver(remote_version) > main._parse_semver(installed_version)
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
    main.require_admin(request)
    body = await request.json()
    model_name = str(body.get('model') or '').strip().lower()
    if model_name not in main.YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'.")
    return main._do_download_model(model_name, switch_active=False)


@router.post('/api/settings/ai/test-detector')
def test_ai_detector():
    ai_settings = main.effective_ai_config()
    ai_state = main.ai_status_payload(ai_settings)
    ai_error: str | None = None
    detections: list = []
    if not hasattr(main.detector, 'detect_image'):
        ai_error = 'Configured detector cannot run image inference.'
    else:
        try:
            detections = main.detector.detect_image(main.ONE_PIXEL_PNG)
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
