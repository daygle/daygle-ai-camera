"""Recordings APIRouter.
"""

from __future__ import annotations

import mimetypes
import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.auth_gates import require_admin, require_user
from app.camera_config import normalize_camera_id
from app.config_facades import effective_cameras_config, effective_recording_config
from app.deps import get_database
from app.pagination import decode_cursor, encode_cursor
from app.media_utils import (
    _recording_timeline_segment,
    mp4_has_video_stream,
    recording_stream_path,
    safe_storage_path,
)
from app.recording_extension import (
    _recording_capture_window,
    delete_recording_files,
    load_recording_detection_track,
    recording_track_sidecar_path,
    write_live_history_detection_track,
)
from app.request_helpers import write_audit_log
from app.utils import camera_default_name
from app.backup import purge_recordings_by_policy

router = APIRouter()

# ``(source path, mtime_ns, size) -> (stream path, mtime_ns, size)`` for clips
# already proven playable. A browser plays a clip through a long run of HTTP
# Range requests (initial load, every seek, the tail probe), and each one used
# to re-run up to three ffprobe subprocesses (video codec, audio codec, video
# stream presence) on an unchanged file. Only positive results are cached, and
# an entry is used only while both files still carry the recorded stat
# signature, so a re-encoded clip or a rebuilt playback sidecar is re-probed.
_PLAYABLE_STREAM_CACHE: OrderedDict[tuple[str, int, int], tuple[Path, int, int]] = OrderedDict()
_PLAYABLE_STREAM_CACHE_MAX = 256
_PLAYABLE_STREAM_CACHE_LOCK = threading.Lock()


def _playable_stream_path(file_path: Path) -> Path | None:
    """Return the browser-playable copy of ``file_path``, or ``None``."""
    source_stat = file_path.stat()
    key = (str(file_path), source_stat.st_mtime_ns, source_stat.st_size)
    with _PLAYABLE_STREAM_CACHE_LOCK:
        cached = _PLAYABLE_STREAM_CACHE.get(key)
    if cached is not None:
        stream_path, mtime_ns, size = cached
        try:
            stream_stat = stream_path.stat()
        except OSError:
            stream_stat = None
        if stream_stat is not None and (stream_stat.st_mtime_ns, stream_stat.st_size) == (mtime_ns, size):
            with _PLAYABLE_STREAM_CACHE_LOCK:
                _PLAYABLE_STREAM_CACHE.move_to_end(key)
            return stream_path
    stream_path = recording_stream_path(file_path)
    if not stream_path.exists() or not mp4_has_video_stream(stream_path):
        return None
    stream_stat = stream_path.stat()
    with _PLAYABLE_STREAM_CACHE_LOCK:
        _PLAYABLE_STREAM_CACHE[key] = (stream_path, stream_stat.st_mtime_ns, stream_stat.st_size)
        _PLAYABLE_STREAM_CACHE.move_to_end(key)
        while len(_PLAYABLE_STREAM_CACHE) > _PLAYABLE_STREAM_CACHE_MAX:
            _PLAYABLE_STREAM_CACHE.popitem(last=False)
    return stream_path


def _visible_recording(user: dict, db, recording_id: int) -> dict:
    """Fetch a recording the signed-in user may see, else raise 404.

    Viewers cannot retrieve another user's recording. The answer is 404 (NOT
    403) so the existence of someone else's recording is not leaked via the
    status code.
    """
    recording = db.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    if str(user.get('role') or '').strip().lower() != 'admin':
        owner_id = recording.get('owner_user_id')
        if owner_id is not None and int(owner_id) != int(user.get('id') or 0):
            raise HTTPException(status_code=404, detail='Recording not found')
    return recording


@router.get('/api/recordings')
def recordings(
    request: Request,
    label: str | None = None,
    camera_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None),
    alerted_only: bool = False,
    started_after: str | None = Query(None, description='ISO timestamp; include recordings started at or after this time.'),
    started_before: str | None = Query(None, description='ISO timestamp; include recordings started at or before this time.'),
    sort: str = Query('newest', pattern='^(newest|oldest)$', description='Sort order by started_at. Default: newest.'),
    source_type: str | None = Query(None, pattern='^(sound|object)$', description='Filter by recording type: sound or object.'),
    db=Depends(get_database),
):
    # M1 fix: defence-in-depth - middleware already enforces a session
    # for non-public /api/* paths; this handler-level gate is the
    # second line if a future refactor reorders middleware or adds
    # this path to PUBLIC_PATHS by accident.
    user = require_user(request)
    # round-5 finish / M2: capture identity for post-fetch scope filter.
    session_user_id = int(user['id'])
    session_role = str(user.get('role') or '').strip().lower()
    labels: list[str] | None = None
    if label:
        labels = [part.strip().lower() for part in str(label).split(',') if part.strip()]
    try:
        decoded = decode_cursor(cursor, 'recordings', sort) if cursor else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    owner_user_id = None if session_role == 'admin' else session_user_id
    results, next_cursor = db.list_recordings_page(
        labels=labels,
        camera_id=camera_id,
        limit=limit,
        alerted_only=alerted_only,
        started_after=started_after,
        started_before=started_before,
        sort=sort,
        source_type=source_type,
        cursor=decoded,
        owner_user_id=owner_user_id,
    )
    return {
        'items': results,
        'next_cursor': (
            encode_cursor('recordings', sort, next_cursor[0], next_cursor[1])
            if next_cursor else None
        ),
    }


@router.get('/api/recordings/timeline')
def recordings_timeline(
    request: Request,
    camera_id: str | None = None,
    day: str | None = None,
    tz_offset_minutes: int | None = Query(None, ge=-840, le=840),
    db=Depends(get_database),
):
    user = require_user(request)
    cameras = [
        {
            'id': str(camera_settings.get('id') or ''),
            'name': camera_default_name(camera_settings, f'Camera {index}'),
        }
        for index, camera_settings in enumerate(effective_cameras_config(), start=1)
    ]
    if not cameras and not camera_id:
        raise HTTPException(status_code=404, detail='No cameras configured')

    selected_camera_id = normalize_camera_id(camera_id or cameras[0]['id'])
    selected_camera = next((camera for camera in cameras if camera['id'] == selected_camera_id), None)
    if selected_camera is None:
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
        timeline_timezone = timezone(timedelta(minutes=-tz_offset_minutes))

    day_start_local = datetime.combine(target_day, datetime.min.time(), tzinfo=timeline_timezone)
    day_end_local = day_start_local + timedelta(days=1)
    day_start = day_start_local.astimezone(timezone.utc)
    day_end = day_end_local.astimezone(timezone.utc)

    recordings_list = db.list_recordings_for_camera_day(selected_camera_id, day_start.isoformat(), day_end.isoformat())
    request_user = user
    if str(request_user.get('role') or '').strip().lower() != 'admin':
        session_user_id = int(request_user.get('id') or 0)
        recordings_list = [
            recording for recording in recordings_list
            if recording.get('owner_user_id') is None
            or int(recording.get('owner_user_id') or 0) == session_user_id
        ]
    segments = [
        segment
        for segment in (
            _recording_timeline_segment(recording, day_start, day_end)
            for recording in recordings_list
        )
        if segment is not None
    ]
    rec_config = effective_recording_config()
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
    require_admin(request)
    return purge_recordings_by_policy(force=True)


@router.get('/api/recordings/{recording_id}')
def recording_detail(request: Request, recording_id: int, db=Depends(get_database)):
    recording = _visible_recording(require_user(request), db, recording_id)
    file_path = safe_storage_path(recording.get('file_path'), roots=('recordings_dir',))
    recording['track'] = load_recording_detection_track(file_path) if file_path is not None else None
    if (
        file_path is not None
        and recording['track'] is None
        and file_path.exists()
        and not recording_track_sidecar_path(file_path).exists()
    ):
        window = _recording_capture_window(recording)
        if window and write_live_history_detection_track(
            recording_id, file_path, str(recording.get('camera_id') or '') or None, window[0], window[1],
        ):
            recording['track'] = load_recording_detection_track(file_path)
    return recording


@router.get('/api/recordings/{recording_id}/stream')
def stream_recording(recording_id: int, request: Request, db=Depends(get_database)):
    recording = _visible_recording(require_user(request), db, recording_id)
    file_path = safe_storage_path(recording.get('file_path'), roots=('recordings_dir',))
    if file_path is None or not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail='Recording media file not found')

    stream_path = _playable_stream_path(file_path)
    if stream_path is None:
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
    try:
        if start_text:
            # ``bytes=start-`` (open-ended) or ``bytes=start-end`` (explicit).
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
        elif end_text:
            # Suffix range ``bytes=-N``: the LAST N bytes of the file (RFC 7233
            # §2.1). The previous code parsed this as ``0-N`` and served the
            # first N bytes instead, corrupting playback for clients (e.g.
            # Safari) that request the tail of the media with a suffix range.
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return Response(status_code=416, headers={'Content-Range': f'bytes */{file_size}'})
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            # ``bytes=-`` with neither bound is unsatisfiable.
            return Response(status_code=416, headers={'Content-Range': f'bytes */{file_size}'})
    except (ValueError, OverflowError):
        return Response(status_code=416, headers={'Content-Range': f'bytes */{file_size}'})
    # Defence-in-depth: reject negative values, start beyond file, and end before start.
    # Overflow from maliciously large integers is caught by the try/except above.
    if start < 0 or end < 0 or start >= file_size or end < start:
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
def download_recording(request: Request, recording_id: int, db=Depends(get_database)):
    recording = _visible_recording(require_user(request), db, recording_id)
    file_path = safe_storage_path(recording.get('file_path'), roots=('recordings_dir',))
    if file_path is None or not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail='Recording media file not found')
    stream_path = _playable_stream_path(file_path)
    if stream_path is None:
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
def delete_recording(recording_id: int, request: Request, db=Depends(get_database)):
    user = require_admin(request)
    # Fetch and ownership-check BEFORE the destructive delete. The previous
    # order deleted the row first and then ran an ownership check that could
    # never fire (``require_admin`` already guarantees admin), so if the route
    # gate were ever relaxed to ``require_user`` a non-owner would destroy the
    # row before the check ran. Reading first keeps the guard correct
    # regardless of the top gate.
    recording = db.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    # round-5 finish / M2: viewer cannot delete another user's recording --
    # returning 404 (NOT 403) so the existence of someone-else's recording is
    # not leaked via the response status code.
    if str(user.get('role') or '').strip().lower() != 'admin':
        owner_id = recording.get('owner_user_id')
        if owner_id is not None and int(owner_id) != int(user.get('id') or 0):
            raise HTTPException(status_code=404, detail='Recording not found')
    deleted = db.delete_recording(recording_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    delete_recording_files([deleted])
    write_audit_log(request, db, 'delete', 'recording', recording_id)
    return {'ok': True}


@router.delete('/api/recordings')
def delete_all_recordings(request: Request, db=Depends(get_database)):
    require_admin(request)
    recordings_list = db.delete_all_recordings()
    delete_recording_files(recordings_list)
    write_audit_log(request, db, 'delete_all', 'recordings', details={'count': len(recordings_list)})
    return {'ok': True, 'deleted': len(recordings_list)}
