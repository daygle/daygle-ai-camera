"""Dual-invariant regression net for the hybrid-pattern router split.

Four assertions:

1. ``test_all_main_attr_references_resolve_on_app_main`` — AST-walks every
   ``main.<attr>`` reference in ``tests/test_api.py`` and asserts each
   ``<attr>`` is still defined on the freshly-loaded ``app.main`` module.
   Defends the hybrid-pattern rule documented in ``app/api/__init__.py``
   ("anything tests use as main.X must stay defined on app.main").

2. ``test_hybrid_pattern_rule_docstring_is_present`` — defense-in-depth
   check that ``app/api/__init__.py`` still documents the hybrid-pattern
   rule. The invariants in this file enforce the rule, and a stray doc
   removal would silently remove its documentation; this fails fast.

3. ``test_every_request_path_has_a_registered_route`` (NEW for Phase 2) —
   AST-walks every ``LocalClient.request("<path>", ...)`` literal in
   ``tests/test_api.py`` and asserts each path matches a registered FastAPI
   route on the loaded ``main.app`` (matched via Starlette's
   ``route.path_regex``). This is the assertion that would have caught the
   e365ec5 regression: the Phase 2 attempt used a path-agnostic splice that
   deleted every ``@app.X(...)`` decorator, so EVERY test callsite became
   unrouted at once.

4. ``test_settings_ai_router_includes_exactly_ten_endpoints`` — simple
   structural count check on the Phase 2 router file. Locks the inventory
   so future edits can't silently drop or duplicate an endpoint.

All assertions load ``app.main`` fresh against a tmpdir ``DAYGLE_CONFIG`` —
mirrors ``tests/test_api.py::_load_app`` so any global that resolves there
resolves here. ``APP_API_MODULES`` is auto-discovered via glob so future
Phase-N routers (cameras/recordings/live/etc.) join the routes-coverage
walk without manual list upkeep.
"""

from __future__ import annotations

import ast
import importlib
import logging
from pathlib import Path
import sys

import pytest

# Mirror tests/test_api.py's pattern: stick the repo root on sys.path so
# `import app.main` resolves at collection time. Without this bootstrap the
# AST-only invariant tests crash with `ModuleNotFoundError: No module named
# 'app'` because pytest doesn't auto-add cwd to sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEST_API_PY = PROJECT_ROOT / 'tests' / 'test_api.py'
APP_MAIN_PY = PROJECT_ROOT / 'app' / 'main.py'
APP_API_INIT = PROJECT_ROOT / 'app' / 'api' / '__init__.py'
APP_API_MODULES = sorted((PROJECT_ROOT / 'app' / 'api').glob('*.py'))
EXPECTED_SETTINGS_AI_REMOVE_COUNT = 10


def _discover_main_attr_references(*, source_text: str, source_path: Path) -> list[str]:
    """Every ``<attr>`` accessed via ``main.<attr>`` in ``source_text``.

    Walks the AST for Attribute nodes whose value is ``Name('main')``. Names
    accessed via dynamic expressions (e.g. ``getattr(main, name)``) are
    deliberately ignored — those can't be statically validated.
    """
    tree = ast.parse(source_text, filename=str(source_path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == 'main'
        ):
            names.add(node.attr)
    return sorted(names)


def _collect_test_request_paths() -> tuple[list[tuple[int, str]], int]:
    """Every literal path-string passed as the first positional arg of a
    ``*.request(...)`` call in ``tests/test_api.py``.

    Records ``(lineno, path)`` for AST nodes where the first arg is a string
    ``Constant``. Variable / f-string paths (e.g.
    ``f"/api/users/{user['id']}"``) cannot be statically resolved, so the
    second return value counts the callsites we skip. The Phase-2 routes-
    coverage test surfaces that count so future readers see how much of the
    test surface the assertion actually validates.
    """
    source = TEST_API_PY.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(TEST_API_PY))
    out: list[tuple[int, str]] = []
    ignored = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == 'request' and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    p = first.value
                    if p.startswith('/'):
                        out.append((node.lineno, p))
                else:
                    ignored += 1
    return out, ignored


def _collect_decorator_paths(source_text: str, source_path: Path) -> list[tuple[int, str]]:
    """Every ``@app.X("<path>", ...)`` / ``@router.X("<path>", ...)`` decorator.

    AST walk across the entire module — top-level and nested defs both. The
    ``app.X`` and ``router.X`` patterns both qualify because the latter is
    what routers in ``app/api/*.py`` use. Any decorator whose target matches
    one of those AND whose first positional arg is a string literal is
    recorded.
    """
    tree = ast.parse(source_text, filename=str(source_path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                target = dec.func.value
                is_app_or_router = (
                    (isinstance(target, ast.Name) and target.id in ('app', 'router'))
                    or (
                        isinstance(target, ast.Attribute)
                        and target.attr == 'router'
                        and isinstance(target.value, ast.Name)
                        and target.value.id == 'app'
                    )
                )
                if not is_app_or_router or not dec.args:
                    continue
                first = dec.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    out.append((dec.lineno, first.value))
    return out


def _load_app_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh ``import app.main`` against a tmpdir DAYGLE_CONFIG.

    Mirrors ``tests/test_api.py::_load_app`` shape so any global that
    resolves there also resolves here. The ``sys.modules.pop`` ensures a
    clean import even if a previous test loaded the module under a different
    config.
    """
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        'server:\n  port: 8080\nstorage:\n  data_dir: tmp\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('DAYGLE_CONFIG', str(config_path))
    import sys as _s
    for mod in list(_s.modules.keys()):
        if mod == "app" or mod.startswith("app."):
            _s.modules.pop(mod, None)
    return importlib.import_module('app.main')


def test_all_main_attr_references_resolve_on_app_main(tmp_path, monkeypatch):
    """Phase-1 invariant. Every ``main.X`` used by tests must still resolve
    on the freshly-loaded ``app.main`` module."""
    refs = _discover_main_attr_references(
        source_text=TEST_API_PY.read_text(encoding='utf-8'),
        source_path=TEST_API_PY,
    )
    app_main = _load_app_fresh(tmp_path, monkeypatch)
    missing = [r for r in refs if not hasattr(app_main, r)]
    assert not missing, (
        f"Hybrid-pattern invariant violation: tests reference main.<attr> "
        f"for {len(missing)} attrs that are NOT defined on app.main after the "
        f"Phase 2 router split: {missing}\n"
        f"Per app/api/__init__.py, anything referenced as main.X must stay "
        f"defined on app.main or be re-imported explicitly."
    )


def test_hybrid_pattern_rule_docstring_is_present():
    """Defense-in-depth: the rule docstring must still live in
    ``app/api/__init__.py`` so this test framework remains self-aware.
    """
    text = APP_API_INIT.read_text(encoding='utf-8')
    assert 'hybrid' in text.lower() and 'router' in text.lower() and 'main' in text.lower(), (
        'app/api/__init__.py must document the hybrid-pattern rule. '
        'The invariant tests in this file enforce it; if you delete the '
        'docstring without realizing that, this test fails fast.'
    )


def test_every_request_path_has_a_registered_route(tmp_path, monkeypatch, caplog):
    """Phase-2 invariant. Every literal ``LocalClient.request('<path>', ...)``
    in ``tests/test_api.py`` must match a registered FastAPI route (matched
    via Starlette's ``route.path_regex``). This is the assertion that would
    have caught the e365ec5 regression: that Phase-2 attempt's path-agnostic
    splice deleted EVERY ``@app.X(...)`` decorator and left every endpoint
    unrouted.

    Diagnostic shape: lists every unrouted callsite (``file:lineno: path``)
    in the failure message so the regression is mechanically debuggable.
    """
    caplog.set_level(logging.INFO)
    test_paths, ignored = _collect_test_request_paths()
    # Sanity floor against the AST walker going vacuous: a future refactor in
    # tests/test_api.py could rename `LocalClient.request` and silently start
    # "passing" 0/0. Fail loudly here so the regression is mechanically visible.
    assert len(test_paths) > 50, (
        f'AST walker for LocalClient.request(...) found only {len(test_paths)} '
        f'callsites - has the walker gone vacuous? Review any refactor that '
        f'renamed `LocalClient` or its `.request` method.'
    )
    app_main = _load_app_fresh(tmp_path, monkeypatch)
    # Surface how much of the test surface the AST walk can statically validate.
    # F-string / variable paths cannot be resolved. caplog (not print) so the
    # count is visible without `pytest -s`.
    if ignored > 0:
        # pytest's caplog fixture captures standard `logging` calls (after
        # set_level); it does not have its own .info() method.
        logging.info(
            'routes-coverage: validating %d literal-path callsites; '
            'skipping %d non-literal (f-string/variable) - see '
            '_collect_test_request_paths for AST-Constant intent',
            len(test_paths), ignored,
        )

    def _collect_path_regexes(routes):
        """Walk every APIRoute's ``path_regex`` reachable from ``app.routes``.

        ``app.include_router(small_router)`` does NOT flatten ``small_router.routes``
        into ``app.routes`` as APIRoute objects — it adds a wrapper instance
        (Starlette's ``_IncludedRouter`` or FastAPI 0.137+'s equivalent) that
        holds the inner routes. The storage location differs by version:

        * FastAPI 0.137+ (this codebase): ``wrapper.original_router.routes``.
          ``wrapper.routes`` is empty until the first request is served.
        * Starlette (legacy): ``wrapper.routes``.

        We try ``original_router.routes`` first, fall back to ``wrapper.routes``
        when it isn't there. The inner APIRoute's ``path_regex`` already has
        any prefix baked in, so the same ``rx.match(base)`` mechanism from the
        parent test works unchanged.

        Note: each wrapper's OWN ``path_regex`` (the prefix) is also collected,
        so a fresh app with 2 ``include_router`` calls registers ~2 extra noise
        patterns (``^/?$`` for the empty prefix). Harmless; just inflates the
        count by a small constant.
        """
        patterns = []
        for route in routes:
            if getattr(route, 'path_regex', None) is not None:
                patterns.append(route.path_regex)
            original_router = getattr(route, 'original_router', None)
            if original_router is not None and hasattr(original_router, 'routes'):
                patterns.extend(_collect_path_regexes(original_router.routes))
                continue
            # Legacy Starlette fallback.
            sub = getattr(route, 'routes', None)
            if sub and type(route).__module__.split('.')[0] in ('starlette', 'fastapi'):
                patterns.extend(_collect_path_regexes(sub))
        return patterns

    # Collect decorator paths across app/ + app/api/ first so the sanity floor
    # can compare apples-to-apples: every decorator we wrote should map to a
    # registered pattern. If ``registered_patterns`` ever falls below
    # ``decorator_paths`` we know over-deletion or framework drift struck.
    decorator_paths = [
        (src, ln, p)
        for src in APP_API_MODULES
        if src.exists()
        for ln, p in _collect_decorator_paths(
            source_text=src.read_text(encoding='utf-8'), source_path=src,
        )
    ]

    # Compile every registered route's path_regex once (recurse into include_router wrappers).
    registered_patterns = _collect_path_regexes(app_main.app.routes)
    assert len(registered_patterns) >= len(decorator_paths), (
        f'Routes walker found {len(registered_patterns)} patterns but '
        f'{len(decorator_paths)} decorator paths exist across app/api/*.py. '
        f'Possibilities: include_router() failed, the walker recursed too '
        f'shallow, or a decorator was deleted without un-registering.\n'
        f'Decorator paths:\n  ' +
        '\n  '.join(f'{src.relative_to(PROJECT_ROOT)}:{ln}: {p}'
                  for src, ln, p in decorator_paths)
    )

    # Cross-check: every decorator path across app/ + app/api/ should be
    # matched by a registered route too (defends against routers that exist
    # but are never include_router'd).
    decorator_paths = [
        (src, ln, p)
        for src in APP_API_MODULES
        if src.exists()
        for ln, p in _collect_decorator_paths(
            source_text=src.read_text(encoding='utf-8'), source_path=src,
        )
    ]
    unregistered_decorators = []
    for src, ln, p in decorator_paths:
        base = p.split('?', 1)[0]
        if not any(rx.match(base) for rx in registered_patterns):
            unregistered_decorators.append((src, ln, p))

    mismatches = []
    for lineno, tp in test_paths:
        base = tp.split('?', 1)[0]
        if not any(rx.match(base) for rx in registered_patterns):
            mismatches.append((lineno, tp))

    if mismatches or unregistered_decorators:
        tests_section = '\n'.join(
            f'  tests/test_api.py:{ln}: LocalClient.request("{p}", ...)'
            for ln, p in mismatches
        ) or '  (none)'
        decos_section = '\n'.join(
            f'  {src.relative_to(PROJECT_ROOT)}:{ln}: @...("{p}", ...)'
            for src, ln, p in unregistered_decorators
        ) or '  (none)'
        raise AssertionError(
            f"Routes-coverage invariant violation:\n\n"
            f"  Test callsites with no registered route ({len(mismatches)}):\n{tests_section}\n\n"
            f"  Decorator paths with no registered route ({len(unregistered_decorators)}):\n{decos_section}\n\n"
            f"This is the same bug-class as e365ec5 (a router-extraction step "
            f"that deleted decorators past the intended domain). Verify that "
            f"every Phase-N router is include_router'd in app.main and that "
            f"the AST splice stayed inside its target path prefix."
        )


def test_settings_ai_router_includes_exactly_ten_endpoints():
    """Lightweight structural check on the Phase 2 router.

    Independent of pytest's app loading (no DAYGLE_CONFIG, no sys.modules
    dance). Reads the new router file directly and counts @router.X(...)
    decorators whose first positional arg is a string starting with
    '/api/settings/ai'. The router MUST contribute exactly 10 endpoints —
    fewer and something's missing; more and the splice over-extracted.
    """
    router_path = PROJECT_ROOT / 'app' / 'api' / 'settings_ai_router.py'
    text = router_path.read_text(encoding='utf-8')
    paths = [p for _ln, p in _collect_decorator_paths(source_text=text, source_path=router_path)]
    settings_ai_paths = [p for p in paths if p.startswith('/api/settings/ai')]
    assert len(settings_ai_paths) == EXPECTED_SETTINGS_AI_REMOVE_COUNT, (
        f"Expected exactly {EXPECTED_SETTINGS_AI_REMOVE_COUNT} endpoints in "
        f"the Phase 2 router; found {len(settings_ai_paths)}: {settings_ai_paths}"
    )
