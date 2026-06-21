"""Tests for ``app/camera_id.py`` and Pool A back-compat re-exports.

Three contracts:

1. ``main.normalize_camera_id is camera_config.normalize_camera_id`` — both
   Pool A re-exports point to the same ``camera_id.normalize_camera_id``
   function object (identity via sys.modules to survive test-isolation wipes).

2. ``zone_schema.normalize_camera_id is camera_id.normalize_camera_id`` —
   zone_schema's import is also the same object (it was previously imported
   from camera_config, which caused the mutual cycle; now it comes from
   camera_id directly).

3. Behavior of ``normalize_camera_id`` is exercised here as the canonical
   test; ``test_camera_config.py`` also exercises it through the ``cc``
   fixture but does not own the canonical assertions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load app.main first to fully populate sys.modules before other app.*
# modules are imported. This avoids the circular-import gate that fires when
# a partial-load chain encounters camera_config → zone_schema → camera_id
# before zone_schema or camera_config is fully initialized.
import app.main  # noqa: E402  -- must precede the imports below
import app.camera_id as camera_id  # noqa: E402


@pytest.fixture
def main():
    return sys.modules["app.main"]


@pytest.fixture
def ci():
    return sys.modules["app.camera_id"]


@pytest.fixture
def cc():
    return sys.modules["app.camera_config"]


@pytest.fixture
def zs():
    return sys.modules["app.zone_schema"]


# ---------------------------------------------------------------------------
# 1. Pool A identity checks
# ---------------------------------------------------------------------------

def test_main_normalize_camera_id_is_camera_id_normalize_camera_id(main, ci):
    assert main.normalize_camera_id is ci.normalize_camera_id, (
        "main.normalize_camera_id does not point to camera_id.normalize_camera_id "
        "-- Pool A rebind broke"
    )


def test_camera_config_normalize_camera_id_is_camera_id_normalize_camera_id(cc, ci):
    assert cc.normalize_camera_id is ci.normalize_camera_id, (
        "camera_config.normalize_camera_id does not re-export camera_id.normalize_camera_id"
    )


def test_zone_schema_normalize_camera_id_is_camera_id_normalize_camera_id(zs, ci):
    assert zs.normalize_camera_id is ci.normalize_camera_id, (
        "zone_schema.normalize_camera_id does not come from camera_id -- cycle broken wrong"
    )


# ---------------------------------------------------------------------------
# 2. Behavior
# ---------------------------------------------------------------------------

def test_normalize_camera_id_sanitizes_spaces_and_slashes(ci):
    assert ci.normalize_camera_id('  Hello World  ') == 'hello-world'
    assert ci.normalize_camera_id('front/cam 1') == 'front-cam-1'


def test_normalize_camera_id_empty_returns_fallback(ci):
    assert ci.normalize_camera_id('!!!') == 'camera-1'
    assert ci.normalize_camera_id('', fallback='yard') == 'yard'
    assert ci.normalize_camera_id(None, fallback='default') == 'default'


def test_normalize_camera_id_preserves_underscore_and_dash(ci):
    assert ci.normalize_camera_id('cam-1_OK') == 'cam-1_ok'
    assert ci.normalize_camera_id('Cam_2') == 'cam_2'
