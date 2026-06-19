"""Recordings APIRouter.

Extracted from ``app/main.py`` (Phase-3 of the hybrid-pattern router split).
Same template as ``app/api/settings_ai_router.py``: ``import app.main as main``
at module level, every global / helper read through ``main.<name>`` *inside*
handler bodies.

Handlers moved (8):

- GET    /api/recordings
- GET    /api/recordings/timeline
- POST   /api/recordings/purge
- GET    /api/recordings/{recording_id}
- GET    /api/recordings/{recording_id}/stream
- GET    /api/recordings/{recording_id}/download
- DELETE /api/recordings/{recording_id}
- DELETE /api/recordings

The splice was AST-driven, FunctionDef-by-FunctionDef, so ``_parse_iso_datetime``
and ``_recording_timeline_segment`` (in-block helpers that sit between the
extracted handlers) stayed on ``app.main``. The router reaches them as
``main._recording_timeline_segment(...)`` from ``recordings_timeline``.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.camera_default_name``, ``main.normalize_camera_id`` — camera
  resolution used by ``recordings_timeline``.
- ``main.effective_cameras_config``, ``main.effective_recording_config`` —
  config facades used both inside this router and by the broader live-detect /
  recording pipeline.
- ``main.require_admin`` — auth gate, also used by every other mutating route.
- ``main._recording_timeline_segment`` — purely in-block helper for
  ``recordings_timeline``. Stays on main because the same convention as
  ``_sound_status_reason`` / ``reload_detector`` / ``export_yolo_onnx`` keeps
  test-referenced or in-block helpers on ``app.main`` for back-compat.
- ``main._recording_capture_window``,
  ``main.write_live_history_detection_track``,
  ``main.load_recording_detection_track``,
  ``main.recording_track_sidecar_path``,
  ``main.recording_stream_path``,
  ``main.mp4_has_video_stream``,
  ``main.delete_recording_files``,
  ``main.purge_recordings_by_policy`` — all live-detection / recording helpers
  that are shared between this router, the live alert monitor, the upgrade
  script, and the cleanup worker. Their ``def`` blocks remain in
  ``app/main.py`` so other callers stay wired through ``main.<name>``.
- ``main.write_audit_log`` — also called by user / camera / alert mutations.
- ``main.database``, ``main.recording_service`` — shared mutable state.

Tests do NOT exercise these endpoints' ``main.<helper>`` calls directly
(they go through ``LocalClient.request``), so no additional
``main.<attr>`` references were added to ``tests/test_api.py`` by this
extraction. The existing
``tests/test_api_router_split_invariants.py::test_all_main_attr_references_resolve_on_app_main``
invariant covers the broader hybrid-pattern contract.

See ``app/api/__init__.py`` for the full hybrid-pattern rules and the
route-coverage invariant that would have caught the e365ec5 over-deletion
regression.
"""

from __future__ import annotations

import mimetypes
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

import app.main as main

router = APIRouter()


@router.get('/api/recordings')
def recordings(
    label: str | None = None,
    camera_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    alerted_only: bool = False,
    started_after: str | None = Query(None, description='ISO timestamp; include recordings started at or after this time.'),
    started_before: str | None = Query(None, description='ISO timestamp; include recordings started at or before this time.'),
    sort: str = Query('newest', pattern='^(newest|oldest)$', description='Sort order by started_at. Default: newest.'),
    source_type: str | None = Query(None, pattern='^(sound|object)$', description='Filter by recording type: sound or object.'),
):
    # Support comma-separated labels (e.g. ?label=person,car,cat_meow)
    labels: list[str] | None = None
    if label:
        labels = [l.strip().lower() for l in str(label).split(',') if l.strip()]
    return main.database.list_recordings(
        labels=labels,
        camera_id=camera_id,
        limit=limit,
        alerted_only=alerted_only,
        started_after=started_after,
        started_before=started_before,
        sort=sort,
        source_type=source_type,
    )


@router.get('/api/recordings/timeline')
def recordings_timeline(
    camera_id: str | None = None,
    day: str | None = None,
    tz_offset_minutes: int | None = Query(None, ge=-840, le=840),
):
    cameras = [
        {
            'id': str(camera_settings.get('id') or ''),
            'name': main.camera_default_name(camera_settings, f'Camera {index}'),
        }
        for index, camera_settings in enumerate(main.effective_cameras_config(), start=1)
    ]
    if not cameras and not camera_id:
        raise HTTPException(status_code=404, detail='No cameras configured')

    selected_camera_id = main.normalize_camera_id(camera_id or cameras[0]['id'])
    selected_camera = next((camera for camera in cameras if camera['id'] == selected_camera_id), None)
    if selected_camera is None:
        # Recordings outlive camera configuration: keep an explicitly requested
        # camera's timeline viewable even after the camera entry is removed.
        selected_camera = {'id': selected_camera_id, 'name': selected_camera_id}
        cameras = [*cameras, selected_camera]

    if day:
        try:
            target_day = datetime.strptime(day, '%Y-%m-%d').date()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='Invalid day. Use YYYY-MM-DD.') from exc
    else:
        target_day = datetime.now(timezone.utc).date()

    if tz_offset_minutes is None:
        timeline_timezone = timezone.utc
    else:
        # Browser getTimezoneOffset() is UTC-local minutes, so invert to get local UTC offset.
        timeline_timezone = timezone(timedelta(minutes=-tz_offset_minutes))

    day_start_local = datetime.combine(target_day, datetime.min.time(), tzinfo=timeline_timezone)
    day_end_local = day_start_local + timedelta(days=1)
    day_start = day_start_local.astimezone(timezone.utc)
    day_end = day_end_local.astimezone(timezone.utc)

    recordings = main.database.list_recordings_for_camera_day(selected_camera_id, day_start.isoformat(), day_end.isoformat())
    segments = [
        segment
        for segment in (
            main._recording_timeline_segment(recording, day_start, day_end)
            for recording in recordings
        )
        if segment is not None
    ]
    rec_config = main.effective_recording_config()
    return {
        'camera': selected_camera,
        'cameras': cameras,
        'day': target_day.isoformat(),
        'day_start': day_start.isoformat(),
        'day_end': day_end.isoformat(),
        'timeline_timezone_offset_minutes': tz_offset_minutes if tz_offset_minutes is not None else 0,
        'pre_event_seconds': max(0, int(rec_config.get('pre_event_seconds', 5))),
        'recordings': segments,
    }


@router.post('/api/recordings/purge')
def purge_recordings(request: Request):
    main.require_admin(request)
    return main.purge_recordings_by_policy(force=True)


@router.get('/api/recordings/{recording_id}')
def recording_detail(recording_id: int):
    recording = main.database.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    file_path = Path(str(recording.get('file_path') or ''))
    recording['track'] = main.load_recording_detection_track(file_path)
    # Backfill from the live monitor's in-memory history while it still covers
    # the clip's window (e.g. a recording finalized before this feature, viewed
    # shortly after capture). Older clips simply have no track and playback
    # falls back to the static event boxes - clips are never decoded or
    # re-analyzed for overlays.
    if (
        recording['track'] is None
        and str(file_path)
        and file_path.exists()
        and not main.recording_track_sidecar_path(file_path).exists()
    ):
        window = main._recording_capture_window(recording)
        if window and main.write_live_history_detection_track(
            recording_id, file_path, str(recording.get('camera_id') or '') or None, window[0], window[1],
        ):
            recording['track'] = main.load_recording_detection_track(file_path)
    return recording


@router.get('/api/recordings/{recording_id}/stream')
def stream_recording(recording_id: int, request: Request):
    recording = main.database.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    file_path = Path(recording['file_path'])
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail='Recording media file not found')

    stream_path = main.recording_stream_path(file_path)
    if not stream_path.exists() or not main.mp4_has_video_stream(stream_path):
        raise HTTPException(
            status_code=415,
            detail='Recording file is not a playable video stream. Generate a new recording to rebuild media.',
        )
    file_size = stream_path.stat().st_size
    media_type = mimetypes.guess_type(stream_path.name)[0] or 'video/mp4'
    range_header = request.headers.get('range')
    if not range_header:
        return FileResponse(stream_path, media_type=media_type)

    match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip())
    if not match:
        return Response(status_code=416, headers={'Content-Range': f'bytes */{file_size}'})
    start_text, end_text = match.groups()
    start = int(start_text) if start_text else 0
    end = int(end_text) if end_text else file_size - 1
    if start >= file_size or end < start:
        return Response(status_code=416, headers={'Content-Range': f'bytes */{file_size}'})
    end = min(end, file_size - 1)
    chunk_size = end - start + 1

    def iter_file():
        with stream_path.open('rb') as handle:
            handle.seek(start)
            remaining = chunk_size
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iter_file(),
        status_code=206,
        media_type=media_type,
        headers={
            'Content-Range': f'bytes {start}-{end}/{file_size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(chunk_size),
        },
    )


@router.get('/api/recordings/{recording_id}/download')
def download_recording(recording_id: int):
    recording = main.database.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    file_path = Path(recording['file_path'])
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail='Recording media file not found')
    stream_path = main.recording_stream_path(file_path)
    if not stream_path.exists() or not main.mp4_has_video_stream(stream_path):
        raise HTTPException(status_code=415, detail='Recording file is not a playable video stream.')
    started_at = str(recording.get('started_at') or '')
    safe_ts = re.sub(r'[^\w\-]', '_', started_at)[:19]
    filename = f'recording_{recording_id}_{safe_ts}.mp4'
    return FileResponse(
        stream_path,
        media_type='video/mp4',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@router.delete('/api/recordings/{recording_id}')
def delete_recording(recording_id: int, request: Request):
    main.require_admin(request)
    recording = main.database.delete_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    main.delete_recording_files([recording])
    main.write_audit_log(request, 'delete', 'recording', recording_id)
    return {'ok': True}


@router.delete('/api/recordings')
def delete_all_recordings(request: Request):
    main.require_admin(request)
    recordings = main.database.delete_all_recordings()
    main.delete_recording_files(recordings)
    main.write_audit_log(request, 'delete_all', 'recordings', details={'count': len(recordings)})
    return {'ok': True, 'deleted': len(recordings)}
