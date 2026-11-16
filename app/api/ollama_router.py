"""Ollama APIRouter: install Ollama and manage its models from the AI page.

Admin only. Every state-changing call is written to the audit log. The work
itself lives in app/ollama_manager.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

import app.state as _state
from app.auth_gates import require_admin
from app.deps import get_database
from app.ollama_manager import OllamaManager, get_ollama_manager
from app.request_helpers import admin_json_object, write_audit_log

router = APIRouter()


def _manager() -> OllamaManager:
    return get_ollama_manager(_state.config)


def _call(fn, *args: Any) -> dict[str, Any]:
    """Map the manager's exceptions onto HTTP errors."""
    try:
        return fn(*args)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get('/api/settings/ollama')
def get_ollama_status(request: Request):
    require_admin(request)
    return _manager().status()


@router.post('/api/settings/ollama/install')
def install_ollama(request: Request, db=Depends(get_database)):
    require_admin(request)
    result = _call(_manager().start_install)
    write_audit_log(request, db, 'install', 'settings.ollama', details={'version': result.get('pinned_version')})
    return result


@router.post('/api/settings/ollama/check-updates')
def check_ollama_updates(request: Request, payload: dict[str, Any] = Depends(admin_json_object)):
    """Look for a newer Ollama release and newer versions of installed models.

    ``{"force": true}`` (the Check for Updates button) skips the 6-hour cache
    used when the page opens."""
    force = payload.get('force', False)
    if not isinstance(force, bool):
        raise HTTPException(status_code=400, detail='force must be a boolean.')
    return _call(_manager().check_updates, force)


@router.post('/api/settings/ollama/update')
def update_ollama(request: Request, db=Depends(get_database)):
    """Update Ollama to the newest release this server's update check found.

    Same job as install; the version and checksum are never taken from the
    request."""
    require_admin(request)
    manager = _manager()
    before = manager.status()
    if not before.get('installed'):
        raise HTTPException(status_code=409, detail='Ollama is not installed.')
    if not before.get('update_available'):
        raise HTTPException(status_code=409, detail='Ollama is already up to date.')
    result = _call(manager.start_install)
    write_audit_log(request, db, 'update', 'settings.ollama', details={'from': before.get('version'), 'to': result.get('install_version')})
    return result


@router.post('/api/settings/ollama/service')
def control_ollama_service(request: Request, db=Depends(get_database), payload: dict[str, Any] = Depends(admin_json_object)):
    action = payload.get('action')
    result = _call(_manager().service_action, action)
    write_audit_log(request, db, 'update', 'settings.ollama.service', details={'action': action})
    return result


@router.post('/api/settings/ollama/models/pull')
def pull_ollama_model(request: Request, db=Depends(get_database), payload: dict[str, Any] = Depends(admin_json_object)):
    result = _call(_manager().start_pull, payload.get('model'))
    write_audit_log(request, db, 'download', 'settings.ollama.model', resource_id=result.get('pull', {}).get('model'))
    return result


@router.post('/api/settings/ollama/models/update')
def update_ollama_models(request: Request, db=Depends(get_database), payload: dict[str, Any] = Depends(admin_json_object)):
    """``{"models": [...]}`` re-downloads those models; ``{"all": true}``
    every model the last update check found out of date."""
    models = None if payload.get('all') is True else payload.get('models', [])
    result = _call(_manager().start_update_models, models)
    write_audit_log(request, db, 'update', 'settings.ollama.model', details={'models': result.get('pull', {}).get('queue')})
    return result


@router.post('/api/settings/ollama/models/delete')
def delete_ollama_model(request: Request, db=Depends(get_database), payload: dict[str, Any] = Depends(admin_json_object)):
    model = payload.get('model')
    result = _call(_manager().delete_model, model)
    write_audit_log(request, db, 'delete', 'settings.ollama.model', resource_id=str(model).strip())
    return result
