"""Regression tests for the Cameras page solar schedule suggestion endpoint."""

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


def test_suggestion_uses_query_overrides_for_unsaved_camera():
    """An unknown camera id with explicit parameters must succeed."""
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
        baseline = cameras_router.camera_profile_schedule_suggestion(
            "saved-cam", _FakeRequest(),
        )
        assert baseline["timezone"] == "UTC"
        result = cameras_router.camera_profile_schedule_suggestion(
            "saved-cam",
            _FakeRequest(),
            latitude=-33.8688,
            longitude=151.2093,
        )
        assert result["timezone"] == "UTC"
        assert result["day_start"] != baseline["day_start"]
        partial = cameras_router.camera_profile_schedule_suggestion(
            "saved-cam", _FakeRequest(), latitude=-33.8688,
        )
        assert partial["day_start"] != baseline["day_start"]
    finally:
        state.cameras_config = original


def test_suggestion_for_unknown_camera_without_overrides_is_400():
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            cameras_router.camera_profile_schedule_suggestion(
                "brand-new-camera", _FakeRequest(),
            )
        )
    assert excinfo.value.status_code == 400
