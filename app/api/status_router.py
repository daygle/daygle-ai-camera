"""System Status APIRouter.

Extracted from ``app/main.py`` (Phase-8 of the hybrid-pattern router split).
Same template as ``app/api/alert_email_router.py`` (Phase-7), etc.:
``import app.main as main`` at module level, every global / helper read
through ``main.<name>`` *inside* handler bodies.

Handlers moved (2):

- GET   /api/status
- GET   /api/status/ai

BODY-REWRITE NOTE
Unlike prior routers (which already called helpers as ``main.X(...)``),
these two handlers in main.py reach module-level state via bare names
(``cameras_config``, ``ai_status_payload``, ``get_camera_instance``,
``get_camera_config``, ``live_detection_status_payload``). Once extracted
into ``app/api/status_router.py``, the bare names resolve to ZERO
attributes in this module's namespace -- they would NameError at request
time. Per the hybrid-pattern uniformity (and rule 5 of ``app/api/__init__.py``,
"the router contract is main.<name>"), each bare call is rewritten as
``main.<bare>`` here. Pure syntactic change with zero behavioral impact.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.cameras_config`` - the resolved per-camera config list (global).
- ``main.ai_status_payload`` - facaded AI subsystem status dict builder.
- ``main.get_camera_instance`` - resolves the live camera instance by id.
- ``main.get_camera_config`` - resolves a camera's static config dict by id.
- ``main.live_detection_status_payload`` - facaded live detection status.

Tests go through ``LocalClient.request`` rather than calling
``main.<attr>`` directly, so no back-compat alias on ``app.main`` is
needed. The Phase-7.1 invariant
``tests/test_api_router_split_invariants.py::test_app_api_imports_in_main_are_consumed``
will catch orphan-import regressions if a future refactor drops the
``from app.api.status_router import router as status_router`` rebind line
in ``app/main.py``.
"""

from __future__ import annotations

from fastapi import APIRouter

import app.main as main

router = APIRouter()


@router.get('/api/status')
def status(camera_id: str | None=None):
    if not main.cameras_config:
        ai_state = main.ai_status_payload()
        return {'status': 'online', 'mode': None, 'camera_id': None, 'camera_name': None, 'camera_detection': {}, 'ai_backend': ai_state['active_backend'], 'ai_available': ai_state['inference_available'], 'ai_error': ai_state['error'], 'ai_mode': ai_state['mode'], 'live_detection': main.live_detection_status_payload(camera_id), 'frame_number': 0, 'uptime_seconds': 0, 'resolution': {'width': 0, 'height': 0}}
    selected_camera = main.get_camera_instance(camera_id)
    selected_config = main.get_camera_config(camera_id)
    frame = selected_camera.get_frame()
    ai_state = main.ai_status_payload()
    return {'status': 'online', 'mode': selected_config.get('backend', 'onvif'), 'camera_id': selected_config.get('id'), 'camera_name': selected_config.get('name'), 'camera_detection': selected_config.get('detection', {}), 'ai_backend': ai_state['active_backend'], 'ai_available': ai_state['inference_available'], 'ai_error': ai_state['error'], 'ai_mode': ai_state['mode'], 'live_detection': main.live_detection_status_payload(camera_id), 'frame_number': frame['frame_number'], 'uptime_seconds': frame['uptime_seconds'], 'resolution': {'width': frame['width'], 'height': frame['height']}}


@router.get('/api/status/ai')
def ai_status():
    return main.ai_status_payload()
