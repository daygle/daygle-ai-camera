"""Live APIRouter.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth_gates import require_user
from app.config_facades import get_camera_config
from app.deps import get_recording_service
from app.detection_status import live_detection_status_payload
from app.detection_telemetry import detection_telemetry_payload
from app.pipeline_timing import pipeline_timing_payload
from app.postprocess_pool import pool_stats as postprocess_pool_stats
from app.utils import build_stream_url
from app.zone_detection import get_camera_instance

router = APIRouter()


def _queue_detection_snapshot(
    selected_config: dict,
    frame_bytes: bytes,
    *,
    captured_ts: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Feed live-page snapshots into the opt-in foreground detector path.

    When background detection is disabled, the Live page is the intended source
    of detection frames. Previously ``queue_live_stream_alerts`` had no
    production caller, leaving the status (including the motion bar) at
    ``Waiting`` even while snapshots visibly changed. The queue function keeps
    its own setting/interval/worker guards, so this is a cheap no-op when the
    background monitor is enabled and cannot duplicate active work.
    """
    from app.live_monitor import queue_live_stream_alerts

    frame = {
        'frame_number': 0,
        'timestamp': float(captured_ts or time.time()),
        'width': int(width or selected_config.get('width') or 1280),
        'height': int(height or selected_config.get('height') or 720),
    }
    queue_live_stream_alerts(
        frame_bytes,
        frame,
        selected_config,
        allow_when_background_enabled=True,
    )


@router.get('/api/live/detection-status')
def live_detection_status_api(request: Request, camera_id: str | None = None):
    # M1 fix: defence-in-depth. The auth middleware already enforces a
    # session for any non-public /api/* path; this handler-level gate
    # is the second line if a future refactor reorders middleware or
    # accidentally moves this path into PUBLIC_PATHS.
    require_user(request)
    return live_detection_status_payload(camera_id)


@router.get('/api/live/detection-telemetry')
def live_detection_telemetry_api(request: Request, camera_id: str | None = None):
    """Per-camera pipeline telemetry: which stages ran and where candidates went.

    Reports, per cycle, whether inference ran always-on or under the
    motion gate, whether the camera-motion guard suppressed the cycle, how many
    candidates survived each stage, and how many were rejected by zone, motion
    mode, camera scope, or confirmation. Without ``camera_id`` the response
    aggregates every camera.
    """
    require_user(request)
    return detection_telemetry_payload(camera_id)


@router.get('/api/live/pipeline-timing')
def live_pipeline_timing_api(request: Request, camera_id: str | None = None):
    """Per-stage latency breakdown for the live detection cycle.

    Reports p50/p95/max/mean milliseconds for every pipeline stage (motion
    scoring, preprocess, ONNX inference, NMS, tracking, filtering,
    confirmation, face identity, zone matching, alerts, event persistence),
    plus the frame-read time sampled before the cycle begins.

    ``unaccounted`` is the gap between the measured cycle and the sum of its
    stages. It is the roadmap's "p95 cycle overhead < 10 ms" acceptance target
    and doubles as a completeness alarm for this instrumentation: a persistent
    gap means a stage is still unmeasured, so the breakdown is not yet safe to
    act on.

    The postprocess worker pools and the inference scheduler ride along in
    ``pools`` / ``scheduler`` because the three answer one question -- "is the
    live pipeline slow, and if so is it the model, the queue, or the work behind
    it?" -- and an operator should not have to correlate three endpoints to see
    it. Without ``camera_id`` the stage percentiles pool every camera's samples
    and ``by_camera`` keeps the per-camera view.
    """
    require_user(request)
    payload = pipeline_timing_payload(camera_id)
    payload['pools'] = postprocess_pool_stats()
    try:
        from app.live_monitor import get_live_inference_scheduler

        payload['scheduler'] = get_live_inference_scheduler().stats()
    except Exception:  # noqa: BLE001 - diagnostics must never break the page
        payload['scheduler'] = None
    return payload


@router.get('/api/live/snapshot')
def live_snapshot(request: Request, camera_id: str | None = None, recording_service=Depends(get_recording_service)):
    # M1 fix: defence-in-depth handler gate (mirror
    # ``live_detection_status_api``).
    require_user(request)
    selected_config = get_camera_config(camera_id)
    resolved_id = str(selected_config.get('id') or camera_id or '')

    has_stream = bool(resolved_id and build_stream_url(selected_config))
    if has_stream:
        sample = recording_service.latest_frame_jpeg(resolved_id)
        if sample is not None:
            _queue_detection_snapshot(
                selected_config,
                sample[0],
                captured_ts=sample[1],
            )
            return Response(
                content=sample[0],
                media_type='image/jpeg',
                headers={'X-Frame-Timestamp': f'{float(sample[1]):.6f}'},
            )
    try:
        selected_camera = get_camera_instance(camera_id)
    except HTTPException:
        if has_stream:
            raise HTTPException(status_code=503, detail='Camera ingest is warming up; no frame available yet.') from None
        raise
    if hasattr(selected_camera, 'read_jpeg'):
        try:
            image_bytes, frame = selected_camera.read_jpeg()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        _queue_detection_snapshot(
            selected_config,
            image_bytes,
            captured_ts=frame.get('timestamp'),
            width=frame.get('width'),
            height=frame.get('height'),
        )
        return Response(
            content=image_bytes,
            media_type='image/jpeg',
            headers={'X-Frame-Timestamp': f'{float(frame.get("timestamp") or time.time()):.6f}'},
        )
    raise HTTPException(status_code=503, detail='Live snapshots require an ONVIF/RTSP camera backend.')
