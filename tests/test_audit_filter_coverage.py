"""The Audit Log filters cover every action and resource the app records.

Scans ``app/`` for ``write_audit_log`` / ``add_audit_log`` calls and checks
each literal action is offered in the Action filter, and each literal
resource is reachable from the Resource filter (which the server matches by
prefix, so ``person`` also covers ``person.face``). It also checks neither
filter offers options nothing records.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _recorded() -> tuple[set[str], set[str]]:
    actions: set[str] = set()
    resources: set[str] = set()
    for path in (ROOT / 'app').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            if name == 'write_audit_log':
                # write_audit_log(request, db, action, resource, ...)
                action = node.args[2] if len(node.args) > 2 else kwargs.get('action')
                resource = node.args[3] if len(node.args) > 3 else kwargs.get('resource')
            elif name == 'add_audit_log':
                action, resource = kwargs.get('action'), kwargs.get('resource')
            else:
                continue
            if isinstance(action, ast.Constant) and isinstance(action.value, str):
                actions.add(action.value)
            if isinstance(resource, ast.Constant) and isinstance(resource.value, str):
                resources.add(resource.value)
    return actions, resources


def _options(select_id: str) -> set[str]:
    html = (ROOT / 'web' / 'audit.html').read_text(encoding='utf-8')
    select = re.search(rf'<select id="{select_id}".*?</select>', html, re.S)
    assert select, f'audit.html has no #{select_id} select'
    return {v for v in re.findall(r'<option value="([^"]*)">', select.group(0)) if v}


def test_scan_finds_audit_calls():
    actions, resources = _recorded()
    assert {'login', 'update'} <= actions
    assert {'session', 'user'} <= resources


def test_every_recorded_action_is_filterable():
    actions, _ = _recorded()
    missing = actions - _options('filterAction')
    assert not missing, f'actions missing from the Audit Log Action filter: {sorted(missing)}'


def test_every_recorded_resource_is_filterable():
    _, resources = _recorded()
    options = _options('filterResource')
    missing = {r for r in resources if not any(r.startswith(o) for o in options)}
    assert not missing, f'resources no Audit Log Resource option reaches: {sorted(missing)}'


def test_filters_offer_only_recorded_values():
    actions, resources = _recorded()
    stale_actions = _options('filterAction') - actions
    assert not stale_actions, f'Action filter lists actions nothing records: {sorted(stale_actions)}'
    stale_resources = {o for o in _options('filterResource') if not any(r.startswith(o) for r in resources)}
    assert not stale_resources, f'Resource filter lists resources nothing records: {sorted(stale_resources)}'
