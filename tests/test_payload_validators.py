"""Phase-22 integration tests for ``app/payload_validators.py``.

Phase-22 extracted the 9 settings payload-validator helpers (8 validators
+ 1 ``_int_field`` helper) from ``app/main.py`` into
``app/payload_validators.py`` using the hybrid-pattern template (same
as Phases 16-21).

Internally:

- ``_int_field`` is referenced as a bare name from every validator that
  uses it (~9 internal call sites). It is also pushed through the Pool A
  rebind even though no main.py caller references it -- the rebind
  preserves symmetry with the public rebinds for future cross-router
  use.

- ``validate_camera_settings`` -> ``validate_cameras_settings`` is an
  intra-cluster bare-name call resolved inside the new module.

- The cluster reaches 18 main.<attr> sites at call time (effective_*,
  normalize_*, build_stream_url, default_camera_detection_settings,
  cameras_config, etc.) so the validators can keep their original
  cross-cluster dependencies without rewriting their bodies.

Tests pin three contracts:

1. **Pool A back-compat identity.** The 9 Pool A rebinds MUST wire
   ``main.<name>`` to the SAME function object as
   ``app.payload_validators.<name>``. Re-resolved via ``sys.modules``
   per the Phase-17 lesson (defeats the
   ``tests/test_api.py::_load_app`` sys-modules-wipe state leak).

2. **Behavior of each validator.** Each validator has subtle coercion
   + range-check semantics:

   - ``_int_field``: TypeError/ValueError/HTTPException on
     non-numeric, out-of-range, in-range accepted.
   - ``validate_alert_email_settings``: ``port`` in [1, 65535],
     TLS/SSL mutual exclusion, missing host when enabled,
     valid from_address, defaults preserved for missing keys.
   - ``validate_push_notification_settings``: default ``server_url`` to
     ntfy.sh, default ``priority`` to 'default', valid_priorities
     enforcement, topic required when enabled.
   - ``validate_camera_settings``: backend in {onvif, rtsp},
     ``stream_url`` (or host+...) required for ONVIF/RTSP, flip in
     {none, horizontal, vertical, both}, dims/fps clamped,
     motion-migration delegation, recording/ptz delegated to
     Phase-19.
   - ``validate_cameras_settings``: non-list rejected, dup-id
     rejected, normal list passes, makes only list of normalized
     per-camera dicts.
   - ``validate_recording_settings``: format must be mp4 (avi coerced),
     all int-fields clamped, auto_purge_enabled via
     ``main.normalize_bool_setting``.
   - ``validate_storage_settings``: blank dir rejected, ``database``
     path preserved from ``main.config`` even on override.
   - ``validate_auth_settings``: session_timeout_hours in [0.25, 720],
     max_login_attempts/lockout_minutes via _int_field.
   - ``validate_live_settings``: all numeric fields clamped or ranged,
     motion_*_fraction ranges, history_minutes range, full-shape
     return.

3. **Top-level preload pattern.** ``import app.main`` BEFORE
   ``import app.payload_validators`` at module top -- same pattern as
   Phases 17-21 tests. Without this, pytest collection triggers
   the circular-import gate at ``app.payload_validators`` load time
   (its top has ``import app.main as main`` for the 18 Pool C reach
   sites).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest  # noqa: E402  -- used below

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Preload app.main before payload_validators. After Pool C elimination,
# payload_validators.py has no module-level import of app.main (only one
# remaining lazy ``from app.main import config`` inside
# validate_storage_settings). The preload is kept for safety: app.main's
# Pool A rebind block imports from payload_validators, so loading
# payload_validators first could still trigger a partial-load cycle.
import app.main  # noqa: E402  -- must precede the import below
import app.payload_validators as payload_validators  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Pool A back-compat identity -- ``main.<name> is pv.<name>``.
#    Re-resolve via sys.modules per Phase-17 lesson (defeats the
#    tests/test_api.py::_load_app() sys-modules-wipe state leak).
# ---------------------------------------------------------------------------


@pytest.fixture
def main():
    """Return the CURRENT ``app.main`` module instance. See the module
    docstring for why we cannot rely on the test file's module-level
    globals directly. Centralised as fixtures so the rationale lives
    in one comment rather than copy-pasted into 9 tests."""
    return sys.modules["app.main"]


@pytest.fixture
def current_payload_validators():
    """Return the CURRENT ``app.payload_validators`` module instance.
    See the ``main`` fixture above for the leak rationale."""
    return sys.modules["app.payload_validators"]


@pytest.fixture
def pv():
    """Convenience alias for ``current_payload_validators`` -- used by
    the behavior tests below to call ``pv.validate_camera_settings(...)``
    etc. without ``import app.payload_validators as pv`` boilerplate."""
    return sys.modules["app.payload_validators"]


# ---------------------------------------------------------------------------
# 2. Helpers -- isolate cross-module deps via monkeypatched stubs.
# ---------------------------------------------------------------------------


def _install_validator_dependencies(
    monkeypatch,
    *,
    effective_email_alert_settings=None,
    effective_push_notification_settings=None,
    normalize_bool_setting=None,
    normalize_camera_id=None,
    camera_default_name=None,
    default_camera_detection_settings=None,
    build_stream_url=None,
    normalize_label_list=None,
    normalize_monitoring_zones=None,
    _migrate_legacy_camera_motion=None,
    normalize_camera_recording_settings=None,
    normalize_camera_ptz_settings=None,
    effective_recording_config=None,
    effective_storage_config=None,
    config=None,
    effective_auth_config=None,
    effective_live_config=None,
    cameras_config=None,
):
    """Install hermetic stand-ins for the cross-module deps of the 9 validators.

    All names are module-level bindings in ``app.payload_validators``; patch
    on ``pv`` so the bare-name lookups inside each validator body see the stub.

    Two special cases:
    - ``config`` -- accessed as ``_state.config`` inside
      ``validate_storage_settings``; patch on ``app.state``.
    - ``cameras_config`` -- accessed as ``_state.cameras_config``; patch
      on ``app.state``.
    """
    import app.state as _state
    pv = sys.modules['app.payload_validators']

    if effective_email_alert_settings is None:
        effective_email_alert_settings = lambda: {}
    if effective_push_notification_settings is None:
        effective_push_notification_settings = lambda: {}
    if normalize_bool_setting is None:
        normalize_bool_setting = lambda raw, default=False: bool(raw) if raw is not None else default
    if normalize_camera_id is None:
        normalize_camera_id = lambda value, fallback='camera-1': str(value or '') or fallback
    if camera_default_name is None:
        camera_default_name = lambda settings, fallback='Camera': (
            str(settings.get('name') or settings.get('device') or fallback).strip() or fallback
        )
    if default_camera_detection_settings is None:
        default_camera_detection_settings = lambda: {'object_detection_enabled': True, 'zones': []}
    if build_stream_url is None:
        build_stream_url = lambda settings: str(settings.get('stream_url') or '')
    if normalize_label_list is None:
        normalize_label_list = lambda raw: list(raw) if isinstance(raw, list) else (
            list(raw.split(',')) if isinstance(raw, str) else []
        )
    if normalize_monitoring_zones is None:
        normalize_monitoring_zones = lambda raw: list(raw) if isinstance(raw, list) else []
    if _migrate_legacy_camera_motion is None:
        def _migrate_legacy_camera_motion_stub(detection): pass
        _migrate_legacy_camera_motion = _migrate_legacy_camera_motion_stub
    if normalize_camera_recording_settings is None:
        normalize_camera_recording_settings = lambda raw: dict(raw or {})
    if normalize_camera_ptz_settings is None:
        normalize_camera_ptz_settings = lambda raw: dict(raw or {})
    if effective_recording_config is None:
        effective_recording_config = lambda: {}
    if effective_storage_config is None:
        effective_storage_config = lambda: {}
    if config is None:
        config = {'storage': {}}
    if effective_auth_config is None:
        effective_auth_config = lambda: {}
    if effective_live_config is None:
        effective_live_config = lambda: {}
    if cameras_config is None:
        cameras_config = []

    # All module-level names in pv -- patch where the call sites look them up.
    monkeypatch.setattr(pv, 'effective_email_alert_settings', effective_email_alert_settings)
    monkeypatch.setattr(pv, 'effective_push_notification_settings', effective_push_notification_settings)
    monkeypatch.setattr(pv, 'effective_storage_config', effective_storage_config)
    monkeypatch.setattr(pv, 'effective_auth_config', effective_auth_config)
    monkeypatch.setattr(pv, 'effective_live_config', effective_live_config)
    monkeypatch.setattr(pv, 'effective_recording_config', effective_recording_config)
    monkeypatch.setattr(pv, 'normalize_bool_setting', normalize_bool_setting)
    monkeypatch.setattr(pv, 'normalize_camera_id', normalize_camera_id)
    monkeypatch.setattr(pv, 'camera_default_name', camera_default_name)
    monkeypatch.setattr(pv, 'default_camera_detection_settings', default_camera_detection_settings)
    monkeypatch.setattr(pv, 'build_stream_url', build_stream_url)
    monkeypatch.setattr(pv, 'normalize_label_list', normalize_label_list)
    monkeypatch.setattr(pv, 'normalize_monitoring_zones', normalize_monitoring_zones)
    monkeypatch.setattr(pv, '_migrate_legacy_camera_motion', _migrate_legacy_camera_motion)
    monkeypatch.setattr(pv, 'normalize_camera_recording_settings', normalize_camera_recording_settings)
    monkeypatch.setattr(pv, 'normalize_camera_ptz_settings', normalize_camera_ptz_settings)

    # cameras_config and config accessed via _state.
    monkeypatch.setattr(_state, 'cameras_config', cameras_config)
    monkeypatch.setattr(_state, 'config', config)


# ---------------------------------------------------------------------------
# 3. _int_field -- pure coercion + range guard.
# ---------------------------------------------------------------------------


def test_int_field_returns_default_when_key_missing_and_in_range(pv):
    """Missing key -> default (assumed in-range)."""
    assert pv._int_field({}, 'width', 1280, 160, 7680) == 1280


def test_int_field_rejects_non_numeric_with_http_exception(pv):
    """``width='oops'`` triggers TypeError/ValueError -> HTTPException(400)."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        pv._int_field({'width': 'oops'}, 'width', 1280, 160, 7680)
    assert exc_info.value.status_code == 400
    assert 'width must be an integer' in exc_info.value.detail


def test_int_field_rejects_out_of_range_low(pv):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        pv._int_field({'width': 100}, 'width', 1280, 160, 7680)
    assert exc_info.value.status_code == 400
    assert 'between 160 and 7680' in exc_info.value.detail


def test_int_field_rejects_out_of_range_high(pv):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        pv._int_field({'width': 10000}, 'width', 1280, 160, 7680)
    assert exc_info.value.status_code == 400


def test_int_field_accepts_in_range_value(pv):
    assert pv._int_field({'width': 1920}, 'width', 1280, 160, 7680) == 1920


# ---------------------------------------------------------------------------
# 4. validate_alert_email_settings
# ---------------------------------------------------------------------------


def test_validate_alert_email_settings_keeps_default_port_when_missing(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_alert_email_settings({})
    assert out['port'] == 587  # default


def test_validate_alert_email_settings_rejects_non_integer_port(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_alert_email_settings({'port': 'oops'})
    assert exc_info.value.status_code == 400
    assert 'SMTP port must be an integer' in exc_info.value.detail


def test_validate_alert_email_settings_rejects_out_of_range_port(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_alert_email_settings({'port': 99999})
    assert exc_info.value.status_code == 400
    assert 'between 1 and 65535' in exc_info.value.detail


def test_validate_alert_email_settings_requires_host_when_enabled(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_alert_email_settings({'enabled': True, 'host': '', 'from_address': 'a@b.com'})
    assert exc_info.value.status_code == 400
    assert 'SMTP host is required' in exc_info.value.detail


def test_validate_alert_email_settings_requires_from_address_when_enabled(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_alert_email_settings({'enabled': True, 'host': 'smtp.example.com', 'from_address': ''})
    assert exc_info.value.status_code == 400
    assert 'From address is required' in exc_info.value.detail


def test_validate_alert_email_settings_rejects_invalid_from_address(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_alert_email_settings({'enabled': True, 'host': 'smtp.example.com', 'from_address': 'no_at_sign'})
    assert exc_info.value.status_code == 400
    assert 'must be a valid email' in exc_info.value.detail


def test_validate_alert_email_settings_use_ssl_disables_use_tls(monkeypatch, pv):
    """``use_ssl=True`` -> ``use_tls`` cleared (mutual exclusion rule)."""
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_alert_email_settings({'use_tls': True, 'use_ssl': True})
    assert out['use_ssl'] is True
    assert out['use_tls'] is False


def test_validate_alert_email_settings_coerces_enabled_string_truthy(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_alert_email_settings({'enabled': 'YES', 'host': 'smtp.example.com', 'from_address': 'a@b.com'})
    assert out['enabled'] is True


def test_validate_alert_email_settings_coerces_use_tls_string_falsy(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_alert_email_settings({'use_tls': 'no'})
    assert out['use_tls'] is False


def test_validate_alert_email_settings_drops_unknown_keys(monkeypatch, pv):
    """The validator enforces an allowed-keys allow-list
    ({'enabled','host','port','username','password','from_address',
     'use_tls','use_ssl'}); any other key in the payload is silently
    dropped to prevent typos / future-field regressions from sneaking
    through the schema."""
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_alert_email_settings({
        'unknown_evil_key': 'should_be_dropped',
        'recipients': ['attacker@example.com'],
        'enabled': True,
        'host': 'smtp.example.com',
        'from_address': 'a@b.com',
    })
    assert 'unknown_evil_key' not in out
    assert 'recipients' not in out
    # Allowed keys round-trip correctly.
    assert out['enabled'] is True
    assert out['host'] == 'smtp.example.com'


# ---------------------------------------------------------------------------
# 5. validate_push_notification_settings
# ---------------------------------------------------------------------------


def test_validate_push_notification_settings_defaults_to_ntfy_sh_server(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_push_notification_settings({})
    assert out['server_url'] == 'https://ntfy.sh'


def test_validate_push_notification_settings_defaults_priority_to_default(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_push_notification_settings({})
    assert out['priority'] == 'default'


def test_validate_push_notification_settings_rejects_unknown_priority(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_push_notification_settings({'priority': 'super-urgent'})
    assert exc_info.value.status_code == 400
    assert 'priority must be one of' in exc_info.value.detail


def test_validate_push_notification_settings_accepts_valid_priority(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_push_notification_settings({'priority': 'high'})
    assert out['priority'] == 'high'


def test_validate_push_notification_settings_requires_topic_when_enabled(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_push_notification_settings({'enabled': True, 'topic': ''})
    assert exc_info.value.status_code == 400
    assert 'Topic is required' in exc_info.value.detail


def test_validate_push_notification_settings_coerces_enabled_via_main_stub(monkeypatch, pv):
    """``enabled=True`` (bool) stays True via main.normalize_bool_setting stub."""
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_push_notification_settings({'enabled': True, 'topic': 'alerts'})
    assert out['enabled'] is True


# ---------------------------------------------------------------------------
# 6. validate_camera_settings
# ---------------------------------------------------------------------------


def test_validate_camera_settings_rejects_unknown_backend(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_camera_settings({'backend': 'mjpeg', 'stream_url': 'rtsp://example/stream'})
    assert exc_info.value.status_code == 400
    assert 'backend must be onvif or rtsp' in exc_info.value.detail


def test_validate_camera_settings_rejects_missing_stream_url_for_onvif(monkeypatch, pv):
    """When ``main.build_stream_url`` returns an empty string for an ONVIF
    camera, ``validate_camera_settings`` raises HTTPException(400)
    pinning the stream_url-or-host requirement."""
    from fastapi import HTTPException
    # Patch main.build_stream_url to return empty via the standard fixture
    # helper -- do NOT patch payload_validators.build_stream_url because
    # the cluster reaches the helper via ``main.<attr>``.
    _install_validator_dependencies(
        monkeypatch,
        build_stream_url=lambda settings: '',
    )
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_camera_settings({'backend': 'onvif'})
    assert exc_info.value.status_code == 400
    assert 'stream_url is required' in exc_info.value.detail


def test_validate_camera_settings_rejects_invalid_flip(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch, build_stream_url=lambda settings: 'rtsp://ok')
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_camera_settings({'flip': 'mirror'})
    assert exc_info.value.status_code == 400
    assert 'flip must be none' in exc_info.value.detail


def test_validate_camera_settings_accepts_valid_flip(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch, build_stream_url=lambda settings: 'rtsp://ok')
    out = pv.validate_camera_settings({'flip': 'both'})
    assert out['flip'] == 'both'


def test_validate_camera_settings_clamps_dimensions_to_min_max(monkeypatch, pv):
    """Per-spec: width [160, 7680] / height [120, 4320] / fps [1, 120].

    Round-2 reviewer nit: assert the distinct per-field error substring
    on each guard so a regression swapping clamp ranges can't silently
    pass. The dims guards fire before the stream_url guard, so a stub
    stream_url is not strictly needed but is passed for symmetry with
    the other tests in this section."""
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch, build_stream_url=lambda settings: 'rtsp://ok')
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_camera_settings({'width': 10})  # below min 160
    assert exc_info.value.status_code == 400
    assert 'width must be between 160 and 7680' in exc_info.value.detail
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_camera_settings({'height': 10})  # below min 120
    assert exc_info.value.status_code == 400
    assert 'height must be between 120 and 4320' in exc_info.value.detail
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_camera_settings({'fps': 200})  # above max 120
    assert exc_info.value.status_code == 400
    assert 'fps must be between 1 and 120' in exc_info.value.detail


def test_validate_camera_settings_threads_detection_through_migration(monkeypatch, pv):
    """The cluster reaches 5 main.* call sites for detection/recording/ptz
    normalization AND threads the detection dict through
    ``main._migrate_legacy_camera_motion`` -- verify BOTH the call count
    AND the state-side-effect on the detection dict.

    The migration stub sets ``detection['migrated'] = True`` so a
    regression where the cluster replaces the call with a bare-name
    function or skips it entirely will be detectable (the assertion
    on ``out['detection']['migrated'] is True`` would fail)."""
    calls: dict = {'label': 0, 'zones': 0, 'motion_migrate': 0, 'recording': 0, 'ptz': 0}

    def _record_labels(raw):
        calls['label'] += 1
        return list(raw) if isinstance(raw, list) else []

    def _record_zones(raw):
        calls['zones'] += 1
        return list(raw) if isinstance(raw, list) else []

    def _record_motion(detection):
        calls['motion_migrate'] += 1
        # Side-effect marker so the post-call state assertion can verify
        # the cluster actually invoked the cross-module helper.
        detection['migrated'] = True

    def _record_recording(raw):
        calls['recording'] += 1
        return dict(raw or {})

    def _record_ptz(raw):
        calls['ptz'] += 1
        return dict(raw or {})

    _install_validator_dependencies(
        monkeypatch,
        normalize_label_list=_record_labels,
        normalize_monitoring_zones=_record_zones,
        _migrate_legacy_camera_motion=_record_motion,
        normalize_camera_recording_settings=_record_recording,
        normalize_camera_ptz_settings=_record_ptz,
        build_stream_url=lambda settings: 'rtsp://ok',
    )
    out = pv.validate_camera_settings({'detection': {'object_labels': ['car']}, 'zones': [], 'recording': {}, 'ptz': {}})
    assert calls['label'] == 1
    assert calls['zones'] == 1
    assert calls['motion_migrate'] == 1
    assert calls['recording'] == 1
    assert calls['ptz'] == 1
    # State-side-effect: the migration stub set 'migrated' on the
    # detection dict, and the cluster re-uses that same dict for the
    # returned camera settings -- so we observe the side-effect downstream.
    assert out['detection']['migrated'] is True


def test_validate_camera_settings_preserves_password_when_empty(monkeypatch, pv):
    """Empty ``password`` in payload preserves the existing password (so
    PUT doesn't blow away the stored ONVIF credential)."""
    _install_validator_dependencies(monkeypatch, build_stream_url=lambda settings: 'rtsp://ok')
    out = pv.validate_camera_settings({'password': ''}, current={'password': 'kept-secret'})
    assert out['password'] == 'kept-secret'


# ---------------------------------------------------------------------------
# 7. validate_cameras_settings
# ---------------------------------------------------------------------------


def test_validate_cameras_settings_rejects_non_list_payload(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_cameras_settings({'cameras': 'not-a-list'})
    assert exc_info.value.status_code == 400
    assert 'cameras must be a list' in exc_info.value.detail


def test_validate_cameras_settings_rejects_non_dict_rows(monkeypatch, pv):
    """When any row inside the cameras list is not a dict, the helper
    raises ``HTTPException(400)`` with ``Each camera must be an object``
    BEFORE recursing into ``validate_camera_settings``.

    Use a single-row payload so the loop hits the non-dict check first
    (a multi-row payload would call ``validate_camera_settings`` on the
    first row and that validation may fail for unrelated reasons, e.g.
    missing stream_url, masking the non-dict error)."""
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_cameras_settings(['not-a-dict'])
    assert exc_info.value.status_code == 400
    assert 'Each camera must be an object' in exc_info.value.detail


def test_validate_cameras_settings_rejects_duplicate_ids(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch, build_stream_url=lambda settings: 'rtsp://ok')
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_cameras_settings([
            {'id': 'cam-shared', 'stream_url': 'rtsp://a'},
            {'id': 'cam-shared', 'stream_url': 'rtsp://b'},
        ])
    assert exc_info.value.status_code == 400
    assert 'Duplicate camera id' in exc_info.value.detail


def test_validate_cameras_settings_passes_with_unique_ids(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch, build_stream_url=lambda settings: 'rtsp://ok')
    out = pv.validate_cameras_settings([
        {'id': 'cam-a', 'stream_url': 'rtsp://a'},
        {'id': 'cam-b', 'stream_url': 'rtsp://b'},
    ])
    assert [c['id'] for c in out] == ['cam-a', 'cam-b']


def test_validate_cameras_settings_accepts_direct_list_payload(monkeypatch, pv):
    """``payload`` can be a bare list (not wrapped in ``{'cameras': [...]}``)."""
    _install_validator_dependencies(monkeypatch, build_stream_url=lambda settings: 'rtsp://ok')
    out = pv.validate_cameras_settings([{'id': 'cam-1', 'stream_url': 'rtsp://1'}])
    assert len(out) == 1


# ---------------------------------------------------------------------------
# 8. validate_recording_settings
# ---------------------------------------------------------------------------


def test_validate_recording_settings_enforces_mp4_format(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_recording_settings({'format': 'mov'})
    assert exc_info.value.status_code == 400
    assert 'format must be mp4' in exc_info.value.detail


def test_validate_recording_settings_coerces_avi_to_mp4(monkeypatch, pv):
    """Legacy avi preference -> mp4 (silent coercion)."""
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_recording_settings({'format': 'avi'})
    assert out['format'] == 'mp4'


def test_validate_recording_settings_rejects_non_numeric_max_clip_seconds(monkeypatch, pv):
    """``max_clip_seconds='oops'`` triggers ``_int_field`` rejection and
    surfaces the standard ``HTTPException(400)``.

    This locks in the wiring between ``validate_recording_settings``
    and the per-field ``_int_field`` coercion that backs all 6 numeric
    fields (pre/post/extension/max_clip/chunk_duration/retention_days/
    max_storage_gb)."""
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_recording_settings({'max_clip_seconds': 'oops'})
    assert exc_info.value.status_code == 400
    assert 'max_clip_seconds must be an integer' in exc_info.value.detail


def test_validate_recording_settings_clamps_max_clip_seconds(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException):
        pv.validate_recording_settings({'max_clip_seconds': 99999})  # above max 3600


def test_validate_recording_settings_keeps_all_numeric_fields(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_recording_settings({})
    assert 'pre_event_seconds' in out
    assert 'post_event_seconds' in out
    assert 'extension_step_seconds' in out
    assert 'max_clip_seconds' in out
    assert 'chunk_duration_seconds' in out
    assert 'retention_days' in out
    assert 'max_storage_gb' in out
    assert 'auto_purge_enabled' in out


# ---------------------------------------------------------------------------
# 9. validate_storage_settings
# ---------------------------------------------------------------------------


def test_validate_storage_settings_rejects_blank_dir(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_storage_settings({'snapshots_dir': ''})
    assert exc_info.value.status_code == 400
    assert 'snapshots_dir cannot be blank' in exc_info.value.detail


def test_validate_storage_settings_preserves_database_path_from_config(monkeypatch, pv):
    """The on-disk DB path stays the source of truth even when override
    is sent (matches the contract in main.effective_storage_config)."""
    _install_validator_dependencies(
        monkeypatch,
        config={'storage': {'database': 'data/source.sqlite3'}},
    )
    out = pv.validate_storage_settings({'database': 'data/CANT-OVERRIDE.sqlite3'})
    assert out['database'] == 'data/source.sqlite3'


def test_validate_storage_settings_returns_all_five_dir_fields(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_storage_settings({})
    assert set(out.keys()) == {'data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir', 'database'}


# ---------------------------------------------------------------------------
# 10. validate_auth_settings
# ---------------------------------------------------------------------------


def test_validate_auth_settings_rejects_session_below_min(monkeypatch, pv):
    """``session_timeout_hours`` must be in [0.25, 720]."""
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_auth_settings({'session_timeout_hours': 0.01})
    assert exc_info.value.status_code == 400
    assert 'between 0.25 and 720' in exc_info.value.detail


def test_validate_auth_settings_rejects_session_above_max(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException):
        pv.validate_auth_settings({'session_timeout_hours': 9999})


def test_validate_auth_settings_rejects_non_numeric_session(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException):
        pv.validate_auth_settings({'session_timeout_hours': 'forever'})


def test_validate_auth_settings_rejects_max_login_attempts_out_of_range(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException):
        pv.validate_auth_settings({'max_login_attempts': 200})  # above max 100


def test_validate_auth_settings_returns_three_fields(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_auth_settings({})
    assert set(out.keys()) == {'session_timeout_hours', 'max_login_attempts', 'lockout_minutes'}


# ---------------------------------------------------------------------------
# 11. validate_live_settings
# ---------------------------------------------------------------------------


def test_validate_live_settings_rejects_snapshot_refresh_out_of_range(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException):
        pv.validate_live_settings({'snapshot_refresh_ms': 50})  # below min 150


def test_validate_live_settings_rejects_detection_interval_non_numeric(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException):
        pv.validate_live_settings({'detection_interval_seconds': 'quick'})


def test_validate_live_settings_rejects_detection_interval_below_min(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException):
        pv.validate_live_settings({'detection_interval_seconds': 0.01})  # below min 0.1


def test_validate_live_settings_rejects_motion_gate_fraction_out_of_range(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException):
        pv.validate_live_settings({'motion_gate_fraction': 0.00001})  # below min 0.0001
    with pytest.raises(HTTPException):
        pv.validate_live_settings({'motion_gate_fraction': 0.9})  # above max 0.5


def test_validate_live_settings_rejects_history_minutes_out_of_range(monkeypatch, pv):
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException):
        pv.validate_live_settings({'detection_history_minutes': 200})  # above max 120


def test_validate_live_settings_rejects_non_numeric_history_minutes(monkeypatch, pv):
    """``detection_history_minutes='forever'`` triggers the cluster's
    TypeError/ValueError fallback path -> HTTPException(400). Exercise
    the wiring between ``validate_live_settings`` and the explicit
    int-cast fallback for one of the 7 _int_field-style fields."""
    from fastapi import HTTPException
    _install_validator_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        pv.validate_live_settings({'detection_history_minutes': 'forever'})
    assert exc_info.value.status_code == 400
    assert 'detection_history_minutes must be a whole number' in exc_info.value.detail


def test_validate_live_settings_returns_all_twelve_fields(monkeypatch, pv):
    _install_validator_dependencies(monkeypatch)
    out = pv.validate_live_settings({})
    assert set(out.keys()) == {
        'snapshot_refresh_ms', 'detection_status_refresh_ms',
        'detection_interval_seconds', 'event_debounce_seconds',
        'background_detection_enabled', 'detection_history_minutes',
        'motion_pixel_threshold', 'motion_gate_fraction',
        'motion_scale_fraction', 'motion_background_alpha',
        'periodic_scan_interval_seconds',
    }
