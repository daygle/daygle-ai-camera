from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.app_log_router import get_app_log
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
