"""Admin APIRouter.

Extracted from ``app/main.py`` (Phase 11 of the hybrid-pattern router split).
Same template as ``app/api/status_router.py`` (Phase 8) and
``app/api/live_router.py`` (Phase 10): ``import app.main as main`` at module
level, every global / helper / test-referenced symbol read through
``main.<name>`` *inside* handler bodies.

This file deliberately bundles FIVE single-handler admin-flavored routes into
a single router rather than splitting them one-per-file:

- ``GET   /api/audit``                    -- ``list_audit_log``
- ``GET   /api/auth/me``                  -- ``me``
- ``GET   /api/config``                   -- ``runtime_config``
- ``POST  /api/detect/frame``             -- ``detect_frame`` (async)
- ``DELETE /api/system/runtime-data``     -- ``delete_runtime_data``

The motivation: every other Phase-N has been a contiguous single-prefix
extraction. The remaining routes in ``main.py`` are scattered single
handlers -- ``/api/audit`` (L3370), ``/api/auth/me`` (L2772),
``/api/config`` (L2810), ``/api/detect/frame`` (L2777), and
``/api/system/runtime-data`` (L2857) -- none of which are pairwise
contiguous with each other. Combining them into a single ``admin_router``
file delivers the cumulative pattern benefit of Phase-7-style cleanup
without inflating ``main.py`` with five separate ``include_router`` lines
for one-handler apis. The combined test_api.py literal-path coverage for
these 5 routes (~14 refs) is the highest of any remaining single-router
candidate, which is the safer extraction.

BODY-REWRITE NOTE
Each handler in this file originally referenced module-level state in
main.py via bare names (e.g. ``require_admin``, ``database``,
``auth_enabled``, ``config``, ``effective_ai_config``, ``ai_status_payload``,
``detector``, ``active_rtsp_recordings_lock``, ``_read_uploaded_image``,
``DetectorUnavailableError``, ``compute_minimum_rule_confidence``,
``active_rtsp_recordings``, ``clear_runtime_media_directory``,
``write_audit_log``, ``delete_recording_files``). After extraction to this
router, those bare names resolve to ZERO attributes in our namespace --
handlers would NameError at request time. Per hybrid-pattern uniformity
(rule 5 of ``app/api/__init__.py``), each bare call is rewritten as
``main.<bare>``. Pure syntactic change with zero behavioral impact.

ASYNC HANDLER NOTE
``detect_frame`` is the only ``async def`` in this router (matches the
original main.py decision). Blocking detector calls inside
``async def detect_frame`` are wrapped in
``asyncio.get_event_loop().run_in_executor(None, _run_detection)`` to keep
the FastAPI event loop responsive (the original main.py pattern;
preserved verbatim). The FastAPI builtin ``HTTPException`` stays at
top-level import.

LOCK HANDLER NOTE
``delete_runtime_data`` clears ``active_rtsp_recordings`` under the
``active_rtsp_recordings_lock``. The router reaches the lock + dict via
``main.active_rtsp_recordings_lock`` and ``main.active_rtsp_recordings``
identically to how ``main.py`` resolved them at module-level scope.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.require_admin``, ``main.require_session`` - auth + admin gates
- ``main.database`` - EventDatabase instance with all the .delete_all_*(),
  .list_audit_logs(), .count_audit_logs(), .add_event(), etc. methods
- ``main.write_audit_log`` - audit-log emitter used by both ``list_audit_log``
  (no - the list handler is read-only, no audit write) and ``delete_runtime_data``
- ``main.config`` - the loaded YAML dict
- ``main.auth_enabled`` - bool flag
- ``main.effective_ai_config``, ``main.effective_cameras_config``,
  ``main.effective_live_config``, ``main.effective_recording_config``,
  ``main.effective_storage_config``, ``main.effective_auth_config`` -
  reading setters built at module-load
- ``main.get_camera_config`` - per-camera setting lookup
- ``main.ai_status_payload`` - the AI status dict shape referenced by
  ``runtime_config`` AND returned by ``/api/status/ai``
- ``main.detector`` - the OnnxYoloDetector (or similar) instance with
  ``detect_image(image_bytes, confidence=...)``
- ``main._read_uploaded_image`` - multipart-parse helper for the
  detect_frame body
- ``main.compute_minimum_rule_confidence`` - per-rule floor used before
  running the detector
- ``main.DetectorUnavailableError`` - raised by ``detector.detect_image``
  on missing-model + missing-runtime, caught and converted to a 200 + empty
  detection list
- ``main.delete_recording_files``, ``main.clear_runtime_media_directory``
  - filesystem-side cleanup helpers
- ``main.active_rtsp_recordings_lock``, ``main.active_rtsp_recordings`` -
  the live RTSP capture registry the DELETE handler clears

FastAPI builtins stay at top-level imports: ``APIRouter``, ``Query``,
``HTTPException``, ``Request``.

Tests go through ``LocalClient.request`` rather than calling
``main.<attr>`` directly for these endpoints, so no back-compat alias on
``app.main`` is needed. The Phase 7.1 invariant
``tests/test_api_router_split_invariants.py::test_app_api_imports_in_main_are_consumed``
will catch any orphan-import regression if a future refactor drops the
``from app.api.admin_router import router as admin_router`` rebind line
in ``app/main.py``.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request

import app.main as main

router = APIRouter()


@router.get('/api/audit')
def list_audit_log(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: str | None = None,
    username: str | None = None,
    resource: str | None = None,
):
    main.require_admin(request)
    entries = main.database.list_audit_logs(
        limit=limit,
        offset=offset,
        action=action or None,
        username=username or None,
        resource=resource or None,
    )
    total = main.database.count_audit_logs(
        action=action or None,
        username=username or None,
        resource=resource or None,
    )
    return {'entries': entries, 'total': total, 'limit': limit, 'offset': offset}


@router.get('/api/auth/me')
def me(request: Request):
    session = main.require_session(request)
    return {'user': session['user'], 'csrf_token': session['csrf_token'], 'expires_at': session['expires_at']}


@router.get('/api/config')
def runtime_config():
    ai_state = main.ai_status_payload()
    ai_cfg = main.effective_ai_config()
    return {
        'server': {'host': main.config.get('server', {}).get('host'), 'port': main.config.get('server', {}).get('port')},
        'camera': main.get_camera_config(None),
        'cameras': main.effective_cameras_config(),
        'ai': {
            'enabled': ai_cfg.get('enabled'),
            'backend': ai_cfg.get('backend'),
            'confidence': ai_cfg.get('confidence'),
            'iou_threshold': ai_cfg.get('iou_threshold'),
            'input_size': ai_cfg.get('input_size'),
            'model_path': ai_cfg.get('model_path'),
            'labels_path': ai_cfg.get('labels_path'),
            'active_backend': ai_state['active_backend'],
            'mode': ai_state['mode'],
            'available': ai_state['inference_available'],
            'model_loaded': ai_state['model_loaded'],
            'error': ai_state['error'],
            'categories': ai_cfg.get('categories', []),
        },
        'alerts': main.config.get('alerts', {}),
        'auth': {
            'enabled': main.auth_enabled,
            'session_timeout_hours': main.effective_auth_config().get('session_timeout_hours'),
            'max_login_attempts': main.effective_auth_config().get('max_login_attempts'),
            'lockout_minutes': main.effective_auth_config().get('lockout_minutes'),
        },
        'storage': {
            'database': main.effective_storage_config().get('database'),
            'snapshots_dir': main.effective_storage_config().get('snapshots_dir'),
            'recordings_dir': main.effective_storage_config().get('recordings_dir'),
        },
        'live': main.effective_live_config(),
        'recording': main.effective_recording_config(),
    }


@router.post('/api/detect/frame')
async def detect_frame(request: Request):
    image_bytes, _filename, _content_type = await main._read_uploaded_image(request)
    if not image_bytes:
        raise HTTPException(status_code=400, detail='Uploaded image is empty')
    ai_settings = main.effective_ai_config()
    ai_state = main.ai_status_payload(ai_settings)
    ai_error: str | None = None
    min_confidence = main.compute_minimum_rule_confidence()

    def _run_detection() -> list:
        return main.detector.detect_image(image_bytes, confidence=min_confidence)

    try:
        detections = await asyncio.get_event_loop().run_in_executor(None, _run_detection)
    except main.DetectorUnavailableError as exc:
        detections = []
        ai_error = str(exc) or ai_state.get('last_detector_error') or ai_state.get('error') or 'Detector unavailable.'
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'detections': detections, 'count': len(detections), 'ai_backend': ai_state['active_backend'], 'ai_error': ai_error}


@router.delete('/api/system/runtime-data')
def delete_runtime_data(request: Request):
    main.require_admin(request)
    recordings = main.database.delete_all_recordings()
    main.delete_recording_files(recordings)
    deleted_events = main.database.delete_all_events()
    deleted_alerts = main.database.delete_all_alerts()
    deleted_objects = main.database.delete_all_objects()
    deleted_diagnostics = main.database.delete_all_camera_diagnostics()
    storage_config = main.effective_storage_config()
    deleted_snapshots = main.clear_runtime_media_directory(storage_config.get('snapshots_dir'))
    deleted_event_artifacts = main.clear_runtime_media_directory(storage_config.get('events_dir'))
    with main.active_rtsp_recordings_lock:
        main.active_rtsp_recordings.clear()
    result = {
        'ok': True,
        'deleted': {
            'recordings': len(recordings),
            'events': deleted_events,
            'alerts': deleted_alerts,
            'objects': deleted_objects,
            'camera_diagnostics': deleted_diagnostics,
            'snapshot_files': deleted_snapshots,
            'event_artifacts': deleted_event_artifacts,
        },
        'preserved': ['settings', 'users', 'sessions', 'rules'],
    }
    main.write_audit_log(request, 'delete_all', 'runtime_data', details=result['deleted'])
    return result
