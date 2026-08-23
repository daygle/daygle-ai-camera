"""Settings / AI APIRouter.
"""

from __future__ import annotations

import json
import logging
import urllib.error

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.ai_settings import YOLO_MODELS, detector_status, validate_ai_settings
from app.auth import utc_now
from app.auth_gates import require_admin
from app.config_facades import effective_ai_config
from app.deps import get_database, get_detector, get_reload_detector
from app.detector import DetectorUnavailableError
from app.model_management import (
    BASE_DIR,
    _do_download_model,
    _fetch_models_manifest,
    _model_variants,
    _normalise_model_path,
    _parse_semver,
    _same_model_path,
    _read_installed_models,
    delete_model,
)
from app.request_helpers import write_audit_log
from app.media_utils import ONE_PIXEL_PNG

logger = logging.getLogger('daygle.ai')

router = APIRouter()


@router.get('/api/settings/ai')
def get_ai_settings(request: Request):
    require_admin(request)
    return detector_status(effective_ai_config())


@router.put('/api/settings/ai')
async def update_ai_settings(
    request: Request,
    db=Depends(get_database),
    reload_detector=Depends(get_reload_detector),
):
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
def reload_ai_detector(request: Request, reload_detector=Depends(get_reload_detector)):
    require_admin(request)
    ai_settings = effective_ai_config()
    reloaded, error = reload_detector(ai_settings)
    response = detector_status(ai_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
    if not reloaded:
        return JSONResponse(response, status_code=400)
    return response


@router.post('/api/settings/ai/check-model')
def check_ai_model(request: Request):
    require_admin(request)
    return detector_status(effective_ai_config())


@router.get('/api/settings/ai/models')
def list_ai_models():
    """List one selectable entry per installed model resolution.

    The catalog still returns an available base entry when no variant exists,
    while installed variants are flattened into separate rows. This keeps the
    existing frontend/API shape useful and makes ``path`` the stable identity
    used when switching the active detector.
    """
    models_dir = BASE_DIR / 'models'
    ai_settings = effective_ai_config()
    active_path = str(ai_settings.get('model_path') or '').replace('\\', '/')
    face_active_path = str(ai_settings.get('face_model_path') or '').replace('\\', '/')
    installed_meta = _read_installed_models()

    def _model_family(info: dict) -> str:
        """Face-family catalog entries ship their own ``face.names`` labels."""
        return 'face' if str(info.get('labels') or '').endswith('face.names') else 'object'

    result = []
    for model_id, info in YOLO_MODELS.items():
        family = _model_family(info)
        variants = _model_variants(model_id, installed_meta)
        if not variants:
            result.append({
                'id': model_id,
                'variant_id': model_id,
                'label': info['label'],
                'description': info['description'],
                'approx_mb': info['approx_mb'],
                'input_size': info.get('input_size'),
                'nms_free': info.get('nms_free', False),
                'path': (models_dir / info['onnx']).relative_to(BASE_DIR).as_posix(),
                'installed': False,
                'active': False,
                'size_bytes': None,
                'installed_version': None,
                'exported_imgsz': None,
            })
            continue
        # Keep a download card alongside installed variants so another
        # resolution can always be added without replacing an existing one.
        result.append({
            'id': model_id,
            'variant_id': f'{model_id}-new',
            'label': info['label'],
            'description': info['description'],
            'approx_mb': info['approx_mb'],
            'input_size': info.get('input_size'),
            'nms_free': info.get('nms_free', False),
            'path': (models_dir / info['onnx']).relative_to(BASE_DIR).as_posix(),
            'installed': False,
            'active': False,
            'family': family,
            'size_bytes': None,
            'installed_version': None,
            'exported_imgsz': None,
        })
        for variant in sorted(variants.values(), key=lambda item: int(item.get('imgsz', info.get('input_size', 640)))):
            path = str(variant['path'])
            absolute = BASE_DIR / path
            result.append({
                'id': model_id,
                'variant_id': f"{model_id}-{int(variant['imgsz'])}",
                'label': f"{info['label']} · {int(variant['imgsz'])}×{int(variant['imgsz'])}",
                'description': info['description'],
                'approx_mb': info['approx_mb'],
                'input_size': info.get('input_size'),
                'nms_free': info.get('nms_free', False),
                'path': path,
                'installed': absolute.is_file(),
                # Face models are "in use" when they are the configured
                # secondary face model -- never by being the active PRIMARY,
                # which is always an object model now that the face pass is
                # separate.
                'active': _same_model_path(face_active_path if family == 'face' else active_path, path),
                'family': family,
                'size_bytes': absolute.stat().st_size if absolute.is_file() else None,
                'installed_version': variant.get('version'),
                'exported_imgsz': int(variant['imgsz']),
            })
    return result


@router.post('/api/settings/ai/download-model')
async def download_ai_model(request: Request, db=Depends(get_database)):
    require_admin(request)
    body = await request.json()
    model_name = str(body.get('model') or '').strip().lower()
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'.")
    info = YOLO_MODELS[model_name]
    try:
        imgsz = int(body.get('imgsz') or info.get('input_size', 640))
    except (TypeError, ValueError):
        imgsz = info.get('input_size', 640)
    # Clamp to reasonable range: Ultralytics supports 32-4096, we cap at 1280
    imgsz = max(32, min(1280, imgsz))
    # Round to nearest multiple of 32 for optimal Ultralytics export
    imgsz = ((imgsz + 16) // 32) * 32
    # Audit-log gate (audit-trail finding): admin downloads of
    # bin/exported artefacts must leave a trail, mirroring
    # ``update_ai_settings``. Without this row, an admin replacement
    # of the active ONNX model has no audit entry pointing back to
    # who kicked it off.
    # Face-family downloads configure the SECONDARY face pass (see
    # ``_do_download_model``) instead of replacing the active object model.
    is_face_model = str(info.get('labels') or '').endswith('face.names')
    write_audit_log(request, db, 'download', 'settings.ai.model',
                    details={'model_id': model_name, 'switch_active': not is_face_model, 'configure_face': is_face_model, 'imgsz': imgsz})
    # Round-6 / N2 removal (drop N2 entirely (B3)): the previous SHA-256
    # pin-on-upstream gate has been removed because ``_do_download_model``
    # produces a locally-exported ONNX binary via the Ultralytics SDK
    # rather than fetching a canonical .pt file. There is no upstream
    # source-of-truth hash to pin against (Ultralytics does not publish
    # SHA-256 digests for ``yolov8{}.pt``), so any "verify-against-pinned"
    # attempt is unsatisfiable for these specific artefacts. Trust
    # transfers to the Ultralytics SDK + pip TLS for delivery integrity,
    # and the existing per-installed-model SHA-256 metadata record on
    # ``_do_download_model`` continues to capture byte-fingerprints for
    # local auditing. The whitelist above (``YOLO_MODELS`` membership
    # check) remains the active gate against off-list blob fetches.
    return await run_in_threadpool(_do_download_model, model_name, not is_face_model, imgsz, is_face_model)


@router.get('/api/settings/ai/check-model-updates')
def check_model_updates(request: Request):
    require_admin(request)
    installed_meta = _read_installed_models()
    models_dir = BASE_DIR / 'models'
    try:
        manifest = _fetch_models_manifest()
    except urllib.error.HTTPError as exc:
        return {'error': f'Manifest fetch error {exc.code}: {exc.reason}', 'models': [], 'any_updates': False}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        # R9 H4: narrow + log + sanitize. The previous broad except
        # collapsed every network / parse / config fault to one opaque
        # ``str(exc)`` blob; the new shape keeps the full message on the
        # operator-side ``app.log`` but only exposes the exception type
        # name to the admin client.
        logger.warning('check_model_updates manifest fetch failed (%s): %s', type(exc).__name__, exc)
        return {
            'error': f'Could not fetch model-update manifest ({type(exc).__name__}).',
            'models': [],
            'any_updates': False,
        }
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
async def update_ai_model(request: Request, db=Depends(get_database)):
    require_admin(request)
    body = await request.json()
    model_name = str(body.get('model') or '').strip().lower()
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'.")
    # Read stored export resolution from installed.json so the model
    # is re-exported at the same resolution it was originally downloaded at.
    # This prevents silently downgrading from e.g. 1024 to 640 on update.
    info = YOLO_MODELS[model_name]
    installed_meta = _read_installed_models()
    stored_imgsz = installed_meta.get(model_name, {}).get('imgsz', info.get('input_size', 640))
    try:
        stored_imgsz = int(stored_imgsz)
    except (TypeError, ValueError):
        stored_imgsz = int(info.get('input_size', 640))
    requested_imgsz = body.get('imgsz')
    if requested_imgsz in (None, ''):
        # With multiple installed resolutions, update the currently active
        # variant by default. This avoids silently re-exporting a different
        # size merely because it happens to be the metadata summary variant.
        # Both the primary object model AND the secondary face model count as
        # "active" so updating an in-use face card targets its real resolution.
        ai_settings = effective_ai_config()
        active_paths = {
            _normalise_model_path(ai_settings.get('model_path')),
            _normalise_model_path(ai_settings.get('face_model_path')),
        }
        active_variant = next(
            (
                variant for variant in _model_variants(model_name, installed_meta).values()
                if _normalise_model_path(variant.get('path')) in active_paths
            ),
            None,
        )
        requested_imgsz = (active_variant or {}).get('imgsz', stored_imgsz)
    try:
        imgsz = int(requested_imgsz)
    except (TypeError, ValueError):
        imgsz = stored_imgsz
    imgsz = max(32, min(1280, imgsz))
    # Round to the nearest multiple of 32 (Ultralytics requirement) so the
    # value we store in installed.json matches the model the export actually
    # produces, mirroring ``download_ai_model``.
    imgsz = ((imgsz + 16) // 32) * 32
    # Audit-log gate (audit-trail finding): same shape as
    # ``download_ai_model`` so the admin actions for the matching
    # model endpoint is traceable.
    write_audit_log(request, db, 'update', 'settings.ai.model',
                    details={'model_id': model_name, 'switch_active': False, 'imgsz': imgsz})
    return await run_in_threadpool(_do_download_model, model_name, False, imgsz)


@router.delete('/api/settings/ai/models/{model_id}')
def delete_ai_model(model_id: str, request: Request, db=Depends(get_database), imgsz: int | None = Query(default=None)):
    require_admin(request)
    model_name = model_id.strip().lower()
    result = delete_model(model_name, imgsz=imgsz)
    write_audit_log(request, db, 'delete', 'settings.ai.model',
                    details={'model_id': model_name, 'imgsz': imgsz})
    result['models'] = list_ai_models()
    return result


@router.post('/api/settings/ai/test-detector')
def test_ai_detector(request: Request, detector=Depends(get_detector)):
    require_admin(request)
    ai_settings = effective_ai_config()
    ai_state = detector_status(ai_settings)
    ai_error: str | None = None
    detections: list = []
    if not hasattr(detector, 'detect_image'):
        ai_error = 'Configured detector cannot run image inference.'
    else:
        try:
            detections = detector.detect_image(ONE_PIXEL_PNG)
        except DetectorUnavailableError:
            ai_error = 'Detector unavailable.'
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='Invalid detector input.') from exc
    return {
        'ok': ai_error is None,
        'backend_used': ai_state['configured_backend'],
        'detections': detections,
        'status': ai_state,
        'ai_error': ai_error,
    }
