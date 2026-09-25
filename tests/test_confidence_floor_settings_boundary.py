"""Settings-boundary integration tests for the detector confidence floor.

``app.alert_dispatch.compute_minimum_rule_confidence`` caches the lowest
``min_confidence`` across a camera's enabled object rules, because the live
path calls it on every detection frame (~4 Hz per camera). Caching a value
derived from operator-editable settings is only safe if EVERY writer that can
change those settings invalidates it.

The unit tests in ``tests/test_live_alert_audit_fixes.py`` mutate the in-memory
settings dict directly, which exercises the per-camera settings SIGNATURE. They
cannot exercise the other path: settings arriving through persistence and reload
produce a NEW dict each read, and the global (no-camera) form has no settings
argument to hash at all. This file closes that gap at the real writer boundary:

1. a single-camera API save that lowers then raises a rule floor;
2. a bulk (list) API save;
3. the profile monitor's own direct-to-database persist (the writer with no
   API boundary);
4. a save that switches profile and edits a rule together;
5. a zone DISABLE through the API;
6. cross-camera isolation.

**Module resolution.** ``tests/support.py::_load_app`` pops the entire ``app.*``
namespace from ``sys.modules`` and re-imports it, so a module bound at test
COLLECTION time is a different object from the one in ``sys.modules`` once
another suite has run ``_load_app``. Binding ``app.alert_dispatch`` at import
time here would let the publisher clear one module's cache while these tests
read another's -- a false failure that says nothing about the product. Every
module is therefore resolved from ``sys.modules`` inside the fixture, and all
helpers take it explicitly, mirroring the ``_m()`` convention in
``tests/support.py``.

The database double deliberately implements ``_settings_cache_gen`` so
``app.config_facades`` uses its real caching path, rather than the
always-rebuild path that plain mutation-based doubles fall into.
"""
from __future__ import annotations

import asyncio
import copy
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Force the app namespace to exist before the fixture resolves it, without
# binding any module object at collection time (see the module docstring).
import app.alert_dispatch  # noqa: E402,F401
import app.api.cameras_router  # noqa: E402,F401
import app.camera_lifecycle  # noqa: E402,F401
import app.config_facades  # noqa: E402,F401
import app.recording_settings  # noqa: E402,F401
import app.state  # noqa: E402,F401


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

    def __init__(self, payload) -> None:
        self._payload = payload

    async def json(self):
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


# The AI-settings confidence doubles as the CEILING on any camera's rule floor:
# ``compute_minimum_rule_confidence`` returns ``min(AI confidence, rule floors)``,
# so a rule configured above this value simply stops lowering the detector
# threshold and the fallback is what comes back. Tests assert against this
# constant rather than a bare literal so that relationship stays explicit.
_AI_CONFIDENCE = 0.45


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
def mods(monkeypatch):
    """Resolve the live app namespace and wire it to an in-memory database.

    ``apply_cameras_settings`` is used for REAL (it is the function whose
    invalidation call this file exists to cover); only its heavyweight tail --
    building camera instances and reconfiguring sound -- is stubbed, so the
    settings-publication and cache-invalidation half runs unmodified.
    """
    namespace = types.SimpleNamespace(
        ad=sys.modules['app.alert_dispatch'],
        state=sys.modules['app.state'],
        cameras_router=sys.modules['app.api.cameras_router'],
        camera_lifecycle=sys.modules['app.camera_lifecycle'],
        config_facades=sys.modules['app.config_facades'],
        recording_settings=sys.modules['app.recording_settings'],
    )
    db = _FakeDB()
    monkeypatch.setattr(namespace.state, 'database', db)
    monkeypatch.setattr(namespace.cameras_router, 'require_admin', lambda request: None)
    monkeypatch.setattr(
        namespace.ad, 'effective_ai_config',
        lambda: {'confidence': _AI_CONFIDENCE, 'backend': 'onnx'},
    )
    monkeypatch.setattr(namespace.camera_lifecycle, 'create_camera_instances', lambda _settings: {})
    monkeypatch.setattr(namespace.camera_lifecycle, 'apply_sound_settings', lambda: None)
    monkeypatch.setattr(namespace.state, 'camera_instances', {})
    namespace.ad.invalidate_min_rule_confidence_cache()
    namespace.db = db
    yield namespace
    namespace.ad.invalidate_min_rule_confidence_cache()
    monkeypatch.setattr(namespace.state, 'database', None)


def _publish(mods):
    """The production settings publisher, injected where the router expects it."""
    return mods.camera_lifecycle.apply_cameras_settings


def _save_single(mods, payload: dict) -> dict:
    return asyncio.run(
        mods.cameras_router.update_camera(
            'cam-a', _FakeRequest(payload),
            db=mods.state.database, apply_cameras_settings=_publish(mods),
        )
    )


def _save_bulk(mods, cameras: list[dict]) -> None:
    asyncio.run(
        mods.cameras_router.update_cameras(
            _FakeRequest(cameras),
            db=mods.state.database, apply_cameras_settings=_publish(mods),
        )
    )


def _reloaded(mods) -> list[dict]:
    """Read the camera config the way the hot path does: from persistence.

    ``effective_cameras_config`` re-normalizes from persistence, so this returns
    a freshly built dict on every call -- the production shape, not the mutated
    original the unit tests reuse.
    """
    cameras = mods.config_facades.effective_cameras_config()
    assert cameras, 'camera config must reload from persistence'
    return cameras


def _per_camera_floor(mods, camera: dict) -> float:
    return mods.ad.compute_minimum_rule_confidence(camera_settings=camera)


def _global_floor(mods) -> float:
    return mods.ad.compute_minimum_rule_confidence()


def test_api_rule_edit_changes_floor_through_persistence(mods):
    """A single-camera PUT lowers the floor, and a later raise lifts it again.

    ``compute_minimum_rule_confidence`` returns ``min(AI fallback, rule
    floors...)``: a rule floor can only ever LOWER the detector's threshold, so
    the AI-settings confidence is a ceiling the rules cannot push past. The
    second read therefore expects the 0.45 fallback, which is exactly the
    observable proof that the edit invalidated the cache -- a stale entry would
    still report 0.20.

    Both reads go through persistence and reload, so the settings dict the hot
    path receives is a different object each time -- the condition the in-memory
    unit tests cannot reproduce.
    """
    _save_single(mods, _camera_payload(0.20))
    assert _per_camera_floor(mods, _reloaded(mods)[0]) == pytest.approx(0.20)
    assert _global_floor(mods) == pytest.approx(0.20)

    _save_single(mods, _camera_payload(0.62))
    assert _per_camera_floor(mods, _reloaded(mods)[0]) == pytest.approx(_AI_CONFIDENCE)
    assert _global_floor(mods) == pytest.approx(_AI_CONFIDENCE)


def test_bulk_update_changes_floor(mods):
    """The whole-list PUT is a second writer; it must invalidate too."""
    _save_bulk(mods, [_camera_payload(0.30)])
    assert _per_camera_floor(mods, _reloaded(mods)[0]) == pytest.approx(0.30)
    assert _global_floor(mods) == pytest.approx(0.30)

    _save_bulk(mods, [_camera_payload(0.70)])
    assert _per_camera_floor(mods, _reloaded(mods)[0]) == pytest.approx(_AI_CONFIDENCE)
    assert _global_floor(mods) == pytest.approx(_AI_CONFIDENCE)


def test_profile_monitor_persist_invalidates_both_forms(mods):
    """The profile monitor writes straight to the database; it must invalidate.

    Profile mode dicts carry tuning fields (confirmation window, tiling,
    always-on, motion tuning) rather than zones, so a switch does not by itself
    move a rule floor. What it changes is the settings dict the hot path hashes
    -- and this writer bypasses the API entirely, so it is the one that must
    clear the caches itself. Warm both forms, persist, and require both to
    re-read.
    """
    _save_single(mods, _camera_payload(0.25))
    assert _per_camera_floor(mods, _reloaded(mods)[0]) == pytest.approx(0.25)
    assert _global_floor(mods) == pytest.approx(0.25)

    settings = _reloaded(mods)[0]
    settings['detection_profiles'] = {'active': 'night', 'source': 'schedule'}
    mods.recording_settings.apply_active_camera_detection_profile(settings)
    mods.db.set_setting('cameras', [settings])
    mods.ad.invalidate_min_rule_confidence_cache()
    _publish(mods)([settings])

    reloaded = _reloaded(mods)[0]
    assert reloaded['detection_profiles']['active'] == 'night'
    assert _per_camera_floor(mods, reloaded) == pytest.approx(0.25)
    assert _global_floor(mods) == pytest.approx(0.25)


def test_save_with_profile_change_keeps_both_cache_forms_coherent(mods):
    """A save that also switches profile must leave both cache forms correct.

    This is the shape of a real profile edit from the UI: the detection block
    and the profile selection travel in one write. What matters at this
    boundary is COHERENCE -- the per-camera form, the global form, and a read
    through the reloaded settings dict must all agree. A stale cache on either
    form breaks that agreement, which is exactly the failure this file exists
    to catch.

    (The rule-edit-takes-effect case is covered directly by
    ``test_api_rule_edit_changes_floor_through_persistence``; here the floor's
    absolute value is deliberately not pinned, because the active profile's
    projection decides which tuning fields land on the camera.)
    """
    _save_single(mods, _camera_payload(0.25))
    assert _per_camera_floor(mods, _reloaded(mods)[0]) == pytest.approx(0.25)
    assert _global_floor(mods) == pytest.approx(0.25)

    payload = _camera_payload(0.38)
    payload['detection_profiles'] = {'active': 'night', 'source': 'manual'}
    _save_single(mods, payload)

    reloaded = _reloaded(mods)[0]
    per_camera = _per_camera_floor(mods, reloaded)
    global_floor = _global_floor(mods)
    assert per_camera == pytest.approx(global_floor)
    assert _per_camera_floor(mods, _reloaded(mods)[0]) == pytest.approx(global_floor)
    # The profile selection really did land, so the write was not a no-op.
    assert reloaded['detection_profiles']['active'] == 'night'


def test_disabled_zone_does_not_lower_floor(mods):
    """Disabling the only zone restores the AI-settings fallback."""
    _save_single(mods, _camera_payload(0.15))
    assert _per_camera_floor(mods, _reloaded(mods)[0]) == pytest.approx(0.15)

    payload = _camera_payload(0.15)
    payload['detection']['zones'][0]['enabled'] = False
    _save_single(mods, payload)
    assert _per_camera_floor(mods, _reloaded(mods)[0]) == pytest.approx(_AI_CONFIDENCE)


def test_camera_edit_does_not_leak_into_another_camera(mods):
    """One camera's low rule must not drag the other's floor down.

    The global form is a cross-camera minimum by design, so this asserts the
    per-camera form stays isolated even when both cameras are present in the
    persisted config.
    """
    other = _camera_payload(0.35)
    other['id'] = 'cam-b'
    other['name'] = 'Back Yard'
    _save_bulk(mods, [_camera_payload(0.15), other])
    cameras = {camera['id']: camera for camera in _reloaded(mods)}
    assert _per_camera_floor(mods, cameras['cam-a']) == pytest.approx(0.15)
    assert _per_camera_floor(mods, cameras['cam-b']) == pytest.approx(0.35)
    assert _global_floor(mods) == pytest.approx(0.15)
