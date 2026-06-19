"""Phase-17 integration tests for ``app/config_facades.py``.

Phase 17 extracted the 7 config-facade helpers from ``app/main.py`` into
``app/config_facades.py`` using the hybrid-pattern template. Routers
reach them via ``main.<name>`` (Pool C bare-name reach), preserved by
Pool A from-import rebinds at the top of ``app/main.py`` (NOT the
bottom -- module-load callers in main.py need them earlier; see
.phase17_fix_main.py for the placement rationale).

Tests pin three contracts:

1. **Pool A back-compat identity.** The 7 Pool A rebinds in
   ``app/main.py`` MUST wire ``main.<name>`` to the SAME function
   object as ``app.config_facades.<name>``.

2. **Behavior of each facade.** Each of the 7 helpers has subtle
   ordering / fallback semantics that we exercise in-process via
   ``monkeypatch.setattr`` (NOT raw attribute assignment -- the latter
   leaks state across test invocations). Specifically: deep-copy
   isolation, hardcoded-defaults-vs-config-vs-database ordering, the
   storage ``database`` path preservation rule, the cameras-list
   normalization, the 404-on unknown id behavior in
   ``get_camera_config``.

3. **DEFAULT_LIVE_CONFIG constant.** The hardcoded defaults live as a
   module-level ``dict`` in ``app.config_facades.py`` -- must remain
   immutable across calls (effective_live_config uses
   ``copy.deepcopy(DEFAULT_LIVE_CONFIG)`` to ensure this).

Top-level preload pattern (import app.main BEFORE app.config_facades)
breaks the circular import gate at collection time. Without this,
pytest collection triggers auth_gates.py-style reverse-direction
circular import (Phase 16 lesson re-applied here for Phase 17).
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest  # noqa: E402  -- used by pytest.raises in the 404 test below

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Top-level lazy-ordered preloads to break the Phase-17 circular-import gate
# (same pattern as tests/test_auth_gates.py Phase 16). If we imported
# ``app.config_facades`` FIRST, Python's fresh-load chain would run the
# bottom-of-file rebind ``from app.config_facades import (...)`` inside
# ``app/main.py`` while ``app.config_facades`` is still mid-load (only top
# imports done, function defs pending) -> ImportError. Preloading ``app.main``
# fully first populates ``sys.modules['app.main']`` so ``app.config_facades``'s
# own ``import app.main as main`` at module top returns the cached module
# rather than triggering a recursive fresh-load chain.
import app.main  # noqa: E402  -- must precede the import below
import app.config_facades as config_facades  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Pool A back-compat identity -- ``main.<name> is config_facades.<name>``.
# ---------------------------------------------------------------------------


@pytest.fixture
def main():
    """Return the CURRENT ``app.main`` module instance.

    Earlier test files (e.g. ``tests/test_api.py::_load_app``) wipe
    ``sys.modules`` for any ``app.*`` module then re-import the app tree
    fresh per test session. The test file's module-level
    ``config_facades`` global is bound at pytest collection time and can
    therefore reference a stale module instance. Re-resolving both
    modules via ``sys.modules`` here ensures identity comparisons below
    use the SAME instances the Phase-17 rebind in ``app/main.py`` just
    wired in the most recent import cycle. Centralized as fixtures so
    the rationale lives in one comment rather than being copy-pasted
    into 7 tests.
    """
    return sys.modules["app.main"]


@pytest.fixture
def current_config_facades():
    """Return the CURRENT ``app.config_facades`` module instance. See
    the ``main`` fixture above for why we cannot rely on the test
    file's module-level ``config_facades`` global directly.
    """
    return sys.modules["app.config_facades"]


def test_main_effective_ai_config_is_config_facades_effective_ai_config(main, current_config_facades):
    assert main.effective_ai_config is current_config_facades.effective_ai_config, (
        "main.effective_ai_config is NOT the same function object as "
        "app.config_facades.effective_ai_config -- Pool A rebind wire broke"
    )


def test_main_effective_recording_config_is_config_facades_recording_config(main, current_config_facades):
    assert main.effective_recording_config is current_config_facades.effective_recording_config


def test_main_effective_live_config_is_config_facades_live_config(main, current_config_facades):
    assert main.effective_live_config is current_config_facades.effective_live_config


def test_main_effective_storage_config_is_config_facades_storage_config(main, current_config_facades):
    assert main.effective_storage_config is current_config_facades.effective_storage_config


def test_main_effective_auth_config_is_config_facades_auth_config(main, current_config_facades):
    assert main.effective_auth_config is current_config_facades.effective_auth_config


def test_main_effective_cameras_config_is_config_facades_cameras_config(main, current_config_facades):
    assert main.effective_cameras_config is current_config_facades.effective_cameras_config


def test_main_get_camera_config_is_config_facades_get_camera_config(main, current_config_facades):
    assert main.get_camera_config is current_config_facades.get_camera_config


# ---------------------------------------------------------------------------
# 2. DEFAULT_LIVE_CONFIG constant guard -- mutating it would leak across calls.
# ---------------------------------------------------------------------------


def test_default_live_config_is_a_module_constant_dict():
    assert isinstance(config_facades.DEFAULT_LIVE_CONFIG, dict)
    expected_keys = {
        'snapshot_refresh_ms',
        'detection_interval_seconds',
        'motion_pixel_threshold',
        'background_detection_enabled',
    }
    assert expected_keys <= set(config_facades.DEFAULT_LIVE_CONFIG.keys())


def test_default_live_config_calls_do_not_mutate_constant():
    """After calling ``effective_live_config`` multiple times, the
    DEFAULT_LIVE_CONFIG module constant must be unchanged -- the
    implementation uses ``copy.deepcopy(DEFAULT_LIVE_CONFIG)`` so the
    returned dict is a fresh deep copy each call.
    """
    snapshot = copy.deepcopy(config_facades.DEFAULT_LIVE_CONFIG)

    result1 = config_facades.effective_live_config()
    result2 = config_facades.effective_live_config()

    assert result1 == snapshot
    assert result2 == snapshot
    assert config_facades.DEFAULT_LIVE_CONFIG == snapshot, (
        "DEFAULT_LIVE_CONFIG was mutated by an effective_live_config call -- "
        "the implementation must use copy.deepcopy(DEFAULT_LIVE_CONFIG) "
        "to deep-copy the defaults on every call"
    )


# ---------------------------------------------------------------------------
# 3. Behavior of each facade (monkeypatch on main.<attr> dependencies).
# ---------------------------------------------------------------------------


def test_effective_ai_config_merges_overrides_via_deepcopy(monkeypatch):
    """``effective_ai_config`` deep-copies ``config['ai']`` first, then
    layers ``database.get_setting('ai')`` on top -- mutating the returned
    dict must NOT leak into the source.
    """
    import app.main as main
    import app.config_facades as cf

    original_config_ai = {'backend': 'onnx', 'confidence': 0.45}
    override_db = {'ai': {'confidence': 0.99}}
    monkeypatch.setattr(main, 'config', {'ai': original_config_ai})
    monkeypatch.setattr(main, 'database', _FakeDb(override_db))

    result = cf.effective_ai_config()
    assert result['backend'] == 'onnx'
    assert result['confidence'] == 0.99, "database override should win"

    # Mutate the result; the source dict should be unchanged thanks to deepcopy.
    result['confidence'] = 0.0
    assert original_config_ai == {'backend': 'onnx', 'confidence': 0.45}


def test_effective_storage_config_preserves_database_path_from_source(monkeypatch):
    """The on-disk DB path set at startup must NOT be hot-reloadable.
    If a database override contains a different ``database`` value, the
    returned config must STILL carry the source ``database`` path.
    """
    import app.main as main
    import app.config_facades as cf

    source_storage = {
        'database': '/data/store.sqlite3',
        'data_dir': '/data/main',
        'snapshots_dir': '/data/snapshots',
    }
    override_storage = {
        'database': '/some/other/path.sqlite3',  # attempted overwrite
        'retention_days': 30,
    }
    monkeypatch.setattr(main, 'config', {'storage': source_storage})
    monkeypatch.setattr(main, 'database', _FakeDb({'storage': override_storage}))

    result = cf.effective_storage_config()
    assert result['database'] == '/data/store.sqlite3', (
        "effective_storage_config must preserve the source database "
        "path even when the override specifies a different value"
    )
    assert result['data_dir'] == '/data/main'
    assert result['snapshots_dir'] == '/data/snapshots'
    # Non-database fields from the override DO apply.
    assert result['retention_days'] == 30


def test_effective_live_config_layered_order_defaults_then_config_then_db(monkeypatch):
    """``effective_live_config`` layers: DEFAULT_LIVE_CONFIG ->
    config['live'] -> database.get_setting('live'), each layer
    overriding any keys it specifies.
    """
    import app.main as main
    import app.config_facades as cf

    # Empty config + no override -> exactly DEFAULT_LIVE_CONFIG
    monkeypatch.setattr(main, 'config', {})
    monkeypatch.setattr(main, 'database', _FakeDb({}))
    only_defaults = cf.effective_live_config()
    assert only_defaults == cf.DEFAULT_LIVE_CONFIG

    # Layered: config['live'] provides detection_interval_seconds, database
    # override provides motion_pixel_threshold; DEFAULT carries background_detection_enabled.
    monkeypatch.setattr(main, 'config', {'live': {'detection_interval_seconds': 0.55}})
    monkeypatch.setattr(main, 'database', _FakeDb({'live': {'motion_pixel_threshold': 99}}))
    layered = cf.effective_live_config()
    assert layered['detection_interval_seconds'] == 0.55
    assert layered['motion_pixel_threshold'] == 99
    assert layered['background_detection_enabled'] is True


def test_effective_cameras_config_normalizes_via_main_helper(monkeypatch):
    """When a database override is present, ``effective_cameras_config``
    calls ``main.normalize_camera_settings(camera, index)`` for each
    entry -- verifying the cross-module Pool C reach.
    """
    import app.main as main
    import app.config_facades as cf

    captured: list[tuple] = []

    def fake_normalize(camera, index):
        captured.append((camera, index))
        return {'id': f'normalized-{index}', 'source': camera}

    monkeypatch.setattr(main, 'database', _FakeDb({'cameras': [{'raw': 1}, {'raw': 2}]}))
    monkeypatch.setattr(main, 'normalize_camera_settings', fake_normalize)

    result = cf.effective_cameras_config()
    assert [c['id'] for c in result] == ['normalized-1', 'normalized-2']
    assert [cam['raw'] for cam, _ in captured] == [1, 2]
    assert [idx for _, idx in captured] == [1, 2], (
        "1-based index passed in enumerate(start=1) order"
    )


def test_effective_cameras_config_empty_when_no_override(monkeypatch):
    """If the database has no 'cameras' override, return []. Cameras are
    managed via the mutating API endpoints, not the on-disk config.
    """
    import app.main as main
    import app.config_facades as cf

    monkeypatch.setattr(main, 'database', _FakeDb({'cameras': None}))
    assert cf.effective_cameras_config() == []

    monkeypatch.setattr(main, 'database', _FakeDb({'cameras': []}))
    assert cf.effective_cameras_config() == []

    # Non-list override (defensive) yields empty result too.
    monkeypatch.setattr(main, 'database', _FakeDb({'cameras': {'unexpected': 'object'}}))
    assert cf.effective_cameras_config() == []


def test_get_camera_config_raises_404_on_missing_id_when_runtime_list_non_empty(monkeypatch):
    """When ``main.cameras_config`` is non-empty AND a camera_id is
    requested that doesn't match any entry, raise HTTPException(404,
    'Camera not found').
    """
    import app.main as main
    import app.config_facades as cf
    from fastapi import HTTPException

    monkeypatch.setattr(main, 'cameras_config', [{'id': 'front', 'name': 'Front'}])
    monkeypatch.setattr(main, 'camera_config', {'id': 'fallback', 'name': 'Fallback'})

    with pytest.raises(HTTPException) as exc_info:
        cf.get_camera_config('unknown-camera')
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == 'Camera not found'


def test_get_camera_config_returns_first_when_no_id_and_runtime_list_non_empty(monkeypatch):
    """When ``main.cameras_config`` is non-empty and ``camera_id`` is
    None, return ``cameras_config[0]`` (the legacy "first camera" behaviour).
    """
    import app.main as main
    import app.config_facades as cf

    monkeypatch.setattr(main, 'cameras_config', [
        {'id': 'first', 'name': 'First'},
        {'id': 'second', 'name': 'Second'},
    ])
    monkeypatch.setattr(main, 'camera_config', {'id': 'fallback'})

    assert cf.get_camera_config(None) == {'id': 'first', 'name': 'First'}


def test_get_camera_config_falls_back_to_camera_config_when_runtime_list_empty(monkeypatch):
    """When ``main.cameras_config`` is empty, ``get_camera_config``
    returns the singular ``main.camera_config`` fallback regardless of
    camera_id (single-camera legacy setups).
    """
    import app.main as main
    import app.config_facades as cf

    monkeypatch.setattr(main, 'cameras_config', [])
    monkeypatch.setattr(main, 'camera_config', {'id': 'legacy-single', 'name': 'Legacy'})

    # No id -> camera_config returned
    assert cf.get_camera_config(None) == {'id': 'legacy-single', 'name': 'Legacy'}
    # An id is passed but can't be resolved -> still falls back to camera_config
    # (the empty-list short-circuit precedes the "raise 404" branch in the source)
    assert cf.get_camera_config('any-id') == {'id': 'legacy-single', 'name': 'Legacy'}


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeDb:
    """Minimal double for ``app.main.database`` exposing only ``get_setting``.
    Returns None for unknown keys (matches the real DB's behavior).
    """
    def __init__(self, settings: dict) -> None:
        self._settings = settings

    def get_setting(self, name: str):
        return self._settings.get(name)
