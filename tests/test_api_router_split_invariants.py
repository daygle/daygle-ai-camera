"""Invariant test for the app/main.py router extraction (refactor #1 phase 1+).

For every ``main.<attr>`` reference in ``tests/test_api.py``, assert that
``<attr>`` is still defined on ``app.main`` after a fresh load. This is the
regression net for the hybrid pattern in ``app/api/__init__.py``: routers call
``import app.main as main`` and access globals/helpers via ``main.<attr>``,
*and* tests do the same. If a helper intended for routers gets moved out of
``app.main`` into a router file, the production router code keeps working but
the existing test references AttributeError on import.

Caught case during phase 1: ``_sound_status_reason`` was extracted into
``app/api/sound_router.py``. ``tests/test_api.py`` does
``main._sound_status_reason(...)`` in four places (lines 727, 734, 743, 750)
and would have AttributeErrored in CI on the very first import.

Implementation: stdlib ``ast`` (not regex) walks every ``Attribute`` node
whose ``value`` is ``Name('main')`` in ``tests/test_api.py`` and collects the
unique attr names, then loads ``app.main`` fresh and asserts each attr still
resolves. ``ast`` ignores comments, docstrings, and string literals by
construction, so reformatting / comment churn doesn't break the discovery.
"""

from __future__ import annotations

import ast
import sys
from importlib import import_module
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_API_PATH = REPO_ROOT / 'tests' / 'test_api.py'
APP_API_INIT_PATH = REPO_ROOT / 'app' / 'api' / '__init__.py'

# Minimal DAYGLE_CONFIG that gets ``app.main`` past its full init without spinning
# up real cameras / recordings. Mirrors ``tests/test_api.py:_load_app``'s shape.
_MINIMAL_CONFIG_TEMPLATE = """\
server:
  host: 127.0.0.1
  port: 8080
auth:
  enabled: true
  session_timeout_hours: 12
  max_login_attempts: 5
  lockout_minutes: 15
ai:
  backend: onnx
  confidence: 0.45
storage:
  data_dir: {data_dir}
  database: {database_path}
  snapshots_dir: {snapshots_dir}
  events_dir: {events_dir}
  recordings_dir: {recordings_dir}
recording:
  enabled: true
  mode: motion
  pre_event_seconds: 5
  post_event_seconds: 10
  max_clip_seconds: 60
alerts:
  rules:
    - name: Cat alert
      object: cat
      min_confidence: 0.50
      cooldown_seconds: 0
      enabled: true
"""


def _discover_main_attr_references(source: str) -> frozenset[str]:
    """Walk the AST and collect every ``main.<attr>`` attribute name in ``source``."""
    tree = ast.parse(source)
    refs: set[str] = set()
    for node in ast.walk(tree):
        # Only top-level `main.<attr>` — chained `main.x.y.z` fully resolves via
        # the outermost attribute, so we don't need to descend. We also skip
        # `Main` / `MainWindow` etc. (only the *exact* name ``main`` matches).
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == 'main'
        ):
            refs.add(node.attr)
            # Also collect on the left of an assignment target (e.g. ``main.x = ...``
            # is still a back-compat reference for the read side).
            parent_attr = getattr(node, 'ctx', None)
            _ = parent_attr  # silence type-checkers; just ensures ctx exists on every Python version
    return frozenset(refs)


def _load_app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Load ``app.main`` against a tmpdir DB. Mirrors ``tests/test_api.py:_load_app``."""
    config_path = tmp_path / 'config.yaml'
    data_dir = tmp_path / 'data'
    database_path = data_dir / 'daygle.sqlite3'
    config_text = _MINIMAL_CONFIG_TEMPLATE.format(
        data_dir=data_dir,
        database_path=database_path,
        snapshots_dir=data_dir / 'snapshots',
        events_dir=data_dir / 'events',
        recordings_dir=data_dir / 'recordings',
    )
    config_path.write_text(config_text, encoding='utf-8')
    monkeypatch.setenv('DAYGLE_CONFIG', str(config_path))
    sys.modules.pop('app.main', None)
    return import_module('app.main')


def test_all_main_attr_references_resolve_on_app_main(monkeypatch, tmp_path):
    """Every ``main.<attr>`` referenced in tests/test_api.py must resolve on app.main."""
    main = _load_app_main(tmp_path, monkeypatch)
    referenced = _discover_main_attr_references(TEST_API_PATH.read_text(encoding='utf-8'))
    missing = sorted(referenced - set(dir(main)))
    # ``dir()`` covers inherited module attrs too; ``hasattr`` would also work but
    # ``dir()`` is cheaper (no descriptor probe per attr) and equally correct for
    # module objects.
    _ = missing
    missing = sorted(attr for attr in referenced if not hasattr(main, attr))
    assert not missing, (
        f"These attrs are referenced as `main.<attr>` in tests/test_api.py but "
        f"are not defined on app.main:\n  - " + "\n  - ".join(missing) +
        "\n\nEach test does roughly: ``sys.modules.pop('app.main', None); "
        "main = importlib.import_module('app.main'); main.<attr>``. If app.main "
        "does not expose <attr>, the test AttributeErrors before its main "
        "assertion runs.\n\n"
        "How to fix (see app/api/__init__.py for the hybrid-pattern rules):\n"
        "  - If a router file is the only *production* caller of <attr>, you "
        "STILL need to keep <attr> defined on app.main because tests still "
        "reference it as `main.<attr>` there. Move the call site inside the "
        "router to use `main.<attr>` instead of importing the helper directly.\n"
        "  - If both production AND tests can move off <attr>, land the test "
        "rename as a separate change first so the docs and tests follow."
    )


def test_hybrid_pattern_rule_docstring_is_present():
    """Defense-in-depth: the hybrid-pattern rule this test enforces must remain
    documented in ``app/api/__init__.py``. If somebody deletes that docstring
    without realizing this invariant depends on it, this test fails fast.
    """
    docstring = APP_API_INIT_PATH.read_text(encoding='utf-8')
    assert 'app.main' in docstring and 'stay defined on' in docstring, (
        "app/api/__init__.py must still document the hybrid-pattern rule: "
        "globals and helpers referenced as `main.<attr>` from tests stay "
        "defined on app.main even if a router file is the only production "
        "caller. See tests/test_api_router_split_invariants.py for the test "
        "that enforces this rule."
    )
