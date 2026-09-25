"""Settings-boundary integration tests for the detector confidence floor.

``app.alert_dispatch.compute_minimum_rule_confidence`` caches the lowest
``min_confidence`` across a camera's enabled object rules, because the live path
calls it on every detection frame (~4 Hz per camera). Caching a value derived
from operator-editable settings is only safe if EVERY writer that can change
those settings invalidates it.

The unit tests in ``tests/test_live_alert_audit_fixes.py`` mutate the in-memory
settings dict directly, which exercises the per-camera settings SIGNATURE. They
cannot exercise the other path: settings arriving through persistence and reload
produce a NEW dict each read, and the global (no-camera) form has no settings
argument to hash at all. This file closes that gap at the real writer boundary:

1. a single-camera API save that lowers then raises a rule floor;
2. a bulk (list) API save;
3. a Day/Night PROFILE SWITCH, which is the sneakiest case because it changes
   rule floors without the operator touching a rule;
4. a zone DISABLE through the API;
5. a full persistence-and-reload round trip, so the read path sees freshly
   constructed settings dicts rather than the mutated originals.

The database double deliberately implements ``_settings_cache_gen`` so
``app.config_facades`` uses its real caching path, rather than the
always-rebuild path that plain mutation-based doubles fall into.
"""
from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.alert_dispatch as _ad  # noqa: E402
import app.state as _state  # noqa: E402
from app.api import cameras_router  # noqa: E402
from app.config_facades import effective_cameras_config  # noqa: E402
from app.recording_settings import apply_active_camera_detection_profile  # noqa: E402


class _FakeRequest:
    """Minimal request stand-in for the camera-settings routes.

    ``state.user`` carries a real id/username because the routers write an audit
    row on every save and ``write_audit_log`` reads both fields. ``client`` and
    ``headers`` are present so the IP resolver takes its normal path.
    """

    class _State:
        user = {'id': 1, 'username': 'admin', 'role': 'admin'}

    state = _State()
    client = None
    headers: dict = {}

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class _FakeDB:
    """Database double with the real settings-generation attribute.

    ``app.runtime_config.cached_snapshot`` keys its normalized-config cache on
    ``_settings_cache_gen``. A double WITHOUT that attribute deliberately
    bypasses the cache, which would hide exactly the staleness this file is
    written to catch, so it is present and bumped on every write.
    """

    def __init__(self) -> None:
        self._settings: dict = {}
        self._settings_cache_gen = 0
        self.audit_rows: list[dict] = []

    def get_setting(self, key: str):
        return copy.deepcopy(self._settings.get(key))

    def set_setting(self, key: str, value, _now=None) -> None:
        self._settings[key] = copy.deepcopy(value)
        self._settings_cache_gen += 1

    def add_audit_log(self, **row) -> None:
        self.audit_rows.append(row)


def _zone(rule_confidence: float, *, enabled: bool = True) -> dict:
    return {
        'id': 'driveway',
        'name': 'Driveway',
        'x': 0.0,
        'y': 0.0,
        'width': 1.0,
        'height': 1.0,
        'enabled': enabled,
        'monitor_objects': True,
        'object_rules': [
            {
                'label': 'person',
                'min_confidence': rule_confidence,
                'enabled': True,
                'record_on_detect': True,
            },
        ],
    }


def _camera_payload(rule_confidence: float, **extra) -> dict:
    return {
        'id': 'cam-a',
        'name': 'Front Door',
        'backend': 'rtsp',
        'stream_url': 'rtsp://example/stream',
        'detection': {'object_detection_enabled': True, 'zones': [_zone(rule_confidence)]},
        **extra,
    }


@pytest.fixture
def wired(monkeypatch):
    """Wire the API boundary to an in-memory database and clear all caches.

    ``apply_cameras_settings`` is used for REAL (it is the function whose
    invalidation call this file exists to cover); only its heavyweight tail --
    building camera instances and reconfiguring sound -- is stubbed, so the
    settings-publication and cache-invalidation half runs unmodified.
    """
    from app import camera_lifecycle

    db = _FakeDB()
    monkeypatch.setattr(_state, 'database', db)
    monkeypatch.setattr(cameras_router, 'require_admin', lambda request: None)
    monkeypatch.setattr(
        _ad, 'effective_ai_config', lambda: {'confidence': 0.45, 'backend': 'onnx'},
    )
    monkeypatch.setattr(camera_lifecycle, 'create_camera_instances', lambda _settings: {})
    monkeypatch.setattr(camera_lifecycle, 'apply_sound_settings', lambda: None)
    monkeypatch.setattr(_state, 'camera_instances', {})
    _ad.invalidate_min_rule_confidence_cache()
    yield db
    _ad.invalidate_min_rule_confidence_cache()
    monkeypatch.setattr(_state, 'database', None)


def _apply_publish(settings_list):
    """The production settings publisher, injected where the router expects it."""
    from app import camera_lifecycle
    camera_lifecycle.apply_cameras_settings(settings_list)


def _save_single(payload: dict) -> dict:
    return asyncio.run(
        cameras_router.update_camera(
            'cam-a', _FakeRequest(payload), db=_state.database, apply_cameras_settings=_apply_publish,
        )
    )


def _floor_for_saved_camera() -> float:
    """Read the floor the way the live path does: from a RELOADED settings dict.

    ``effective_cameras_config`` re-normalizes from persistence, so this returns
    a freshly built dict on every call -- the production shape, not the mutated
    original the unit tests reuse.
    """
    cameras = effective_cameras_config()
    assert cameras, 'camera config must reload from persistence'
    return _ad.compute_minimum_rule_confidence(camera_settings=cameras[0])


def test_api_rule_edit_changes_floor_through_persistence(wired):
    """A single-camera PUT lowers the floor, and a later raise restores it.

    Both reads go through persistence and reload, so the settings dict the hot
    path receives is a different object each time -- exactly the condition the
    in-memory unit tests cannot reproduce.
    """
    _save_single(_camera_payload(0.20))
    assert _floor_for_saved_camera() == pytest.approx(0.20)

    _save_single(_camera_payload(0.62))
    assert _floor_for_saved_camera() == pytest.approx(0.62)


def test_bulk_update_changes_floor(wired):
    """The whole-list PUT is a second writer; it must invalidate too."""
    asyncio.run(
        cameras_router.update_cameras(
            _FakeRequest([_camera_payload(0.30)]),
            db=_state.database,
            apply_cameras_settings=_apply_publish,
        )
    )
    assert _floor_for_saved_camera() == pytest.approx(0.30)

    asyncio.run(
        cameras_router.update_cameras(
            _FakeRequest([_camera_payload(0.70)]),
            db=_state.database,
            apply_cameras_settings=_apply_publish,
        )
    )
    assert _floor_for_saved_camera() == pytest.approx(0.70)


def test_profile_switch_changes_floor(wired):
    """A Day/Night profile switch changes the floor without a rule edit.

    This is the case a settings-signature cache handles only by accident: the
    signature is computed from the whole camera dict, so it does change -- but
    the GLOBAL (no-camera) form has no signature at all, so it depends on the
    apply path clearing the cache.
    """
    _save_single(_camera_payload(0.25))
    assert _floor_for_saved_camera() == pytest.approx(0.25)
    assert _ad.compute_minimum_rule_confidence() == pytest.approx(0.25)

    # Warm the global cache with the pre-switch value, then switch profiles.
    settings = effective_cameras_config()[0]
    settings['detection_profiles'] = {
        'active': 'night',
        'day': {'detection': {'zones': [_zone(0.25)]}},
        'night': {'detection': {'zones': [_zone(0.55)]}},
    }
    apply_active_camera_detection_profile(settings)
    wired.set_setting('cameras', [settings])
    _apply_publish([settings])

    assert _floor_for_saved_camera() == pytest.approx(0.55)
    # The global form must not serve the pre-switch floor.
    assert _ad.compute_minimum_rule_confidence() == pytest.approx(0.55)


def test_disabled_zone_does_not_lower_floor(wired):
    """Disabling the only zone restores the AI-settings fallback."""
    _save_single(_camera_payload(0.15))
    assert _floor_for_saved_camera() == pytest.approx(0.15)

    payload = _camera_payload(0.15)
    payload['detection']['zones'][0]['enabled'] = False
    _save_single(payload)
    assert _floor_for_saved_camera() == pytest.approx(0.45)


def test_camera_edit_does_not_leak_into_another_camera(wired):
    """One camera's low rule must not drag the other's floor down.

    The global form is a cross-camera minimum by design, so this asserts the
    per-camera form stays isolated even when both cameras are present in the
    persisted config.
    """
    other = _camera_payload(0.80)
    other['id'] = 'cam-b'
    other['name'] = 'Back Yard'
    asyncio.run(
        cameras_router.update_cameras(
            _FakeRequest([_camera_payload(0.15), other]),
            db=_state.database,
            apply_cameras_settings=_apply_publish,
        )
    )
    cameras = {camera['id']: camera for camera in effective_cameras_config()}
    assert _ad.compute_minimum_rule_confidence(camera_settings=cameras['cam-a']) == pytest.approx(0.15)
    assert _ad.compute_minimum_rule_confidence(camera_settings=cameras['cam-b']) == pytest.approx(0.80)
    assert _ad.compute_minimum_rule_confidence() == pytest.approx(0.15)
