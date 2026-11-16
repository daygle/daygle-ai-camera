from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.app_log_router import _parse_entry, get_app_log
from app.utils import local_day_bounds_to_utc


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={'role': 'admin'}))


def _expected_since_until(date_from, date_to, tz_offset_minutes):
    start, end = local_day_bounds_to_utc(date_from, date_to, tz_offset_minutes)
    # journalctl gets absolute ``@<epoch>`` bounds; ``--until`` steps back one
    # second to stay half-open against the exclusive next-day boundary.
    return ['--since', f'@{int(start.timestamp())}', '--until', f'@{int(end.timestamp()) - 1}']


def test_application_log_passes_date_range_to_journalctl() -> None:
    completed = SimpleNamespace(
        stdout=json.dumps({
            '__REALTIME_TIMESTAMP': '1760000000000000',
            'PRIORITY': '6',
            'MESSAGE': 'INFO: application started',
        }),
    )
    require_admin = MagicMock()
    # Patch the dict ``get_app_log`` actually reads (its ``__globals__``), not
    # the module attribute ``app.api.app_log_router.require_admin``.
    # ``tests/support.py::_load_app`` reloads every ``app.*`` module (it pops
    # them from ``sys.modules`` and re-imports ``app.main``), so a module-path
    # patch resolves to a NEW module object while ``get_app_log`` stays bound to
    # the ORIGINAL one - the mock would never be seen and call_count stays 0.
    with patch.dict(get_app_log.__globals__, {'require_admin': require_admin}), \
         patch('app.api.app_log_router.subprocess.run', return_value=completed) as run:
        payload = get_app_log(
            _request(),
            lines=200,
            date_from='2026-01-10',
            date_to='2026-01-12',
            tz_offset_minutes=None,
        )

    assert require_admin.call_count == 1
    command = run.call_args.args[0]
    # No tz offset -> UTC days, handed to journalctl as absolute @epoch bounds.
    assert command[-4:] == _expected_since_until('2026-01-10', '2026-01-12', None)
    assert len(payload['entries']) == 1


def test_application_log_resolves_dates_in_viewer_timezone() -> None:
    completed = SimpleNamespace(stdout='')
    with patch.dict(get_app_log.__globals__, {'require_admin': MagicMock()}), \
         patch('app.api.app_log_router.subprocess.run', return_value=completed) as run:
        get_app_log(
            _request(),
            lines=200,
            date_from='2026-01-10',
            date_to='2026-01-12',
            tz_offset_minutes=-600,  # UTC+10
        )
    command = run.call_args.args[0]
    # The viewer's local days map to earlier UTC instants than the UTC-day case.
    assert command[-4:] == _expected_since_until('2026-01-10', '2026-01-12', -600)
    assert command[-4:] != _expected_since_until('2026-01-10', '2026-01-12', None)


@pytest.mark.parametrize('field, value', [
    ('date_from', '2026/01/10'),
    ('date_to', 'not-a-date'),
    ('date_from', '2026-02-30'),
])
def test_application_log_rejects_invalid_dates(field: str, value: str) -> None:
    kwargs = {field: value}
    with patch.dict(get_app_log.__globals__, {'require_admin': MagicMock()}), \
         patch('app.api.app_log_router.subprocess.run') as run:
        with pytest.raises(HTTPException) as exc_info:
            get_app_log(_request(), **kwargs)

    assert exc_info.value.status_code == 400
    run.assert_not_called()


def test_application_log_rejects_reversed_date_range() -> None:
    with patch.dict(get_app_log.__globals__, {'require_admin': MagicMock()}), \
         patch('app.api.app_log_router.subprocess.run') as run:
        with pytest.raises(HTTPException) as exc_info:
            get_app_log(
                _request(),
                date_from='2026-01-12',
                date_to='2026-01-10',
            )

    assert exc_info.value.status_code == 400
    run.assert_not_called()


def test_application_log_stream_survives_oversized_journal_line(monkeypatch) -> None:
    """An entry longer than the pipe's line limit is skipped, not fatal.

    asyncio's StreamReader raises ``ValueError`` from ``readline()`` for a line
    over its limit (64 KiB by default). Uncaught, that ended the admin's live
    log stream on the first long journal entry (a traceback, ffmpeg stderr).
    """
    import asyncio
    import sys

    from app.api.app_log_router import stream_app_log

    long_entry = json.dumps({'PRIORITY': '6', 'MESSAGE': 'x' * 20_000})
    short_entry = json.dumps({'PRIORITY': '6', 'MESSAGE': 'after the long line'})
    script = f'import sys; sys.stdout.write({long_entry!r} + "\\n" + {short_entry!r} + "\\n")'
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*_cmd, **kwargs):
        return await real_exec(sys.executable, '-c', script, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_exec)

    class _Request:
        async def is_disconnected(self) -> bool:
            return False

    async def collect() -> list[str]:
        with patch.dict(stream_app_log.__globals__, {
            'require_admin': MagicMock(),
            '_STREAM_LINE_LIMIT_BYTES': 4096,
        }):
            response = await stream_app_log(_Request())
            return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(collect())
    assert len(chunks) == 1
    assert json.loads(chunks[0].removeprefix('data: '))['message'] == 'after the long line'


def _journal_line(message: str, priority: str = '6') -> dict:
    return {'__REALTIME_TIMESTAMP': '1760000000000000', 'PRIORITY': priority, 'MESSAGE': message}


@pytest.mark.parametrize(
    ('message', 'level', 'shown'),
    [
        # Python logging prefixes (the journal records them all at INFO).
        ('WARNING:daygle.ai.rtsp_broker:RTSP broker connection to 192.168.40.104:554 failed: timed out',
         'WARNING', 'RTSP broker connection to 192.168.40.104:554 failed: timed out'),
        ('ERROR:    Exception in ASGI application', 'ERROR', 'Exception in ASGI application'),
        ('CRITICAL:daygle.ai:disk gone', 'CRIT', 'disk gone'),
        ('INFO:daygle.ai.recording:No audio sidecars in this clip window', 'INFO',
         'No audio sidecars in this clip window'),
        ('DEBUG:daygle.ai.tunnel:cloudflared: 2026-11-16T05:53:49Z ERR client closed', 'DEBUG',
         'cloudflared: 2026-11-16T05:53:49Z ERR client closed'),
        # cloudflared output forwarded at INFO carries its own level.
        ('INFO:daygle.ai.tunnel:cloudflared: 2026-11-16T01:45:00Z ERR Connection terminated connIndex=2',
         'ERROR', 'cloudflared: 2026-11-16T01:45:00Z ERR Connection terminated connIndex=2'),
        ('INFO:daygle.ai.tunnel:cloudflared: 2026-11-16T01:45:00Z WRN Retrying connection', 'WARNING',
         'cloudflared: 2026-11-16T01:45:00Z WRN Retrying connection'),
        ('INFO:daygle.ai.tunnel:cloudflared: 2026-11-16T01:45:00Z INF Registered tunnel connection', 'INFO',
         'cloudflared: 2026-11-16T01:45:00Z INF Registered tunnel connection'),
        # OpenCV writes straight to stderr with its own markers.
        ('[ WARN:1@4212.700] global cap_ffmpeg_impl.hpp:453 Stream timeout triggered', 'WARNING',
         '[ WARN:1@4212.700] global cap_ffmpeg_impl.hpp:453 Stream timeout triggered'),
        ('[ERROR:1@4242.744] global cap_ffmpeg_impl.hpp:1890 retrieveFrame Picture does not contain data',
         'ERROR', '[ERROR:1@4242.744] global cap_ffmpeg_impl.hpp:1890 retrieveFrame Picture does not contain data'),
        # Plain output with no marker keeps the journal priority.
        ('Stopping daygle-ai-camera.service - Daygle AI Camera FastAPI service...', 'INFO',
         'Stopping daygle-ai-camera.service - Daygle AI Camera FastAPI service...'),
    ],
)
def test_entry_level_comes_from_the_message_text(message: str, level: str, shown: str) -> None:
    entry = _parse_entry(_journal_line(message))
    assert entry['level'] == level
    assert entry['message'] == shown


def test_explicit_journal_priority_is_never_downgraded_by_the_text() -> None:
    # systemd's own error about the unit stays an error even if it starts "INFO:".
    assert _parse_entry(_journal_line('INFO:something odd', priority='3'))['level'] == 'ERROR'
    # ... and a more severe text level still wins over a non-default priority.
    assert _parse_entry(_journal_line('ERROR:daygle.ai:boom', priority='5'))['level'] == 'ERROR'


def test_level_filter_uses_the_text_level_not_journalctl_priority() -> None:
    lines = [
        _journal_line('INFO:daygle.ai.recording:clip saved'),
        _journal_line('WARNING:daygle.ai.rtsp_broker:RTSP broker connection failed'),
        _journal_line('INFO:daygle.ai.tunnel:cloudflared: 2026-11-16T01:45:00Z ERR Connection terminated'),
        _journal_line('[ WARN:1@4212.700] global cap_ffmpeg_impl.hpp:453 Stream timeout triggered'),
    ]
    completed = SimpleNamespace(stdout='\n'.join(json.dumps(line) for line in lines))
    with patch.dict(get_app_log.__globals__, {'require_admin': MagicMock()}), \
         patch('app.api.app_log_router.subprocess.run', return_value=completed) as run:
        warnings = get_app_log(_request(), lines=200, level='warning', date_from=None, date_to=None, tz_offset_minutes=None)
        errors = get_app_log(_request(), lines=200, level='error', date_from=None, date_to=None, tz_offset_minutes=None)
        everything = get_app_log(_request(), lines=200, level=None, date_from=None, date_to=None, tz_offset_minutes=None)

    # journalctl's -p would see every line as INFO and drop them all.
    for call in run.call_args_list:
        assert '-p' not in call.args[0]
    assert [entry['level'] for entry in warnings['entries']] == ['WARNING', 'ERROR', 'WARNING']
    assert [entry['level'] for entry in errors['entries']] == ['ERROR']
    assert len(everything['entries']) == 4
