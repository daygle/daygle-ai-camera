"""Phase-19 integration tests for ``app/recording_settings.py``.

Phase-19 extracted the 4 recording-settings helpers
(``normalize_camera_recording_settings``, ``normalize_camera_ptz_settings``,
``_normalize_camera_sound_settings``, ``_migrate_legacy_camera_motion``)
from ``app/main.py`` into ``app/recording_settings.py`` using the
hybrid-pattern template (same as Phase-16 ``app/auth_gates.py``,
Phase-17 ``app/config_facades.py``, Phase-18 ``app/camera_config.py``).

Siblings still on ``app/main.py`` (``camera_event_recording_config``,
``validate_camera_settings``) reach these helpers as bare names inside
function bodies; the top-of-file Pool A rebind wires ``main.<name>``
before any of those bodies evaluates.

Tests pin three contracts:

1. **Pool A back-compat identity.** The 4 Pool A rebinds MUST wire
   ``main.<name>`` to the SAME function object as
   ``app.recording_settings.<name>``. Re-resolved via ``sys.modules``
   to defeat the ``tests/test_api.py::_load_app`` sys-modules-wipe
   state leak (Phase-17 lesson).
2. **Behavior of each facade.** Each helper has subtle ordering /
   fallback semantics:
   - ``normalize_camera_recording_settings``: defensively type-coerces
     input to dict and layers ``normalize_bool_setting`` on the single
     ``continuous`` flag.
   - ``normalize_camera_ptz_settings``: protocol allowlist + four
     clamped integer fields with nested ``_int(value, default, lo, hi)``
     helper.
   - ``_normalize_camera_sound_settings``: rebuilds per-class rule list
     from raw sound config, clamping confidence_threshold (0.1-1.0)
     and cooldown_seconds (>=5.0), plus 6 per-rule booleans + 4
     time-window optional strings.
   - ``_migrate_legacy_camera_motion``: pops 3 legacy fields and,
     when the legacy enabled flag was False, disables
     ``monitor_motion`` on every zone + ``enabled`` on the motion
     object_rule.
3. **Top-level preload pattern.** ``import app.main`` BEFORE
   ``import app.recording_settings`` at module top -- same pattern as
   Phase-16 / 17 / 18 tests. Without this, pytest collection
   triggers the circular-import gate at ``app.recording_settings``
   load time (its top has ``import app.main as main`` for the Pool C
   reach sites).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest  # noqa: E402  -- used below

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Top-level lazy-ordered preloads to break the Phase-19 circular-import gate
# (same pattern as tests/test_auth_gates.py Phase-16, test_config_facades.py
# Phase-17, test_camera_config.py Phase-18). If we imported
# ``app.recording_settings`` FIRST, Python's fresh-load chain would run
# the top-of-file rebind ``from app.recording_settings import (...)`` inside
# ``app/main.py`` while ``app.recording_settings`` is still mid-load (only
# top imports done, function defs pending) -> ``cannot import name
# 'normalize_camera_recording_settings' from partially initialized module
# 'app.recording_settings'`` ImportError. Preloading ``app.main`` fully first
# populates ``sys.modules['app.main']`` so ``app.recording_settings``'s own
# ``import app.main as main`` returns the cached module rather than
# triggering a recursive fresh-load chain.
import app.main  # noqa: E402  -- must precede the import below
import app.recording_settings as recording_settings  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Pool A back-compat identity -- ``main.<name> is recording_settings.<name>``.
#    Re-resolve via sys.modules per Phase-17 lesson (defeats the
#    tests/test_api.py::_load_app() sys-modules-wipe state leak).
# ---------------------------------------------------------------------------


@pytest.fixture
def main():
    """Return the CURRENT ``app.main`` module instance. See the module
    docstring for why we cannot rely on the test file's module-level
    globals directly. Centralised as fixtures so the rationale lives
    in one comment rather than copy-pasted into 4 tests."""
    return sys.modules["app.main"]


@pytest.fixture
def current_recording_settings():
    """Return the CURRENT ``app.recording_settings`` module instance.
    See the ``main`` fixture above for the leak rationale."""
    return sys.modules["app.recording_settings"]


@pytest.fixture
def rs():
    """Convenience alias for ``current_recording_settings`` -- used by
    the behavior tests below to call ``rs.normalize_camera_recording_settings(...)``
    etc. without ``import app.recording_settings as rs`` boilerplate."""
    return sys.modules["app.recording_settings"]


# ---------------------------------------------------------------------------
# 2. Helpers -- isolate cross-module deps via monkeypatched helpers.
# ---------------------------------------------------------------------------


# Sentinel object used by ``_install_recording_dependencies`` to
# distinguish the caller's "use the helper's default DEFAULT_RULES"
# intent from the caller explicitly wanting an EMPTY list (a different
# -- but valid -- state for testing the ``if not default: continue``
# branch in ``_normalize_camera_sound_settings``). Cannot use ``None``
# because the empty-list case ``[]`` is a legitimate argument.
_DEFAULT_DEFAULTS_SENTINEL = object()


class _BoolBool:
    """Captures ``normalize_bool_setting(raw, default)`` semantics in a
    way the tests can drive: returns the second argument verbatim."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, raw, default):
        self.calls.append((raw, default))
        if raw is None:
            return default
        return bool(raw)


class _EmailRecipients:
    """Captures ``normalize_email_recipients(raw)`` semantics: pass-through
    identity for lists, default ``[]`` for non-lists."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, raw):
        self.calls.append(raw)
        return list(raw) if isinstance(raw, list) else []


def _install_recording_dependencies(monkeypatch, *, sound_classes=None, default_rules=_DEFAULT_DEFAULTS_SENTINEL):
    """Install hermetic stand-ins for the 4 cross-module deps reached by
    the recording-settings cluster:

    - ``normalize_bool_setting`` -- always succeeds (via ``_BoolBool``).
    - ``normalize_email_recipients`` -- list pass-through.
    - ``SOUND_CLASSES`` + ``DEFAULT_RULES`` -- injected dictionaries.

    Targets are intentionally ``app.recording_settings`` (NOT ``main``):
    app/recording_settings.py binds its dependencies at the top of the
    file via direct imports:
        from app.sound_detector import DEFAULT_RULES, SOUND_CLASSES
        from app.utils import normalize_bool_setting, normalize_email_recipients
    Function bodies consult the module's globals dict, so we must patch
    the bound names on ``app.recording_settings`` -- patching ``main.<name>``
    would NOT intercept the real call path.

    Returns ``(recording_settings_module, bs, er)``. ``default_rules``
    distinguishes ``None`` (caller wants the helper's default list) from
    ``[]`` (caller wants an EMPTY list -- important for testing the
    ``if not default: continue`` branch in ``_normalize_camera_sound_settings``).
    """
    import app.recording_settings as recording_settings_module

    bs = _BoolBool()
    er = _EmailRecipients()
    monkeypatch.setattr(recording_settings_module, 'normalize_bool_setting', bs)
    monkeypatch.setattr(recording_settings_module, 'normalize_email_recipients', er)

    if sound_classes is None:
        sound_classes = {
            'siren': {'label': 'Siren'},
            'glass_break': {'label': 'Glass break'},
        }
    if default_rules is _DEFAULT_DEFAULTS_SENTINEL:
        default_rules = [
            {'class': 'siren', 'confidence_threshold': 0.7, 'cooldown_seconds': 30.0, 'record_on_detect': True},
            {'class': 'glass_break', 'confidence_threshold': 0.6, 'cooldown_seconds': 60.0, 'record_on_detect': True},
        ]
    monkeypatch.setattr(recording_settings_module, 'SOUND_CLASSES', sound_classes)
    monkeypatch.setattr(recording_settings_module, 'DEFAULT_RULES', default_rules)

    return recording_settings_module, bs, er


# -- normalize_camera_recording_settings ---------------------------------

def test_normalize_camera_recording_settings_defaults_continuous_false_when_input_not_dict(rs):
    """Non-dict input (None, str, list) collapses to ``{'continuous': False}``
    without raising -- exercising the ``isinstance(settings, dict)`` guard."""
    assert rs.normalize_camera_recording_settings(None) == {'continuous': False}
    assert rs.normalize_camera_recording_settings('hi') == {'continuous': False}
    assert rs.normalize_camera_recording_settings([1, 2]) == {'continuous': False}


def test_normalize_camera_recording_settings_passes_continuous_through_normalize_bool(monkeypatch, rs):
    """The ``continuous`` field is routed through ``app.recording_settings.normalize_bool_setting``
    (top-of-file bound from ``app.utils``) -- verified by stubbing
    ``normalize_bool_setting`` on the recording_settings module and reading
    back its captured calls."""
    _rs, bs, _er = _install_recording_dependencies(monkeypatch)

    out = rs.normalize_camera_recording_settings({'continuous': 'yes'})
    assert out == {'continuous': True}
    # The bs stub was called once with (raw='yes', default=False).
    assert bs.calls == [('yes', False)]


# -- normalize_camera_ptz_settings --------------------------------------

def test_normalize_camera_ptz_settings_defaults_when_input_not_dict(rs):
    """Non-dict input collapses to the canonical PTZ defaults:
    enabled=False, protocol=onvif, http_port=80, port=6060, address=1, speed=5,
    step_duration=0.4."""
    assert rs.normalize_camera_ptz_settings(None) == {
        'enabled': False,
        'protocol': 'onvif',
        'http_port': 80,
        'port': 6060,
        'address': 1,
        'speed': 5,
        'step_duration': 0.4,
    }


def test_normalize_camera_ptz_settings_protocol_allowlist_falls_back_to_onvif(rs):
    """``tcp_pelcod`` and ``onvif`` are accepted (lowercased). Anything else
    silently falls back to ``onvif`` -- the protocol allowlist guard."""
    out_pelcod = rs.normalize_camera_ptz_settings({'protocol': 'TCP_PELCOD'})
    assert out_pelcod['protocol'] == 'tcp_pelcod'

    out_unknown = rs.normalize_camera_ptz_settings({'protocol': 'webrtc'})
    assert out_unknown['protocol'] == 'onvif'


def test_normalize_camera_ptz_settings_clamps_integer_fields(rs):
    """``port`` / ``http_port`` reject floats outside [1, 65535], ``address``
    rejects > 255, ``speed`` rejects > 8. ``_int`` returns ``default``
    on TypeError/ValueError (non-numeric input).

    Note: the source ``_int(value, default, lo, hi)`` uses ``value or default``
    which treats ``0`` as missing -- so clamping tests below use values > 0.
    A separate test pins the falsy-zero quirk for visibility.
    """
    out = rs.normalize_camera_ptz_settings({
        'http_port': 99999,   # clamps to 65535
        'port': 99999,        # clamps to 65535
        'address': 300,       # clamps to 255
        'speed': 99,          # clamps to 8
    })
    assert out['http_port'] == 65535
    assert out['port'] == 65535
    assert out['address'] == 255
    assert out['speed'] == 8


def test_normalize_camera_ptz_settings_falls_back_on_non_numeric(rs):
    """Non-numeric input to ``_int`` triggers TypeError/ValueError, which the
    helper catches and returns ``default``."""
    out = rs.normalize_camera_ptz_settings({
        'http_port': 'not-a-number',
        'port': None,
    })
    assert out['http_port'] == 80  # default
    assert out['port'] == 6060  # default


# -- _normalize_camera_sound_settings ----------------------------------

def test_normalize_camera_sound_settings_collapses_non_dict_input(rs):
    """Non-dict input collapses to ``{'enabled': False, 'rules': []}``."""
    assert rs._normalize_camera_sound_settings(None) == {'enabled': False, 'rules': []}
    assert rs._normalize_camera_sound_settings('hi') == {'enabled': False, 'rules': []}
    assert rs._normalize_camera_sound_settings(['list', 'of', 'rules']) == {'enabled': False, 'rules': []}


def test_normalize_camera_sound_settings_filters_unknown_rule_classes(monkeypatch, rs):
    """Rules whose ``class`` isn't in ``main.SOUND_CLASSES`` are dropped --
    only known sound classes survive the rebuild."""
    _rs, _bs, _er = _install_recording_dependencies(
        monkeypatch,
        sound_classes={'siren': {'label': 'Siren'}},
        default_rules=[{'class': 'siren', 'confidence_threshold': 0.7, 'cooldown_seconds': 30.0}],
    )

    out = rs._normalize_camera_sound_settings({
        'enabled': True,
        'rules': [
            {'class': 'siren', 'confidence_threshold': 0.8},
            {'class': 'mystery_class', 'confidence_threshold': 0.99},
        ],
    })
    assert out['enabled'] is True
    assert [r['class'] for r in out['rules']] == ['siren']


def test_normalize_camera_sound_settings_clamps_confidence_and_cooldown(monkeypatch, rs):
    """``confidence_threshold`` clamps to [0.1, 1.0]. ``cooldown_seconds``
    clamps to >= 5.0 -- exercising the dual ``max(min(...))`` + numeric
    try/except fallback."""
    _install_recording_dependencies(monkeypatch)

    out = rs._normalize_camera_sound_settings({
        'rules': [{'class': 'siren', 'confidence_threshold': 99.0, 'cooldown_seconds': 1.0}],
    })
    rule = out['rules'][0]
    assert rule['confidence_threshold'] == 1.0
    assert rule['cooldown_seconds'] == 5.0

    out_lo = rs._normalize_camera_sound_settings({
        'rules': [{'class': 'siren', 'confidence_threshold': 0.001, 'cooldown_seconds': 'not-a-float'}],
    })
    rule_lo = out_lo['rules'][0]
    assert rule_lo['confidence_threshold'] == 0.1
    # The expectation here is: cooldown_seconds parsing fail falls back
    # to ``default['cooldown_seconds']`` (30.0 in our stub).
    assert rule_lo['cooldown_seconds'] == 30.0


def test_normalize_camera_sound_settings_passes_through_active_window_strings(monkeypatch, rs):
    """``active_start``/``active_end``/``notify_start``/``notify_end`` are
    strip-then-trimmed; empty strings become ``None`` (semantic gap so UI
    knows "no time window" from "user typed whitespace"). Bonus: name
    defaults to ``SOUND_CLASSES[cls]['label']`` when raw name is missing.
    """
    _install_recording_dependencies(monkeypatch)

    out = rs._normalize_camera_sound_settings({
        'rules': [{'class': 'siren', 'active_start': '  08:00  ', 'active_end': '', 'notify_start': '18:00'}],
    })
    rule = out['rules'][0]
    assert rule['active_start'] == '08:00'
    assert rule['active_end'] is None
    assert rule['notify_start'] == '18:00'
    assert rule['name'] == 'Siren'


def test_normalize_camera_sound_settings_drops_rules_when_class_lacks_default(monkeypatch, rs):
    """If a saved rule's class doesn't have a matching default in
    ``DEFAULT_RULES`` (race with config schema updates), the rule is
    dropped entirely -- prevents stale class configs from polluting the
    UI.
    """
    # SOUND_CLASSES has 'siren', but DEFAULT_RULES is empty -- so the
    # matching default_by_class lookup returns None and the rule is skipped.
    _install_recording_dependencies(
        monkeypatch,
        sound_classes={'siren': {'label': 'Siren'}},
        default_rules=[],
    )

    out = rs._normalize_camera_sound_settings({
        'enabled': True,
        'rules': [{'class': 'siren', 'confidence_threshold': 0.8}],
    })
    assert out == {'enabled': True, 'rules': []}


# -- _migrate_legacy_camera_motion --------------------------------------

def test_migrate_legacy_camera_motion_pops_all_three_legacy_fields(rs):
    """``detection.motion`` / ``detection.motion_enabled`` / ``detection.motion_email_enabled``
    are popped regardless of the legacy ``enabled`` flag -- verifying the
    field-cleanup contract.
    """
    detection = {
        'motion': {'enabled': True},
        'motion_enabled': True,
        'motion_email_enabled': True,
        'zones': [],
    }
    rs._migrate_legacy_camera_motion(detection)
    assert 'motion' not in detection
    assert 'motion_enabled' not in detection
    assert 'motion_email_enabled' not in detection


def test_migrate_legacy_camera_motion_no_op_when_legacy_enabled(rs):
    """When the legacy motion switch was True (or absent), the migration
    leaves zones + object_rules untouched -- preserving existing
    motion-on configuration across the schema upgrade.
    """
    detection = {
        'motion': {'enabled': True},
        'zones': [{'monitor_motion': True, 'object_rules': [{'label': 'motion', 'enabled': True}]}],
    }
    snapshot = {'zones': [{'monitor_motion': True, 'object_rules': [{'label': 'motion', 'enabled': True}]}]}
    rs._migrate_legacy_camera_motion(detection)
    assert detection['zones'] == snapshot['zones']


def test_migrate_legacy_camera_motion_disables_zone_motion_when_legacy_off(monkeypatch, rs):
    """When the legacy motion switch was False (via the legacy dict),
    every zone's ``monitor_motion`` flips to False and every motion
    ``object_rule`` is disabled."""
    _install_recording_dependencies(monkeypatch)
    detection = {
        'motion': {'enabled': False},
        'zones': [
            {
                'monitor_motion': True,
                'object_rules': [
                    {'label': 'motion', 'enabled': True},
                    {'label': 'person', 'enabled': True},
                ],
            },
            {'monitor_motion': True, 'object_rules': []},
        ],
    }
    rs._migrate_legacy_camera_motion(detection)
    assert detection['zones'][0]['monitor_motion'] is False
    assert detection['zones'][0]['object_rules'][0]['enabled'] is False  # motion rule
    assert detection['zones'][0]['object_rules'][1]['enabled'] is True   # person untouched
    assert detection['zones'][1]['monitor_motion'] is False


def test_migrate_legacy_camera_motion_supports_flat_motion_enabled_field(monkeypatch, rs):
    """Older configs use the flat ``motion_enabled`` boolean instead of
    the ``detection.motion`` dict -- set it False and verify the same
    zone-level disable propagates.
    """
    _install_recording_dependencies(monkeypatch)
    detection = {
        'motion_enabled': False,
        'zones': [
            {'monitor_motion': True, 'object_rules': [{'label': 'motion', 'enabled': True}]},
        ],
    }
    rs._migrate_legacy_camera_motion(detection)
    assert 'motion_enabled' not in detection, 'flat motion_enabled must be popped'
    assert detection['zones'][0]['monitor_motion'] is False
    assert detection['zones'][0]['object_rules'][0]['enabled'] is False
