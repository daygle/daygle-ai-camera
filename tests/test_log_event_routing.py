"""Every diagnostic event type lands on the right System log tab.

Scans ``app/`` for calls that write the Camera Log (``log_camera_diagnostic``
and the recorder's ``_emit_diagnostic`` callback) or the AI Log
(``log_ai_diagnostic``), and checks each literal event type is offered in
that page's event-type filter and *not* in the other page's filter, so a new
event is never unfilterable or listed on the wrong tab.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# callee name -> (log, index of the positional event_type argument)
_CALLS = {
    'log_camera_diagnostic': ('camera', 1),
    '_emit_diagnostic': ('camera', 1),
    'log_ai_diagnostic': ('ai', 0),
}


def _emitted_event_types() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {'camera': set(), 'ai': set()}
    for path in (ROOT / 'app').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
            if name not in _CALLS:
                continue
            log, index = _CALLS[name]
            arg = next((kw.value for kw in node.keywords if kw.arg == 'event_type'), None)
            if arg is None and len(node.args) > index:
                arg = node.args[index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found[log].add(arg.value)
    return found


def _filter_options(page: str) -> set[str]:
    html = (ROOT / 'web' / f'{page}-log.html').read_text(encoding='utf-8')
    select = re.search(r'<select id="filterEventType".*?</select>', html, re.S)
    assert select, f'{page}-log.html has no event-type filter'
    return {v for v in re.findall(r'<option value="([^"]*)">', select.group(0)) if v}


def test_scan_finds_both_logs():
    emitted = _emitted_event_types()
    assert 'camera_offline' in emitted['camera']
    assert 'ai_verification_filtered' in emitted['ai']


def test_every_emitted_event_is_filterable_on_its_own_tab():
    emitted = _emitted_event_types()
    for log in ('camera', 'ai'):
        missing = emitted[log] - _filter_options(log)
        assert not missing, f'{log} log events missing from the {log}-log filter: {sorted(missing)}'


def test_no_event_is_filed_under_the_wrong_tab():
    emitted = _emitted_event_types()
    assert not emitted['camera'] & emitted['ai'], 'an event type is written to both logs'
    assert not emitted['camera'] & _filter_options('ai'), 'camera events listed on the AI Log'
    assert not emitted['ai'] & _filter_options('camera'), 'AI events listed on the Camera Log'


def test_filters_offer_only_events_that_are_still_emitted():
    emitted = _emitted_event_types()
    for log in ('camera', 'ai'):
        stale = _filter_options(log) - emitted[log]
        assert not stale, f'{log}-log filter lists event types nothing emits: {sorted(stale)}'
