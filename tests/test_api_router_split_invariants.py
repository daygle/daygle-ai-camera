"""Dual-invariant regression net for the hybrid-pattern router split.

Four assertions:

1. ``test_all_main_attr_references_resolve_on_app_main`` - AST-walks every
   ``main.<attr>`` reference across the split ``tests/test_api_*.py`` suites
   and asserts each
   ``<attr>`` is still defined on the freshly-loaded ``app.main`` module.
   Defends the hybrid-pattern rule documented in ``app/api/__init__.py``
   ("anything tests use as main.X must stay defined on app.main").

2. ``test_hybrid_pattern_rule_docstring_is_present`` - defense-in-depth
   check that ``app/api/__init__.py`` still documents the hybrid-pattern
   rule. The invariants in this file enforce the rule, and a stray doc
   removal would silently remove its documentation; this fails fast.

3. ``test_every_request_path_has_a_registered_route`` (NEW for Phase-2) -
   AST-walks every ``LocalClient.request("<path>", ...)`` literal across the
   split ``tests/test_api_*.py`` suites and asserts each path matches a FastAPI
   route on the loaded ``main.app`` (matched via Starlette's
   ``route.path_regex``). This is the assertion that would have caught the
   e365ec5 regression: the Phase-2 attempt used a path-agnostic splice that
   deleted every ``@app.X(...)`` decorator, so EVERY test callsite became
   unrouted at once.

4. ``test_settings_ai_router_includes_exactly_ten_endpoints`` - simple
   structural count check on the Phase-2 router file. Locks the inventory
   so future edits can't silently drop or duplicate an endpoint.

All assertions load ``app.main`` fresh against a tmpdir ``DAYGLE_CONFIG`` -
mirrors ``tests/support.py::_load_app`` so any global that resolves there
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

# Mirror tests/support.py's pattern: stick the repo root on sys.path so
# `import app.main` resolves at collection time. Without this bootstrap the
# AST-only invariant tests crash with `ModuleNotFoundError: No module named
# 'app'` because pytest doesn't auto-add cwd to sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# The former monolithic tests/test_api.py was split into tests/support.py
# (shared harness) + tests/test_api_<theme>.py (themed suites). The invariant
# walkers below scan the whole set so the main.<attr> and request-path coverage
# assertions stay enforced across the split. This file (itself matching the
# test_api_*.py glob) is excluded.
TEST_API_FILES = [
    PROJECT_ROOT / 'tests' / 'support.py',
    *sorted(
        p for p in (PROJECT_ROOT / 'tests').glob('test_api_*.py')
        if p.name != 'test_api_router_split_invariants.py'
    ),
]
APP_MAIN_PY = PROJECT_ROOT / 'app' / 'main.py'
APP_API_INIT = PROJECT_ROOT / 'app' / 'api' / '__init__.py'
APP_API_MODULES = sorted((PROJECT_ROOT / 'app' / 'api').glob('*.py'))
EXPECTED_SETTINGS_AI_REMOVE_COUNT = 10


def _discover_main_attr_references(*, source_text: str, source_path: Path) -> list[str]:
    """Every ``<attr>`` accessed via ``main.<attr>`` in ``source_text``.

    Walks the AST for Attribute nodes whose value is ``Name('main')``. Names
    accessed via dynamic expressions (e.g. ``getattr(main, name)``) are
    deliberately ignored - those can't be statically validated.
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


def _collect_api_imports_in_main(source_text: str, source_path: Path) -> dict[str, tuple[int, str, str]]:
    """Every top-level ``from app.api.X import Y[ as Z]`` in ``source_text``.

    Returns a dict mapping the effective binding name (the ``as Z`` if
    present, else ``Y``) to ``(lineno, module, original_name)``. The
    Phase-7.1 invariant consumes this to assert each binding is
    referenced somewhere - either as a bare-name in ``app/main.py`` (the
    ``include_router`` pattern), as ``main.<attr>`` *inside*
    ``app/main.py``, or as ``main.<attr>`` in the split ``tests/test_api_*.py``
    suites (test-only back-compat aliases).

    Walks module-level ``ImportFrom`` nodes only - nested from-imports
    inside function bodies are not relevant here (the hybrid pattern
    keeps all cross-module imports at module top). Filters to absolute
    imports whose module starts with ``app.api``; ignores ``from .api``
    relative imports.
    """
    tree = ast.parse(source_text, filename=str(source_path))
    out: dict[str, tuple[int, str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module is None or not node.module.startswith('app.api'):
            continue
        if node.level != 0:
            continue
        for alias in node.names:
            binding = alias.asname or alias.name
            out[binding] = (node.lineno, node.module, alias.name)
    return out


def _collect_bare_name_references(source_text: str, source_path: Path) -> set[str]:
    """Every bare ``Name`` loaded (read) in ``source_text``.

    Filters on ``ast.Load`` so import / function / assignment binding
    sites are excluded - only the actual reference sites count. The
    ``include_router`` pattern ``app.include_router(recordings_router)``
    registers as a bare ``Name`` read of ``recordings_router`` here.

    A bare ``Name`` with ``ast.Load`` context in the include_router call
    is the signal that distinguishes router-assembly from-imports (which
    have NO ``main.<attr>`` reachability from main.py itself) from
    test-only back-compat aliases (which also have NO bare-name
    reachability in main.py - they're consumed only by tests). Both pass
    the Phase-7.1 invariant as long as they fall into one of the three
    consumption pools below.
    """
    tree = ast.parse(source_text, filename=str(source_path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.add(node.id)
    return out


def _collect_test_request_paths() -> tuple[list[tuple[str, int, str]], int]:
    """Every literal path-string passed as the first positional arg of a
    ``*.request(...)`` call across the split ``tests/support.py`` +
    ``tests/test_api_<theme>.py`` suites.

    Records ``(file, lineno, path)`` for AST nodes where the first arg is a
    string ``Constant``. Variable / f-string paths (e.g.
    ``f"/api/users/{user['id']}"``) cannot be statically resolved, so the
    second return value counts the callsites we skip. The Phase-2 routes-
    coverage test surfaces that count so future readers see how much of the
    test surface the assertion actually validates.
    """
    out: list[tuple[str, int, str]] = []
    ignored = 0
    for path in TEST_API_FILES:
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(path))
        rel = str(path.relative_to(PROJECT_ROOT))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == 'request' and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        p = first.value
                        if p.startswith('/'):
                            out.append((rel, node.lineno, p))
                    else:
                        ignored += 1
    return out, ignored


def _collect_decorator_paths(source_text: str, source_path: Path) -> list[tuple[int, str]]:
    """Every ``@app.X("<path>", ...)`` / ``@router.X("<path>", ...)`` decorator.

    AST walk across the entire module - top-level and nested defs both. The
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

    Mirrors ``tests/support.py::_load_app`` shape so any global that
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
    refs = sorted({
        ref
        for path in TEST_API_FILES
        for ref in _discover_main_attr_references(
            source_text=path.read_text(encoding='utf-8'), source_path=path,
        )
    })
    app_main = _load_app_fresh(tmp_path, monkeypatch)
    missing = [r for r in refs if not hasattr(app_main, r)]
    assert not missing, (
        f"Hybrid-pattern invariant violation: tests reference main.<attr> "
        f"for {len(missing)} attrs that are NOT defined on app.main after the "
        f"Phase-2 router split: {missing}\n"
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
        into ``app.routes`` as APIRoute objects - it adds a wrapper instance
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
    for src_rel, lineno, tp in test_paths:
        base = tp.split('?', 1)[0]
        if not any(rx.match(base) for rx in registered_patterns):
            mismatches.append((src_rel, lineno, tp))

    if mismatches or unregistered_decorators:
        tests_section = '\n'.join(
            f'  {src_rel}:{ln}: LocalClient.request("{p}", ...)'
            for src_rel, ln, p in mismatches
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


def test_settings_ai_router_includes_exactly_expected_endpoints():
    """Lightweight structural check on the Phase-2 router.

    Independent of pytest's app loading (no DAYGLE_CONFIG, no sys.modules
    dance). Reads the new router file directly and counts @router.X(...)
    decorators whose first positional arg is a string starting with
    '/api/settings/ai'. The router MUST contribute exactly
    ``EXPECTED_SETTINGS_AI_REMOVE_COUNT`` endpoints - fewer and something's
    missing; more and the splice over-extracted.
    """
    router_path = PROJECT_ROOT / 'app' / 'api' / 'settings_ai_router.py'
    text = router_path.read_text(encoding='utf-8')
    paths = [p for _ln, p in _collect_decorator_paths(source_text=text, source_path=router_path)]
    settings_ai_paths = [p for p in paths if p.startswith('/api/settings/ai')]
    assert len(settings_ai_paths) == EXPECTED_SETTINGS_AI_REMOVE_COUNT, (
        f"Expected exactly {EXPECTED_SETTINGS_AI_REMOVE_COUNT} endpoints in "
        f"the Phase-2 router; found {len(settings_ai_paths)}: {settings_ai_paths}"
    )


def test_app_api_imports_in_main_are_consumed():
    """Phase-7.1 invariant. Every top-level ``from app.api.X import Y[ as Z]``
    in ``app/main.py`` must be consumed somewhere - either as a bare-name
    read in ``app/main.py`` (the ``include_router`` pattern), as
    ``main.<attr>`` *inside* ``app/main.py``, or as ``main.<attr>`` in
    ``tests/test_api.py`` (the Phase-3 back-compat-alias pattern, e.g.
    ``main.recording_detail``).

    Truly unused imports should be DROPPED to reduce module-load cost on
    every test-collection cycle. Imports consumed ONLY by tests as
    ``main.<attr>`` must STAY as back-compat aliases per rule 5 of
    ``app/api/__init__.py``.

    Failure mode this guard exists to defeat: Phase-7 once removed
    ``from app.api.recordings_router import recording_detail`` after a
    one-off orphan-audit walked only ``app/main.py`` and saw exactly one
    occurrence of ``recording_detail`` (the import itself). It missed
    that ``tests/test_api.py`` accesses the binding as
    ``main.recording_detail(recording_id)``. The resulting 3-test failure
    was caught by ``test_all_main_attr_references_resolve_on_app_main``
    AFTER the bad edit; this invariant catches the same regression class
    at audit / commit time so the bad edit never ships.

    Diagnostic shape on failure: ``file:lineno: from <module> import
    <original>[ as <alias>]`` per orphan import - so the next refactor
    sees exactly which line to either drop or convert to a named
    back-compat alias.
    """
    main_text = APP_MAIN_PY.read_text(encoding='utf-8')
    api_imports = _collect_api_imports_in_main(
        source_text=main_text, source_path=APP_MAIN_PY,
    )

    # Sanity floor: as of Phase-7.1 there are 11 from-imports in
    # app/main.py (10 router-assembly + 1 back-compat). Floor `>= 5`
    # absorbs future router consolidations (e.g. merging sound_router +
    # settings_ai_router into a single router drops the count to ~9,
    # still above the floor; deeper merges down to ~5 still pass). The
    # floor's job is to fail loudly if a future refactor accidentally
    # makes the walker go vacuous (e.g. a wrong module filter, AST
    # walking changes, or a typo in the walker). Lower it further only
    # if a legitimate consolidation has documented phase-X tally < 5.
    assert len(api_imports) >= 5, (
        f"_collect_api_imports_in_main walker found only {len(api_imports)} "
        f"app.api bindings at the top of app/main.py. Has the walker gone "
        f"vacuous, or did a Phase-N import-shape refactor require a tally "
        f"bump in the comment above?"
    )

    #    Three consumption pools. A binding passes if it lands in ANY one.
    bare_names_main = _collect_bare_name_references(
        source_text=main_text, source_path=APP_MAIN_PY,
    )
    main_attrs_main = set(_discover_main_attr_references(
        source_text=main_text, source_path=APP_MAIN_PY,
    ))

    # Pool C: `main.<attr>` reachability across all consumer-facing files
    # in `tests/` AND in `app/` (except `app/main.py`, which Pool B
    # already covers). The Phase-7 audit misuse only walked `app/main.py`
    # itself; this broadened symmetric scan tightens the consumer-net so
    # a back-compat from-import consumed by ANY sibling test file OR
    # ANY sibling app module (today or future) is correctly classified
    # as not-orphan. Both globs use recursive `rglob` so a future
    # `tests/integration/test_X.py` and a future `app/feature/ext.py`
    # join the scan for free on the same footing.
    consumer_paths: list[Path] = []
    consumer_paths.extend(sorted((PROJECT_ROOT / 'tests').rglob('*.py')))
    for app_py in sorted((PROJECT_ROOT / 'app').rglob('*.py')):
        if app_py == APP_MAIN_PY:
            continue  # Pool B already walks main.py
        consumer_paths.append(app_py)
    main_attrs_consumers: set[str] = set()
    for cp in consumer_paths:
        if not cp.is_file():
            continue
        # Let ast.parse raise loudly on malformed vendored .py -- a
        # silently-skipped file would mask a real SyntaxError regression
        # in a sibling test. Property of `_discover_main_attr_references`
        # is that it always either parses or raises; we don't catch.
        main_attrs_consumers.update(_discover_main_attr_references(
            source_text=cp.read_text(encoding='utf-8'),
            source_path=cp,
        ))

    orphans: list[tuple[int, str, str, str]] = []
    for binding, (lineno, module, original_name) in api_imports.items():
        if (
            binding in bare_names_main
            or binding in main_attrs_main
            or binding in main_attrs_consumers
        ):
            continue
        alias_tag = f" as {binding}" if binding != original_name else ""
        orphans.append((lineno, module, original_name, alias_tag))

    assert not orphans, (
        f"Phase-7.1 orphan-import invariant violation: found {len(orphans)} "
        f"unused app.api from-imports in app/main.py:\n  "
        + "\n  ".join(
            f"app/main.py:{ln}: from {m} import {n}{a}"
            for ln, m, n, a in orphans
        )
        + "\n\nTruly unused imports should be dropped (they bloat module-load "
        "cost on every test-collection cycle). Imports consumed ONLY by tests "
        "as `main.<attr>` must stay as back-compat aliases and should be "
        "placed adjacent to the originating router's `app.include_router(...)` "
        "line for discoverability (see Phase-7.1 regroup convention).\n"
        "Reference: rule 5 of app/api/__init__.py."
    )



# =============================================================================
# Catchall-import regression net + Pool A / PatchReach drift nets.
# Regression-proof for the three commits in this session:
#   - 3a28b82 (eliminate 19-file `import app.main as main` cascade),
#   - e73da3c (migrate test_camera_health.py monkeypatch sites from
#     `main_module.<X>` to `_app_state.<X>`),
#   - 967f241 (orphan-import deletion at app/main.py:887).
# =============================================================================


def _discover_orphan_catchall_imports():
    """Top-level ``import app.main`` (in any alias form) in any app/ module.

    Walks ``app/*.py`` + ``app/api/*.py`` excluding ``app/main.py`` (the
    latter legitimately imports its own sub-packages as part of the Pool A
    rebind inventory).

    Returns:
        files_walked: every file checked. The test asserts a sanity floor.
        violations: per-violation (path, lineno, verbatim import expression).
    """
    paths = []
    for sub in (PROJECT_ROOT / 'app', PROJECT_ROOT / 'app' / 'api'):
        if sub.exists():
            paths.extend(sorted(p for p in sub.glob('*.py') if p.name != '__init__.py'))
    paths = [p for p in paths if p.resolve() != APP_MAIN_PY.resolve()]
    violations = []
    for p in paths:
        tree = ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
        for node in tree.body:
            if not isinstance(node, ast.Import):
                continue
            for alias in node.names:
                if alias.name != 'app.main':
                    continue
                as_text = f' as {alias.asname}' if alias.asname else ''
                violations.append((p, node.lineno, f'import {alias.name}{as_text}'))
    return paths, violations


def test_no_app_module_has_catchall_import_app_main():
    """Catchall-import regression net (proof vs. 3a28b82).

    Zero tolerance for top-level ``import app.main`` (any alias form) in
    non-``app/main.py`` modules under ``app/`` or ``app/api/``. Catches
    the 19-file circular-import cascade that arose when sibling modules
    eagerly imported ``app.main as main`` to short-circuit the
    dual-binding dance with ``app.state``.
    """
    files_walked, violations = _discover_orphan_catchall_imports()
    # Current count: ~25 app/*.py + ~22 app/api/*.py minus app/main.py = ~46.
    # Floor of 30 catches a vacuous walker without over-fitting.
    assert len(files_walked) >= 30, (
        f"walker went suspiciously vacuous (saw {len(files_walked)} files); "
        f"refusing to silently pass on a possibly-broken glob"
    )
    assert not violations, (
        f"Catchall-import regression: found {len(violations)} top-level "
        f"`import app.main` lines in app/ modules:\n  "
        + "\n  ".join(
            f"{p.relative_to(PROJECT_ROOT)}:{ln}: {expr}"
            for p, ln, expr in violations
        )
        + "\n\nSiblings must NOT catchall `import app.main as main`. Use "
        "function-body `import app.X as _X` instead, OR reach the singleton "
        "via the registry at `app.state.<singleton>`. Circular imports cascade."
    )


def _collect_app_main_definitions():
    """All module-level names defined in ``app/main.py`` (Pool A TARGET surface).

    Every name reachable as ``app.main.<X>``. Includes:

      - ``ast.Assign``: ``X = Y`` and ``X, Y = a, b`` (tuple-unpack)
      - ``ast.AnnAssign``: ``X: <type> = Y`` (e.g. ``cameras_config: list = []``)
      - ``ast.AugAssign``: ``X += Y`` (rare but reachable)
      - ``ast.Import``: ``import M[ as N]`` -> ``N`` (or top-level ``M[0]``)
      - ``ast.ImportFrom``: ``from M import Y[ as Z]`` -> ``Y`` or ``Z``
      - ``ast.FunctionDef``/``AsyncFunctionDef``: ``def X(...):`` -> ``X``
      - ``ast.ClassDef``: ``class X(...):`` -> ``X``

    Returns a ``set[str]`` of binding names. Captures attribute access via
    ``_add`` (e.g. ``_state.database = database`` only registers its
    ``ast.Name``/``ast.Tuple`` targets, not the ``Attribute`` LHS).
    """
    tree = ast.parse(APP_MAIN_PY.read_text(encoding='utf-8'),
                     filename=str(APP_MAIN_PY))
    names: set[str] = set()

    def _add(target):
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Tuple):
            for elt in target.elts:
                _add(elt)
        # Star-targets (e.g. `a, *b = ...`) intentionally skipped: dynamic
        # binds aren't reachable as `main.<X>`.

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _add(t)
        elif isinstance(node, ast.AnnAssign):
            _add(node.target)
        elif isinstance(node, ast.AugAssign):
            _add(node.target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _collect_pool_a_consumers():
    """Every Pool A ``from app.main import X[ as Y]`` consumer site across
    ``app/*.py`` + ``app/api/*.py``.

    Returns a list of ``(binding_name, consumer_path, lineno)``. Function-body
    imports are Pool C and out-of-scope here.
    """
    out = []
    for sub in (PROJECT_ROOT / 'app', PROJECT_ROOT / 'app' / 'api'):
        if sub.exists():
            for p in sorted(sub.glob('*.py')):
                if p.name == '__init__.py':
                    continue
                if p.resolve() == APP_MAIN_PY.resolve():
                    continue
                tree = ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
                for node in tree.body:
                    if not isinstance(node, ast.ImportFrom):
                        continue
                    if node.module != 'app.main':
                        continue
                    for alias in node.names:
                        out.append((alias.asname or alias.name, p, node.lineno))
    return out


def test_every_pool_a_consumer_resolves_on_app_main():
    """Pool A consumer drift net.

    Every Pool A ``from app.main import X[ as Y]`` consumer across
    ``app/*.py`` + ``app/api/*.py`` must resolve to a name module-level
    defined in ``app/main.py``. A missing definition raises ImportError
    at collection time.

    Pool A has been fully eliminated: all routers now import directly from
    their canonical submodules. This test now verifies that zero new
    ``from app.main import`` module-level consumers are introduced -- any
    such line would be a regression back toward the Pool A pattern.
    """
    main_defs = _collect_app_main_definitions()
    consumers = _collect_pool_a_consumers()
    assert len(consumers) == 0, (
        f"Pool A regression: found {len(consumers)} new `from app.main import` "
        f"module-level consumer(s) in app/ -- Pool A has been fully eliminated; "
        f"import directly from the canonical submodule instead:\n  "
        + "\n  ".join(
            f"{p.relative_to(PROJECT_ROOT)}:{ln}: from app.main import {b}"
            for b, p, ln in consumers
        )
    )
    missing = [
        (binding, p, ln)
        for binding, p, ln in consumers
        if binding not in main_defs
    ]
    assert not missing, (
        f"Pool A consumer drift: found {len(missing)} `from app.main import` "
        f"consumers whose target is no longer module-level defined in "
        f"app/main.py:\n  "
        + "\n  ".join(
            f"{p.relative_to(PROJECT_ROOT)}:{ln}: from app.main import {b}"
            for b, p, ln in missing
        )
        + "\n\nResolution paths:\n"
        "  1. Re-export the symbol on `app.main` via a top-level rebind\n"
        "     (`from app.X import Y as Y`) so it's module-level defined.\n"
        "  2. Update the consumer to import directly from its sub-package\n"
        "     (`from app.X import Y`) if the Pool A indirection is no "
        "     longer needed.\n"
    )


def _collect_test_main_module_patches():
    """Every ``monkeypatch.setattr(main_module, '<X>', ...)`` site across
    ``tests/``.

    AST walker: matches ``Call`` nodes where
        ``func.attr == 'setattr'``,
        ``len(args) >= 2``,
        ``arg0 is Name('main_module')``,
        ``arg1 is Constant(str)``.

    Returns a list of ``(lineno, target_name, test_path)``. Dynamic
    indirection (``functools.partial``, string-name variables, ternary
    targets) correctly stays outside the walker.
    """
    out = []
    for p in sorted((PROJECT_ROOT / 'tests').glob('test_*.py')):
        try:
            tree = ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (not isinstance(node, ast.Call)
                    or not isinstance(node.func, ast.Attribute)
                    or node.func.attr != 'setattr'
                    or len(node.args) < 2):
                continue
            target, attr_node = node.args[0], node.args[1]
            if (isinstance(target, ast.Name)
                    and target.id == 'main_module'
                    and isinstance(attr_node, ast.Constant)
                    and isinstance(attr_node.value, str)):
                out.append((node.lineno, attr_node.value, p))
    return out


def test_every_main_module_patch_reaches_a_pool_a_consumer():
    """PatchReach contract net.

    Every ``monkeypatch.setattr(main_module, '<X>', ...)`` site in
    ``tests/`` must target a name module-level defined in ``app/main.py``.
    Names defined only deep in production (Pool C function-body imports
    onto module-local aliases) cannot be intercepted via a module-attribute
    patch - the mutation lands on the wrong surface and the test silently
    loses its fakes.

    The reference set is the Pool A TARGET surface (every module-level
    binding in ``app/main.py``), NOT the consumer side - a module
    attribute is reached via the target, not the consumer.
    """
    reachable = _collect_app_main_definitions()
    sites = _collect_test_main_module_patches()
    # All monkeypatch.setattr(main_module, ...) sites have been migrated to
    # their true module homes (alert_dispatch, camera_health, state, etc.) as
    # part of Pool C elimination. Floor is 0 - the walker is still exercised
    # by the unreachable-name assertion below.
    unreachable = [
        (ln, name, p) for ln, name, p in sites if name not in reachable
    ]
    assert not unreachable, (
        f"PatchReach violation: {len(unreachable)} main_module-X monkeypatch "
        f"sites target a name NOT module-level defined in app/main.py:\n  "
        + "\n  ".join(
            f"{p.relative_to(PROJECT_ROOT)}:{ln}: patch on '{name}'"
            for ln, name, p in unreachable
        )
        + "\n\nTo reach a Pool C-only name, monkeypatch on the producing "
        "module instead (e.g. `monkeypatch.setattr(app.alert_dispatch, "
        "'EmailAlertService', FakeEmail)`), OR move the binding up to "
        "module level in app/main.py via a Pool A rebind so it surfaces "
        "as `main.<X>`."
    )
