"""Live APIRouter.

Extracted from ``app/main.py`` lines 2776-2795 (Phase 10 of the
hybrid-pattern router split). Same template as ``app/api/status_router.py``
(Phase 8): ``import app.main as main`` at module level, every global /
helper / test-referenced symbol read through ``main.<name>`` *inside* handler
bodies.

Handlers moved (2):

- GET /api/live/detection-status
- GET /api/live/snapshot

BODY-REWRITE NOTE
Handlers in the original main.py referenced module-level helpers in
main.py via bare names (``live_detection_status_payload``,
``get_camera_config``, ``build_stream_url``, ``recording_service``,
``get_camera_instance``). After extraction to this router, those bare
names resolve to ZERO attributes in our namespace -- handlers would
NameError at request time. Per hybrid-pattern uniformity (rule 5 of
``app/api/__init__.py``), each bare call is rewritten as ``main.<bare>``
in the router body. Zero behavioral impact: the helpers still live on
``app.main`` and are now reached through the ``main.`` prefix rather
than the implicit module-namespace lookup the original `def`-level
resolution provided.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.live_detection_status_payload`` -- the snapshot-shaped dict the
  detection-status handler returns (lives on main for test back-compat:
  tests may patch ``main.live_detection_status_payload``).
- ``main.get_camera_config`` -- config dict lookup used by snapshot
  handler to resolve ``resolved_id``.
- ``main.build_stream_url`` -- bool predicate deciding whether the
  snapshot path can short-circuit to ``recording_service.latest_frame_jpeg``.
- ``main.recording_service`` -- the RecordingService instance with
  ``latest_frame_jpeg(camera_id)``.
- ``main.get_camera_instance`` -- ONVIF/RTSP camera handle with
  ``read_jpeg()`` for the fallback snapshot path.

FastAPI builtin ``Response`` + ``HTTPException`` stay at top-level
imports (no ``main.`` prefix needed -- these are part of the framework
contract, not module-level state).

Tests go through ``LocalClient.request`` rather than calling
``main.live_detection_status_api`` / ``main.live_snapshot`` directly,
so no back-compat alias on ``app.main`` is needed. The Phase 7.1
invariant
``tests/test_api_router_split_invariants.py::test_app_api_imports_in_main_are_consumed``
will catch any orphan-import regression if a future refactor drops the
``from app.api.live_router import router as live_router`` rebind line
in ``app/main.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

import app.main as main

router = APIRouter()


@router.get('/api/live/detection-status')
def live_detection_status_api(camera_id: str | None = None):
    return main.live_detection_status_payload(camera_id)


@router.get('/api/live/snapshot')
def live_snapshot(camera_id: str | None = None):
    selected_config = main.get_camera_config(camera_id)
    resolved_id = str(selected_config.get('id') or camera_id or '')
    if resolved_id and main.build_stream_url(selected_config):
        sample = main.recording_service.latest_frame_jpeg(resolved_id)
        if sample is not None:
            return Response(content=sample[0], media_type='image/jpeg')
    selected_camera = main.get_camera_instance(camera_id)
    if hasattr(selected_camera, 'read_jpeg'):
        try:
            image_bytes, frame = selected_camera.read_jpeg()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(content=image_bytes, media_type='image/jpeg')
    raise HTTPException(status_code=503, detail='Live snapshots require an ONVIF/RTSP camera backend.')
