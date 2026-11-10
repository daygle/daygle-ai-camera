"""Application Log entries carry a ``source`` that the grouped filter offers.

App modules log on ``daygle.ai.<area>`` child loggers; ``app_log_router``
turns each journal line's logger name into a source key. These tests pin the
mapping, and keep the Source filter in web/application-log.html, the
router's known sources, and the logger names used in ``app/`` in step.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.api import app_log_router
from app.api.app_log_router import _parse_entry

ROOT = Path(__file__).resolve().parent.parent

_FALLBACK_SOURCES = {'web', 'core', 'other'}


def _entry(message: str) -> dict:
    return _parse_entry({'__REALTIME_TIMESTAMP': '1760000000000000', 'PRIORITY': '6', 'MESSAGE': message})


@pytest.mark.parametrize('message, source, text', [
    ('INFO:daygle.ai.recording:Clip saved', 'recording', 'Clip saved'),
    ('WARNING:daygle.ai.cameras:Camera 3 stalled: reconnecting', 'cameras', 'Camera 3 stalled: reconnecting'),
    ('INFO:daygle.ai.faces:Matcher ready', 'faces', 'Matcher ready'),
    ('INFO:daygle.notifications:Push sent', 'alerts', 'Push sent'),
    ('INFO:daygle.sound:YAMNet ready', 'sound', 'YAMNet ready'),
    ('WARNING:daygle.auth:Login failed', 'auth', 'Login failed'),
    ('INFO:app.storage:Purged 3 clips', 'storage', 'Purged 3 clips'),
    ('INFO:daygle.ai:Startup complete', 'core', 'Startup complete'),
    ('ERROR:asyncio:Task exception', 'core', 'Task exception'),
    ('INFO:daygle.ai.unknown_area:hello', 'core', 'hello'),
    ('INFO:     Started server process [42]', 'web', 'Started server process [42]'),
    ('WARNING: Invalid header', 'web', 'Invalid header'),
    ('ERROR:uvicorn.error:Exception in ASGI application', 'web', 'Exception in ASGI application'),
    ('Traceback (most recent call last):', 'other', 'Traceback (most recent call last):'),
])
def test_entries_are_tagged_with_their_source(message: str, source: str, text: str) -> None:
    entry = _entry(message)
    assert entry['source'] == source
    assert entry['message'] == text


def _all_router_sources() -> set[str]:
    return set(app_log_router._AREA_SOURCES) | set(app_log_router._LOGGER_SOURCES.values()) | _FALLBACK_SOURCES


def _filter_options() -> set[str]:
    html = (ROOT / 'web' / 'application-log.html').read_text(encoding='utf-8')
    select = re.search(r'<select id="filterSource".*?</select>', html, re.S)
    assert select, 'application-log.html has no Source filter'
    return {v for v in re.findall(r'<option value="([^"]*)">', select.group(0)) if v}


def test_source_filter_matches_router_sources() -> None:
    assert _filter_options() == _all_router_sources()


def test_every_app_logger_area_is_a_known_source() -> None:
    areas: set[str] = set()
    for path in (ROOT / 'app').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, 'attr', None) == 'getLogger'
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith('daygle.ai.')
            ):
                areas.add(node.args[0].value.split('.')[2])
    assert areas, 'no daygle.ai.<area> loggers found'
    unknown = areas - set(app_log_router._AREA_SOURCES)
    assert not unknown, f'loggers whose area the Application Log does not group: {sorted(unknown)}'
