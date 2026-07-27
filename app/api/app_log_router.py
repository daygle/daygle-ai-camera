"""Application log router - streams journalctl output for daygle-ai-camera."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.auth_gates import require_admin

router = APIRouter()

_PRIORITY_LABEL: dict[str, str] = {
    '0': 'EMERG',
    '1': 'ALERT',
    '2': 'CRIT',
    '3': 'ERROR',
    '4': 'WARNING',
    '5': 'NOTICE',
    '6': 'INFO',
    '7': 'DEBUG',
}

# Maps UI level names to journalctl -p values (inclusive of more-severe levels).
_LEVEL_TO_PRIORITY: dict[str, str] = {
    'error': '3',
    'warning': '4',
    'notice': '5',
    'info': '6',
    'debug': '7',
}

_SERVICE = 'daygle-ai-camera'

# Benign uvicorn protocol noise: a browser's HTTPS-first attempt (or a proxy
# health check) sends a TLS handshake to the plain-HTTP port, which uvicorn
# rejects one-per-connection. New occurrences are already dropped at the source
# (see ``main._suppress_uvicorn_request_noise``); this also hides any already
# recorded in the journal so the viewer stays clean.
_NOISE_MESSAGE = 'Invalid HTTP request received'

# Successful (2xx/3xx) uvicorn access lines: e.g.
#   192.168.30.2:47614 - "GET /api/stats HTTP/1.1" 200 OK
# The dashboard polls constantly, so these dominate the viewer while carrying
# no diagnostic value. New ones are already dropped at the source (see
# ``main._DropSuccessfulAccessLogNoise``); this also hides any already recorded
# in the journal. 4xx/5xx access lines don't match, so errors stay visible.
_ACCESS_OK_RE = re.compile(r'"\w+ .+ HTTP/[\d.]+" (?:2\d\d|3\d\d)\b')
_SOUND_DETECTION_NOISE_RE = re.compile(r'^Sound detected on ')


def _is_noise(entry: dict) -> bool:
    message = str(entry.get('message', ''))
    return (
        _NOISE_MESSAGE in message
        or bool(_ACCESS_OK_RE.search(message))
        or bool(_SOUND_DETECTION_NOISE_RE.search(message))
    )


# Strip the syslog-style level prefix from a raw log message.
# Journalctl MESSAGE fields typically start with "LEVEL:     " (uvicorn access
# logs) or "LEVEL:logger.name:" (app logger). Since the parser returns ``level``
# as a separate field, stripping this prefix makes all log entries align
# cleanly in the viewer. Safe no-op on messages without a level prefix.
_LEVEL_PREFIX_PATTERN = re.compile(r'^[A-Z]+:(\S+:)?\s*')


def _parse_entry(raw: dict) -> dict:
    ts_us = raw.get('__REALTIME_TIMESTAMP', '')
    try:
        ts = datetime.fromtimestamp(int(ts_us) / 1_000_000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        ts = ''
    priority = str(raw.get('PRIORITY', '6'))
    message = raw.get('MESSAGE', '')
    if isinstance(message, list):
        try:
            message = bytes(message).decode('utf-8', errors='replace')
        except Exception:
            message = repr(message)
    message = str(message)
    # Strip redundant syslog level prefix - the ``level`` field already
    # carries the severity, so "INFO:     " or "INFO:daygle.ai:" etc. from
    # the raw message is just visual noise in the viewer.
    message = _LEVEL_PREFIX_PATTERN.sub('', message, count=1)
    return {
        'timestamp': ts,
        'level': _PRIORITY_LABEL.get(priority, 'INFO'),
        'message': message,
    }


@router.get('/api/application-log')
def get_app_log(
    request: Request,
    lines: int = Query(200, ge=1, le=1000),
    level: str | None = None,
):
    require_admin(request)
    cmd = ['journalctl', '-u', _SERVICE, '-n', str(lines), '-o', 'json', '--no-pager']
    if level and level in _LEVEL_TO_PRIORITY:
        cmd += ['-p', _LEVEL_TO_PRIORITY[level]]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        entries: list[dict] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = _parse_entry(json.loads(line))
            except Exception:
                continue
            if _is_noise(entry):
                continue
            entries.append(entry)
        return {'entries': entries}
    except FileNotFoundError:
        return {'entries': [], 'unavailable': True}
    except subprocess.TimeoutExpired:
        return {'entries': [], 'error': 'journalctl timed out'}


@router.get('/api/application-log/stream')
async def stream_app_log(request: Request):
    require_admin(request)

    async def generate():
        cmd = ['journalctl', '-u', _SERVICE, '-f', '-n', '0', '-o', 'json', '--no-pager']
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            yield 'data: {"error":"journalctl not available"}\n\n'
            return
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'
                    continue
                if not raw_line:
                    break
                line_str = raw_line.decode('utf-8', errors='replace').strip()
                if not line_str:
                    continue
                try:
                    entry = _parse_entry(json.loads(line_str))
                except Exception:
                    continue
                if _is_noise(entry):
                    continue
                yield f'data: {json.dumps(entry)}\n\n'
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()

    return StreamingResponse(
        generate(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )
