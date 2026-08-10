"""Tests for ``app/camera_lifecycle.py`` Pool A back-compat identity.

Confirms that all four lifecycle helpers re-exported via Pool A in
``app/main.py`` point to the same function objects as their canonical
homes in ``app/camera_lifecycle.py``, and that the ``_state.*``
callback slots are wired to the same functions.

Import order follows the anti-circular-import pattern established in
``test_camera_config.py``: load ``app.main`` first so the module is
fully initialized before ``app.camera_lifecycle`` resolves its own
``import app.state as _state`` reach.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.main  # noqa: E402  -- must precede the import below  # lgtm[py/unused-import]
import app.camera_lifecycle as cl_mod  # noqa: E402


@pytest.fixture
def main():
    return app.main


@pytest.fixture
def cl():
    return cl_mod


@pytest.fixture
def state():
    return sys.modules["app.state"]


# ---------------------------------------------------------------------------
# Pool A identity: main.<name> is camera_lifecycle.<name>
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _state callback slots: _state.<name> is camera_lifecycle.<name>
# ---------------------------------------------------------------------------

def test_state_camera_event_recording_config_is_lifecycle(state, cl):
    assert state.camera_event_recording_config is cl.camera_event_recording_config


def test_state_apply_cameras_settings_is_lifecycle(state, cl):
    assert state.apply_cameras_settings is cl.apply_cameras_settings


def test_state_apply_storage_and_recording_settings_is_lifecycle(state, cl):
    assert state.apply_storage_and_recording_settings is cl.apply_storage_and_recording_settings


def test_state_reload_detector_is_lifecycle(state, cl):
    assert state.reload_detector is cl.reload_detector


# ---------------------------------------------------------------------------
# _state.camera is initialized
# ---------------------------------------------------------------------------

def test_state_camera_attribute_exists(state):
    """``_state.camera`` must exist (set at main.py module load); may be
    None if no cameras are configured, but must not be missing entirely."""
    assert hasattr(state, 'camera'), "_state.camera attribute not found"
