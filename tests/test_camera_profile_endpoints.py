"""Regression tests for the profile-suggestion and IR-check endpoints.

Both endpoints serve the Cameras page's "Suggest Sunrise/Sunset" and
"Check IR State Now" buttons. They used to read ONLY the saved camera
configuration, so a newly added camera (or freshly typed-but-unsaved
lat/long) could not use either button until the camera was saved. They now
accept explicit per-request overrides, and an unknown ``camera_id`` with
overrides is fine.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.api import cameras_router  # noqa: E402
import app.state as state  # noqa: E402


class _FakeRequest:
    """Satisfies require_admin, which only reads request.state."""

    class _State:
        user = {"role": "admin"}

    state = _State()


@pytest.fixture(autouse=True)
def _bypass_admin(monkeypatch):
    monkeypatch.setattr(cameras_router, "require_admin", lambda request: None)


def _restore_cameras_config(original):
    state.cameras_config = original


def test_suggestion_uses_query_overrides_for_unsaved_camera():
    """An unknown camera id with explicit parameters must succeed."""
    # Sync handler (FastAPI runs it in a threadpool): call it directly.
    result = cameras_router.camera_profile_schedule_suggestion(
        "brand-new-camera",
        _FakeRequest(),
        latitude=-33.8688,
        longitude=151.2093,
        timezone="Australia/Sydney",
    )
    assert result["source"] == "solar"
    assert result["day_start"] != result["night_start"]
    assert len(result["day_start"]) == 5


def test_suggestion_overrides_win_over_saved_camera():
    """A saved camera's stored values are overridden per supplied key."""
    original = state.cameras_config
    state.cameras_config = [{
        "id": "saved-cam",
        "latitude": 51.5072,
        "longitude": -0.1276,
        "timezone": "UTC",
    }]
    try:
        # Baseline: no overrides -> the stored London config.
        baseline = cameras_router.camera_profile_schedule_suggestion(
            "saved-cam", _FakeRequest(),
        )
        assert baseline["timezone"] == "UTC"
        # Sydney coordinates override the stored London ones.
        result = cameras_router.camera_profile_schedule_suggestion(
            "saved-cam",
            _FakeRequest(),
            latitude=-33.8688,
            longitude=151.2093,
        )
        assert result["timezone"] == "UTC"
        assert result["day_start"] != baseline["day_start"]
        # Partial override: only latitude supplied, stored longitude/timezone kept.
        partial = cameras_router.camera_profile_schedule_suggestion(
            "saved-cam", _FakeRequest(), latitude=-33.8688,
        )
        assert partial["day_start"] != baseline["day_start"]
    finally:
        _restore_cameras_config(original)


def test_suggestion_for_unknown_camera_without_overrides_is_400():
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            cameras_router.camera_profile_schedule_suggestion(
                "brand-new-camera", _FakeRequest(),
            )
        )
    assert excinfo.value.status_code == 400


def test_ir_state_uses_body_overrides_for_unsaved_camera(monkeypatch):
    """An unknown camera id with a host in the body must probe that host."""
    captured = {}

    def fake_probe(host, http_port, username, password):
        captured.update(host=host, http_port=http_port, username=username, password=password)
        return "night"

    monkeypatch.setattr(cameras_router, "probe_onvif_day_night", fake_probe)

    class _FakeRequestWithBody(_FakeRequest):
        async def json(self):
            return {
                "host": "192.0.2.50",
                "http_port": 8080,
                "username": "admin",
                "password": "typed-password",
            }

    result = asyncio.run(
        cameras_router.check_camera_ir_state("brand-new-camera", _FakeRequestWithBody())
    )
    assert result["supported"] is True
    assert result["state"] == "night"
    assert captured == {
        "host": "192.0.2.50",
        "http_port": 8080,
        "username": "admin",
        "password": "typed-password",
    }


def test_ir_state_overrides_win_over_saved_camera(monkeypatch):
    """Supplied overrides take precedence; empty password falls back to saved."""
    original = state.cameras_config
    state.cameras_config = [{
        "id": "saved-ir-cam",
        "host": "192.0.2.10",
        "username": "saved-user",
        "password": "saved-password",
        "ptz": {"http_port": 8081},
    }]
    captured = {}

    def fake_probe(host, http_port, username, password):
        captured.update(host=host, http_port=http_port, username=username, password=password)
        return "day"

    monkeypatch.setattr(cameras_router, "probe_onvif_day_night", fake_probe)

    class _Body(dict):
        async def json(self):
            return dict(self)

    try:
        # Full override wins.
        result = asyncio.run(
            cameras_router.check_camera_ir_state(
                "saved-ir-cam", _Body(host="192.0.2.99", username="typed-user"),
            )
        )
        assert result["state"] == "day"
        assert captured["host"] == "192.0.2.99"
        assert captured["username"] == "typed-user"
        assert captured["password"] == "saved-password"  # empty -> saved fallback
        assert captured["http_port"] == 8081  # not overridden -> stored ptz value
    finally:
        _restore_cameras_config(original)


def test_ir_state_unknown_camera_without_host_is_400():
    class _EmptyBody:
        async def json(self):
            return {}

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(cameras_router.check_camera_ir_state("brand-new-camera", _EmptyBody()))
    assert excinfo.value.status_code == 400
