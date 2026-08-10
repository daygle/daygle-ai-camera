"""Phase-18 integration tests for ``app/camera_config.py``.

Phase-18 extracted the 4 camera-config helpers (``normalize_camera_id``,
``normalize_camera_settings``, ``_migrate_camera_id``, ``_redact_camera``)
from ``app/main.py`` into ``app/camera_config.py`` using the hybrid-pattern
template (same as Phase-16 ``app/auth_gates.py`` and Phase-17
``app/config_facades.py``). Routers reach them via ``main.<name>``,
preserved by a top-of-file Pool A from-import rebind in ``app/main.py``
(NOT the bottom -- module-load code in main.py does not call these
eagerly, but sibling helpers still on main.py reference them as bare
names; the top rebind wires ``main.<name>`` before any sibling body
evaluates).

Tests pin three contracts:

1. **Pool A back-compat identity.** The 4 Pool A rebinds in
   ``app/main.py`` MUST wire ``main.<name>`` to the SAME function object
   as ``app.camera_config.<name>``. Re-resolved via ``sys.modules`` to
   defeat the ``tests/support.py::_load_app`` sys-modules-wipe leak
   that surfaces with stale collection-time module globals.

2. **Behavior of each facade.** Each helper has subtle ordering /
   fallback semantics exercised here:

   - ``normalize_camera_id``: regex sanitisation + ``fallback`` default.
   - ``normalize_camera_settings``: layers detection defaults + zones +
     detection.record_on_detect + the 8 cross-module ``main.<attr>``
     dependencies (smoke-tested via the cooperative rebind wiring;
     changes to sibling helpers (e.g. ``camera_default_name``) would
     surface in their own dedicated tests).
   - ``_migrate_camera_id``: rename across recording + history + motion
     locks; tested as a unit by monkeypatching ``main`` so it's hermetic
     on its own deps (no Redis / filesystem required).
   - ``_redact_camera``: strips ``password`` and adds ``has_password``.

3. **Top-level preload pattern.** We ``import app.main`` BEFORE
   ``import app.camera_config`` at module top -- same as test_auth_gates /
   test_config_facades. Without this, pytest collection triggers the
   circular-import gate at ``app.camera_config`` load time (its top has
   ``import app.main as main`` for the Pool C reach sites).
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest  # noqa: E402  -- used by monkeypatch + fixtures below

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Top-level lazy-ordered preloads to break the Phase-18 circular-import gate
# (same pattern as tests/test_auth_gates.py Phase-16 + test_config_facades.py
# Phase-17). If we imported ``app.camera_config`` FIRST, Python's fresh-load
# chain would run the top-of-file rebind ``from app.camera_config import (...)``
# inside ``app/main.py`` while ``app.camera_config`` is still mid-load (only
# top imports done, function defs pending) -> ``cannot import name
# 'normalize_camera_id' from partially initialized module
# 'app.camera_config'`` ImportError. Preloading ``app.main`` fully first
# populates ``sys.modules['app.main']`` so ``app.camera_config``'s own
# ``import app.main as main`` returns the cached module rather than
# triggering a recursive fresh-load chain.
import app.main  # noqa: E402  -- must precede the import below  # lgtm[py/unused-import]
import app.camera_config  # noqa: E402  # lgtm[py/unused-import]


# ---------------------------------------------------------------------------
# 1. Pool A back-compat identity -- ``main.<name> is camera_config.<name>``.
#    Re-resolve via sys.modules per Phase-17 lesson (defeats the
#    tests/support.py::_load_app() sys-modules-wipe state leak).
# ---------------------------------------------------------------------------


@pytest.fixture
def main():
    """Return the CURRENT ``app.main`` module instance. See the module
    docstring for why we cannot rely on the test file's module-level
    globals directly. Centralised as fixtures so the rationale lives in
    one comment rather than copy-pasted into 4 tests."""
    return sys.modules["app.main"]


@pytest.fixture
def current_camera_config():
    """Return the CURRENT ``app.camera_config`` module instance. See
    the ``main`` fixture above for the leak rationale.
    """
    return sys.modules["app.camera_config"]


@pytest.fixture
def cc():
    """Convenience alias for ``current_camera_config`` -- used by the
    behavior tests below to call ``cc.normalize_camera_id(...)`` etc.
    without ``import app.camera_config as cc`` boilerplate."""
    return sys.modules["app.camera_config"]


# ---------------------------------------------------------------------------
# 2. Behavior of each facade (hermetic; uses monkeypatch on main.<attr>).
# ---------------------------------------------------------------------------


class _LockStub(contextlib.AbstractContextManager):
    """Tracks lock acquire / release so tests can confirm serialization."""

    def __init__(self, name: str, log: list) -> None:
        super().__init__()
        self._name = name
        self._log = log

    def __enter__(self):
        self._log.append(f'acquire:{self._name}')
        return self

    def __exit__(self, exc_type, exc, tb):
        self._log.append(f'release:{self._name}')
        return False


class _LoggerStub:
    """Captures ``warning(...)`` calls into a list for assertion."""

    def __init__(self, log: list) -> None:
        self._log = log

    def warning(self, msg, *args, **kwargs):
        self._log.append(f'warn:{msg}')  # noqa: PERF001  -- test-side stub


def _stub_main_with_camera_recording_renamer(monkeypatch):
    """Install a hermetic stand-in for the attributes that
    ``_migrate_camera_id`` reaches: ``RecordingService`` (on cc module),
    ``live_detection_history``, ``live_detection_history_lock``,
    ``_frame_motion_prev``, ``_frame_motion_lock``, ``recording_service``
    (on _state), and ``logger`` (on cc module).

    Returns ``(_state, lock_log)`` so callers can read back side-effects
    (rename ops, lock contention, log calls) post-call.
    """
    import app.state as _state
    import app.camera_config as cc

    class _RecordingServiceStub:
        def __init__(self) -> None:
            self.prebuffer_dir = _FakePath('/virtual/prebuffer')
            self.frames_dir = _FakePath('/virtual/frames')
            self.audio_dir = _FakePath('/virtual/audio')

        @staticmethod
        def _camera_key(camera_id: str) -> str:
            return f'cam-{camera_id}'

    log: list[str] = []
    monkeypatch.setattr(cc, 'RecordingService', _RecordingServiceStub)
    monkeypatch.setattr(_state, 'live_detection_history_lock', _LockStub('history', log))
    monkeypatch.setattr(_state, 'live_detection_history', {'old': ['a', 'b']})
    monkeypatch.setattr(_state, '_frame_motion_lock', _LockStub('motion', log))
    monkeypatch.setattr(_state, '_frame_motion_prev', {'old': {'m': 1}})
    monkeypatch.setattr(_state, 'recording_service', _RecordingServiceStub())
    monkeypatch.setattr(cc, 'logger', _LoggerStub(log))
    return _state, log


class _FakePath:
    """Minimal stand-in for ``pathlib.Path`` supporting the only ops
    ``_migrate_camera_id`` uses: ``__truediv__`` + ``exists()``."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __truediv__(self, key: str):
        return _FakePath(f'{self._label}/{key}')

    def exists(self) -> bool:
        return False


# -- normalize_camera_id -----------------------------------------------------

def test_normalize_camera_id_sanitizes_unsafe_characters(cc):
    """Unsafe characters (spaces, slashes, dots, ...) collapse to ``-``,
    trailing/leading dashes are stripped, and an empty result returns
    the ``fallback``."""
    assert cc.normalize_camera_id('  Hello World  ') == 'hello-world'
    assert cc.normalize_camera_id('front/cam 1') == 'front-cam-1'
    assert cc.normalize_camera_id('!!!') == 'camera-1'  # empty after strip
    assert cc.normalize_camera_id('', fallback='front-yard') == 'front-yard'
    assert cc.normalize_camera_id(None, fallback='default') == 'default'


def test_normalize_camera_id_preserves_underscore_and_dash(cc):
    assert cc.normalize_camera_id('cam-1_OK') == 'cam-1_ok'
    assert cc.normalize_camera_id('Cam_2') == 'cam_2'


# -- normalize_camera_settings ---------------------------------------------

def test_normalize_camera_settings_layers_defaults_id_name_backend(cc):
    """When ``settings`` is empty/None, ``normalize_camera_settings``
    layers in defaults for id (``camera-1``), name (``Camera 1``),
    backend (``onvif``), width (1280), height (720), fps (None for
    auto-detect), stale_frame_grabs (None). Sibling helpers on main.py are reached
    at call time and return their own sensible defaults, so we don't
    monkeypatch them away -- this is the closest "happy path" unit
    test of the cross-module Pool C wiring. Sibling-helper changes
    surface in their own dedicated test files; this test pins the
    cooperative end-to-end shape only.
    """
    out = cc.normalize_camera_settings(None, index=2)
    assert out['id'] == 'camera-2'
    assert out['name'] == 'Camera 2'
    assert out['backend'] == 'onvif'
    assert out['width'] == 1280
    assert out['height'] == 720
    assert out['fps'] is None
    assert out['stale_frame_grabs'] is None
    # detection block should also have been normalized, and the
    # ``default_camera_detection_settings()`` helper from main fills
    # the defaults; we just verify object_detection_enabled is True.
    assert out['detection']['object_detection_enabled'] is True


def test_normalize_camera_settings_propagates_user_id_and_coerces_numeric(cc):
    """When ``settings`` carries user-provided id / width / height,
    normalize_camera_settings uses them (after id-sanitisation) and
    coerces numeric fields via ``int(...)`` so the dataset has
    consistent types."""
    out = cc.normalize_camera_settings(
        {'id': 'NEW ID!!', 'width': '1920', 'height': '1080', 'fps': '30'},
        index=1,
    )
    assert out['id'] == 'new-id'
    assert out['width'] == 1920
    assert out['height'] == 1080
    assert out['fps'] == 30


def test_normalize_camera_settings_coerces_stale_frame_grabs(cc):
    """``stale_frame_grabs`` is preserved (coerced to int) when set,
    otherwise stays ``None`` -- exercising the conditional coercion
    branch."""
    assert cc.normalize_camera_settings({'stale_frame_grabs': '7'})['stale_frame_grabs'] == 7
    assert cc.normalize_camera_settings({})['stale_frame_grabs'] is None


# -- _redact_camera --------------------------------------------------------

def test_redact_camera_strips_password_and_adds_has_password_flag(cc):
    """Critical security test: ``_redact_camera`` must NEVER return raw
    ``password`` values back to the API caller, and must instead add
    a ``has_password`` boolean indicating credential presence."""
    out = cc._redact_camera(
        {'id': 'front', 'name': 'Front', 'username': 'admin', 'password': 'super-secret'}
    )
    assert 'password' not in out
    assert out['has_password'] is True
    assert out['id'] == 'front'
    assert out['username'] == 'admin'


def test_redact_camera_reports_has_password_false_when_no_password(cc):
    out = cc._redact_camera({'id': 'front', 'username': 'admin'})
    assert out['has_password'] is False
    assert 'password' not in out


# -- _migrate_camera_id ----------------------------------------------------

def test_migrate_camera_id_renames_in_memory_state_across_both_locks(monkeypatch, cc):
    """``_migrate_camera_id`` pops the old id from both
    ``live_detection_history`` and ``_frame_motion_prev`` under their
    respective locks, and rebinds the value under the new id.
    """
    state, lock_log = _stub_main_with_camera_recording_renamer(monkeypatch)

    cc._migrate_camera_id('old', 'new')

    assert 'old' not in state.live_detection_history
    assert state.live_detection_history['new'] == ['a', 'b']
    assert 'old' not in state._frame_motion_prev
    assert state._frame_motion_prev['new'] == {'m': 1}
    # Both locks were taken + released in order.
    assert lock_log == ['acquire:history', 'release:history', 'acquire:motion', 'release:motion']


def test_migrate_camera_id_is_noop_when_old_id_absent_from_state(monkeypatch, cc):
    """If neither in-memory dict has the old id, ``_migrate_camera_id``
    acquires both locks but leaves the dicts untouched."""
    state, lock_log = _stub_main_with_camera_recording_renamer(monkeypatch)
    state.live_detection_history.clear()
    state._frame_motion_prev.clear()

    cc._migrate_camera_id('ghost', 'new')

    assert lock_log == ['acquire:history', 'release:history', 'acquire:motion', 'release:motion']
    assert state.live_detection_history == {}
    assert state._frame_motion_prev == {}


def test_migrate_camera_id_handles_recording_service_none(monkeypatch, cc):
    """When ``_state.recording_service`` is ``None`` (e.g. before bring-up),
    the in-memory state migration still runs and the renaming path
    skips cleanly without raising."""
    state, _lock_log = _stub_main_with_camera_recording_renamer(monkeypatch)
    monkeypatch.setattr(state, 'recording_service', None)

    cc._migrate_camera_id('old', 'new')

    assert state.live_detection_history['new'] == ['a', 'b']
    assert state._frame_motion_prev['new'] == {'m': 1}
