"""Phase-21 integration tests for ``app/zone_schema.py``.

Phase-21 extracted the 7 zone/schema normalizers (``normalize_label_list``,
``normalize_zone_object_rules``, ``zone_motion_min_confidence``,
``normalize_zone_point``, ``rectangle_zone_points``, ``zone_bounds``,
``normalize_monitoring_zones``) PLUS the module-private
``_LABEL_ALIASES`` annotation assignment from ``app/main.py`` into
``app/zone_schema.py`` using the hybrid-pattern template (same as
Phase-16 ``app/auth_gates.py``, Phase-17 ``app/config_facades.py``,
Phase-18 ``app/camera_config.py``, Phase-19 ``app/recording_settings.py``,
Phase-20 ``app/ai_settings.py``).

Internal ``main.py`` callers (``validate_camera_settings`` L2537-2538,
``render_live_snapshot_svg`` L2025, ``detection_label_allowed_for_zone``
L799, ``filter_detections_for_camera_zones`` L815 + L911 + L919)
reference these as bare names inside function bodies; the top-of-file
Pool A rebind wires ``main.<name>`` (and ``main._LABEL_ALIASES``)
before any of those bodies evaluates.

Tests pin three contracts:

1. **Pool A back-compat identity.** The 8 Pool A rebinds (``_LABEL_ALIASES``
   + 7 cluster helpers) MUST wire ``main.<name>`` to the SAME
   function/object as ``app.zone_schema.<name>``. Re-resolved via
   ``sys.modules`` to defeat the ``tests/support.py::_load_app``
   sys-modules-wipe state leak (Phase-17 lesson).
2. **Behavior of each facade.** Each helper has subtle ordering /
   fallback semantics:
   - ``_LABEL_ALIASES``: dict of ``{'human': 'person', 'people':
     'person', 'pedestrian': 'person'}``.
   - ``normalize_label_list``: comma-separated string OR list input;
     applies ``_LABEL_ALIASES`` for canonicalization, dedupes, returns
     sorted-unique list.
   - ``normalize_zone_object_rules``: seeds from ``zone.object_rules``
     OR synthesizes from ``zone.object_labels``; clamps
     ``min_confidence`` to [0.0, 1.0] (TypeError/ValueError -> 0.5);
     clamps ``cooldown_seconds`` >= 0 (TypeError/ValueError -> 60);
     all bool/cooldown/email/push/4 time-window fields normalized.
   - ``zone_motion_min_confidence``: clamps min_confidence [0.0, 1.0],
     defaults to 0.45 when no motion rule enabled.
   - ``normalize_zone_point``: non-dict -> None; clamps x/y to [0.0, 1.0];
     TypeError -> None.
   - ``rectangle_zone_points``: 4-corner dict list, rounded 4 dp.
   - ``zone_bounds``: bounding rect from points (left, top, width, height),
     minimum 0.01 width/height.
   - ``normalize_monitoring_zones``: orchestrator; clamps x/y/width/height
     to [0.0, 1.0]; uses ``zone.points`` (>=3 points) OR falls back to
     ``rectangle_zone_points``; rebuilds ``object_rules``; folds legacy
     ``monitor_motion`` into motion rule.
3. **Top-level preload pattern.** ``import app.main`` BEFORE
   ``import app.zone_schema`` at module top. After Pool C elimination
   zone_schema.py no longer imports from app.main at the function level,
   but app.main still imports from zone_schema via Pool A rebinds so
   preloading app.main first prevents any residual circular-import issues
   during pytest collection.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest  # noqa: E402  -- used below

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Preload app.main before zone_schema to ensure all Pool A rebinds on
# app.main are wired before zone_schema's own top-level imports resolve.
# importlib.import_module is used here (rather than import statements) so
# that the side-effect-only loads do not trigger py/unused-import warnings.
importlib.import_module('app.main')  # noqa: E402  -- must precede the import below
importlib.import_module('app.zone_schema')  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Pool A back-compat identity -- ``main.<name> is zone_schema.<name>``.
#    Re-resolve via sys.modules per Phase-17 lesson (defeats the
#    tests/support.py::_load_app() sys-modules-wipe state leak).
# ---------------------------------------------------------------------------


@pytest.fixture
def main():
    """Return the CURRENT ``app.main`` module instance. See the module
    docstring for why we cannot rely on the test file's module-level
    globals directly. Centralised as fixtures so the rationale lives
    in one comment rather than copy-pasted into 8 tests."""
    return sys.modules["app.main"]


@pytest.fixture
def current_zone_schema():
    """Return the CURRENT ``app.zone_schema`` module instance. See
    the ``main`` fixture above for the leak rationale."""
    return sys.modules["app.zone_schema"]


@pytest.fixture
def zs():
    """Convenience alias for ``current_zone_schema`` -- used by the
    behavior tests below to call ``zs.normalize_monitoring_zones(...)``
    etc. without ``import app.zone_schema as zs`` boilerplate."""
    return sys.modules["app.zone_schema"]


# ---------------------------------------------------------------------------
# 2. Helpers -- isolate cross-module deps via monkeypatched helpers.
# ---------------------------------------------------------------------------


class _BoolBool:
    """Captures ``normalize_bool_setting(raw, default)`` semantics:
    ``None`` -> default; otherwise return ``bool(raw)``."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, raw, default):
        self.calls.append((raw, default))
        if raw is None:
            return default
        return bool(raw)


class _EmailRecipients:
    """Captures ``normalize_email_recipients(raw)``: list pass-through;
    non-list -> ``[]``."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, raw):
        self.calls.append(raw)
        return list(raw) if isinstance(raw, list) else []


class _CameraIdStub:
    """Captures ``normalize_camera_id(value, fallback)`` semantics:
    passthrough if non-empty, else fallback."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, value, fallback):
        self.calls.append((value, fallback))
        out = str(value or '').strip()
        return out or fallback


def _install_zone_dependencies(
    monkeypatch,
    *,
    normalize_bool_setting=None,
    normalize_email_recipients=None,
    normalize_camera_id=None,
):
    """Install hermetic stand-ins for the 3 module-level names in
    ``app.zone_schema`` that the cluster calls at runtime:
    - ``zone_schema.normalize_bool_setting`` (called 5x in normalize_zone_object_rules)
    - ``zone_schema.normalize_email_recipients`` (called 1x)
    - ``zone_schema.normalize_camera_id`` (called 1x in normalize_monitoring_zones)

    Patches on the zone_schema module namespace (the true home after Pool C
    elimination) so monkeypatch intercepts the actual call sites.
    """
    zs = sys.modules['app.zone_schema']

    if normalize_bool_setting is None:
        normalize_bool_setting = _BoolBool()
    if normalize_email_recipients is None:
        normalize_email_recipients = _EmailRecipients()
    if normalize_camera_id is None:
        normalize_camera_id = _CameraIdStub()

    monkeypatch.setattr(zs, 'normalize_bool_setting', normalize_bool_setting)
    monkeypatch.setattr(zs, 'normalize_email_recipients', normalize_email_recipients)
    monkeypatch.setattr(zs, 'normalize_camera_id', normalize_camera_id)


# -- _LABEL_ALIASES ---------------------------------------------------------

def test_label_aliases_contains_expected_human_aliases(zs):
    """Pin the 3 known aliases -- they exist specifically to canonicalize
    common alternate human-form labels into the canonical ``person``."""
    assert zs._LABEL_ALIASES == {
        'human': 'person',
        'people': 'person',
        'pedestrian': 'person',
    }


# -- normalize_label_list --------------------------------------------------

def test_normalize_label_list_returns_empty_for_non_string_non_list(zs):
    """Anything other than str or list collapses to ``[]``."""
    assert zs.normalize_label_list(None) == []
    assert zs.normalize_label_list(42) == []
    assert zs.normalize_label_list({'any': 'dict'}) == []


def test_normalize_label_list_canonicalizes_human_via_aliases(zs):
    """``human`` / ``people`` / ``pedestrian`` all canonicalize to
    ``person`` (via ``_LABEL_ALIASES``)."""
    assert zs.normalize_label_list(['human']) == ['person']
    assert zs.normalize_label_list(['people', 'pedestrian']) == ['person']
    # standalone non-alias label
    assert zs.normalize_label_list(['car']) == ['car']


def test_normalize_label_list_splits_comma_separated_string(zs):
    """String input is split on commas. The implementation preserves
    ENCOUNTER order (via the ``seen`` set + list append) rather than
    re-sorting alphabetically -- this matches the orchestrator's
    expectation that label order mirrors source input (e.g. zone list)."""
    assert zs.normalize_label_list('person,car,person') == ['person', 'car']


def test_normalize_label_list_dedupes_and_lowercases(zs):
    """Duplicates collapse + lowercase normalization + aliasing."""
    assert zs.normalize_label_list(['PERSON', 'person', 'human']) == ['person']
    assert zs.normalize_label_list([' CAR ', 'car']) == ['car']


# -- normalize_zone_point --------------------------------------------------

def test_normalize_zone_point_returns_none_for_non_dict(zs):
    """Non-dict input collapses to ``None``."""
    assert zs.normalize_zone_point(None) is None
    assert zs.normalize_zone_point([1, 2]) is None
    assert zs.normalize_zone_point('not-a-dict') is None


def test_normalize_zone_point_clamps_xy_to_unit_square(zs):
    """``x`` and ``y`` clamp to [0.0, 1.0]; out-of-range values clamped
    rather than rejected."""
    out = zs.normalize_zone_point({'x': 1.5, 'y': -0.7})
    assert out['x'] == 1.0
    assert out['y'] == 0.0


def test_normalize_zone_point_returns_none_for_non_numeric_input(zs):
    """TypeError/ValueError inside the float coercion -> ``None``."""
    assert zs.normalize_zone_point({'x': 'oops', 'y': 0.5}) is None


def test_normalize_zone_point_rounds_to_4_dp(zs):
    """Output rounds to 4 decimal places."""
    out = zs.normalize_zone_point({'x': 0.123456789, 'y': 0.5})
    assert out == {'x': 0.1235, 'y': 0.5}


# -- rectangle_zone_points -------------------------------------------------

def test_rectangle_zone_points_produces_4_corners_rounded(zs):
    """A rectangle produces 4 dicts: top-left, top-right, bottom-right,
    bottom-left, each with x/y rounded 4 dp."""
    pts = zs.rectangle_zone_points(0.1, 0.2, 0.3, 0.4)
    assert pts == [
        {'x': 0.1, 'y': 0.2},
        {'x': 0.4, 'y': 0.2},
        {'x': 0.4, 'y': 0.6},
        {'x': 0.1, 'y': 0.6},
    ]


# -- zone_bounds ------------------------------------------------------------

def test_zone_bounds_computes_left_top_width_height(zs):
    """``zone_bounds`` returns ``(left, top, width, height)`` with
    minimum 0.01 width/height (collapses degenerate single-point input).

    Float-precision: the source computes ``right - left`` directly
    (``0.4 - 0.1 = 0.30000000000000004`` in IEEE-754), so use
    ``pytest.approx`` to tolerate the FP drift while still pinning
    the structural shape of the result."""
    # 4 corners of a 0.3 x 0.4 rectangle at origin (0.1, 0.2)
    pts = zs.rectangle_zone_points(0.1, 0.2, 0.3, 0.4)
    left, top, width, height = zs.zone_bounds(pts)
    assert left == pytest.approx(0.1)
    assert top == pytest.approx(0.2)
    assert width == pytest.approx(0.3)
    assert height == pytest.approx(0.4)


def test_zone_bounds_collapses_to_minimum_01_when_points_coincident(zs):
    """Single point (or degenerate points) clamps width/height to a
    minimum of 0.01."""
    out = zs.zone_bounds([{'x': 0.5, 'y': 0.5}])
    assert out[2] == 0.01  # width
    assert out[3] == 0.01  # height


# -- zone_motion_min_confidence ---------------------------------------------

def test_zone_motion_min_confidence_returns_default_when_no_motion_rule(zs):
    """When no motion rule exists, returns 0.45 (the canonical default)."""
    zone = {'object_rules': [{'label': 'person', 'min_confidence': 0.9, 'enabled': True}]}
    assert zs.zone_motion_min_confidence(zone) == 0.45


def test_zone_motion_min_confidence_returns_rule_value_when_motion_enabled(zs):
    """When the motion rule IS in ``object_rules`` AND enabled, returns
    its ``min_confidence`` (clamped to [0.0, 1.0])."""
    zone = {
        'object_rules': [
            {'label': 'motion', 'min_confidence': 0.7, 'enabled': True},
        ],
    }
    assert zs.zone_motion_min_confidence(zone) == 0.7


def test_zone_motion_min_confidence_clamps_out_of_range_value(zs):
    """Out-of-range ``min_confidence`` values clamp to [0.0, 1.0]."""
    zone = {'object_rules': [{'label': 'motion', 'min_confidence': 1.5, 'enabled': True}]}
    assert zs.zone_motion_min_confidence(zone) == 1.0


def test_zone_motion_min_confidence_skips_disabled_motion_rule(zs):
    """If the motion rule exists but is disabled, returns 0.45 (default)
    -- the rule's enabled flag drives the return."""
    zone = {'object_rules': [{'label': 'motion', 'min_confidence': 0.9, 'enabled': False}]}
    assert zs.zone_motion_min_confidence(zone) == 0.45


def test_zone_motion_min_confidence_handles_type_error_gracefully(zs):
    """Non-numeric ``min_confidence`` -> 0.45 fallback."""
    zone = {'object_rules': [{'label': 'motion', 'min_confidence': 'oops', 'enabled': True}]}
    assert zs.zone_motion_min_confidence(zone) == 0.45


def test_zone_motion_max_confidence_defaults_clamps_and_skips_disabled(zs):
    """``zone_motion_max_confidence`` returns the enabled motion rule's upper
    bound, defaulting to 1.0 (no cap) when absent/invalid/disabled/missing,
    and clamps to [0.0, 1.0]."""
    # No motion rule -> default no-cap.
    assert zs.zone_motion_max_confidence({'object_rules': [{'label': 'person', 'max_confidence': 0.5, 'enabled': True}]}) == 1.0
    # Enabled motion rule -> its value.
    assert zs.zone_motion_max_confidence({'object_rules': [{'label': 'motion', 'max_confidence': 0.7, 'enabled': True}]}) == 0.7
    # Absent max on the motion rule -> 1.0.
    assert zs.zone_motion_max_confidence({'object_rules': [{'label': 'motion', 'min_confidence': 0.3, 'enabled': True}]}) == 1.0
    # Out of range clamps.
    assert zs.zone_motion_max_confidence({'object_rules': [{'label': 'motion', 'max_confidence': 5.0, 'enabled': True}]}) == 1.0
    # Disabled motion rule -> default.
    assert zs.zone_motion_max_confidence({'object_rules': [{'label': 'motion', 'max_confidence': 0.4, 'enabled': False}]}) == 1.0
    # Non-numeric -> fallback.
    assert zs.zone_motion_max_confidence({'object_rules': [{'label': 'motion', 'max_confidence': 'oops', 'enabled': True}]}) == 1.0


# -- normalize_zone_object_rules -------------------------------------------

def test_normalize_zone_object_rules_seeds_from_object_rules_list(monkeypatch, zs):
    """When ``zone.object_rules`` is a list, use it as source. Each rule
    is normalized (label via _LABEL_ALIASES, min_confidence clamp,
    bool/email/push via main.*, 4 time-window optionals)."""
    _install_zone_dependencies(monkeypatch)
    zone = {'object_rules': [
        {'label': 'person', 'min_confidence': 0.6, 'cooldown_seconds': 30,
         'email_recipients': ['a@example.com']},
    ]}
    rules = zs.normalize_zone_object_rules(zone)
    assert len(rules) == 1
    rule = rules[0]
    assert rule['label'] == 'person'
    assert rule['min_confidence'] == 0.6
    assert rule['cooldown_seconds'] == 30
    assert rule['email_recipients'] == ['a@example.com']


def test_normalize_zone_object_rules_synthesizes_from_object_labels_when_no_rules(monkeypatch, zs):
    """If ``object_rules`` is missing, synthesize 1-row rules from
    ``object_labels`` (the older UI-driven schema)."""
    _install_zone_dependencies(monkeypatch)
    zone = {'object_labels': ['human', 'car']}  # 'human' -> 'person' via alias
    rules = zs.normalize_zone_object_rules(zone)
    assert [r['label'] for r in rules] == ['person', 'car']
    # All synthesized rules use defaults.
    assert all(r['min_confidence'] == 0.5 for r in rules)
    assert all(r['cooldown_seconds'] == 60 for r in rules)


def test_normalize_zone_object_rules_clamps_min_confidence_and_cooldown(monkeypatch, zs):
    """TypeError/ValueError -> defaults (0.5 / 60); out-of-range clamps."""
    _install_zone_dependencies(monkeypatch)
    zone = {'object_rules': [
        {'label': 'person', 'min_confidence': 99.0, 'cooldown_seconds': -5},
        {'label': 'car', 'min_confidence': 'oops', 'cooldown_seconds': 'no'},
    ]}
    rules = zs.normalize_zone_object_rules(zone)
    # First: clamps both to limit/default
    assert rules[0]['min_confidence'] == 1.0  # 99.0 -> 1.0
    assert rules[0]['cooldown_seconds'] == 0  # -5 -> 0
    # Second: TypeError -> defaults
    assert rules[1]['min_confidence'] == 0.5
    assert rules[1]['cooldown_seconds'] == 60


def test_normalize_zone_object_rules_motion_defaults_to_045_not_05(monkeypatch, zs):
    """A motion rule without ``min_confidence`` defaults to 0.45 (its
    canonical pixel-diff threshold, matching ``zone_motion_min_confidence``
    and the frontend default), while object classes keep the 0.5 default.
    All three axes (detection / recording / alerting) must gate a motion
    rule at the same number, so the schema default has to agree with the
    runtime fallbacks."""
    _install_zone_dependencies(monkeypatch)
    zone = {'object_rules': [
        {'label': 'motion'},
        {'label': 'person'},
        {'label': 'motion', 'min_confidence': 'oops'},
    ]}
    rules = zs.normalize_zone_object_rules(zone)
    by_label = {rule['label']: rule for rule in rules}
    assert by_label['motion']['min_confidence'] == 0.45
    assert by_label['person']['min_confidence'] == 0.5
    # 'motion' dedupes to a single rule, and its bad value still falls back
    # to the motion default.
    assert len([r for r in rules if r['label'] == 'motion']) == 1


def test_normalize_zone_object_rules_max_confidence_defaults_and_clamps(monkeypatch, zs):
    """``max_confidence`` defaults to 1.0 (no upper limit), clamps to [0, 1],
    falls back to 1.0 on bad input, and is never allowed below
    ``min_confidence`` (an empty window is raised up to ``min``)."""
    _install_zone_dependencies(monkeypatch)
    zone = {'object_rules': [
        {'label': 'person'},                                            # absent -> 1.0
        {'label': 'car', 'max_confidence': 0.6},                        # explicit in range
        {'label': 'dog', 'max_confidence': 5.0},                        # clamps to 1.0
        {'label': 'cat', 'max_confidence': 'oops'},                     # bad -> 1.0
        {'label': 'bus', 'min_confidence': 0.8, 'max_confidence': 0.3}, # max < min -> raised to min
    ]}
    rules = {r['label']: r for r in zs.normalize_zone_object_rules(zone)}
    assert rules['person']['max_confidence'] == 1.0
    assert rules['car']['max_confidence'] == 0.6
    assert rules['dog']['max_confidence'] == 1.0
    assert rules['cat']['max_confidence'] == 1.0
    assert rules['bus']['max_confidence'] == 0.8  # never below min_confidence


def test_normalize_zone_object_rules_dedupes_by_label(monkeypatch, zs):
    """Duplicate labels (case-insensitive, after aliasing) collapse to
    one rule -- the first occurrence wins."""
    _install_zone_dependencies(monkeypatch)
    zone = {'object_rules': [
        {'label': 'PERSON', 'min_confidence': 0.6},
        {'label': 'human', 'min_confidence': 0.9},  # dups w/ above via alias
    ]}
    rules = zs.normalize_zone_object_rules(zone)
    labels = [r['label'] for r in rules]
    assert labels == ['person']


def test_normalize_zone_object_rules_drops_non_dict_rules(monkeypatch, zs):
    """Non-dict entries in ``zone.object_rules`` are silently skipped."""
    _install_zone_dependencies(monkeypatch)
    zone = {'object_rules': [
        'not-a-dict',
        {'label': 'person'},
    ]}
    rules = zs.normalize_zone_object_rules(zone)
    assert [r['label'] for r in rules] == ['person']


# -- normalize_monitoring_zones --------------------------------------------

def test_normalize_monitoring_zones_returns_empty_for_non_list(monkeypatch, zs):
    """Non-list input collapses to ``[]`` -- the orchestrator's defensive
    branch."""
    _install_zone_dependencies(monkeypatch)
    assert zs.normalize_monitoring_zones(None) == []
    assert zs.normalize_monitoring_zones('not-a-list') == []
    assert zs.normalize_monitoring_zones({}) == []


def test_normalize_monitoring_zones_skips_non_dict_zones(monkeypatch, zs):
    """Non-dict entries inside the list are silently skipped."""
    _install_zone_dependencies(monkeypatch)
    assert zs.normalize_monitoring_zones(['not-a-dict', 42]) == []


def test_normalize_monitoring_zones_clamps_xy_wh(monkeypatch, zs):
    """All 4 spatial fields (x, y, width, height) clamp to [0.0, 1.0];
    width/height also have a min of 0.01."""
    _install_zone_dependencies(monkeypatch)
    zones = zs.normalize_monitoring_zones([{
        'x': 1.5, 'y': -0.5, 'width': 99.0, 'height': 0.0001,
    }])
    assert zones[0]['x'] == 1.0
    assert zones[0]['y'] == 0.0
    assert zones[0]['width'] == 0.01  # clamped from 0.0001 to min
    assert zones[0]['height'] == 0.01  # clamped from 99.0


def test_normalize_monitoring_zones_falls_back_to_rectangle_when_lt3_points(monkeypatch, zs):
    """When ``zone.points`` has fewer than 3 entries, fall back to
    ``rectangle_zone_points(x, y, width, height)``."""
    _install_zone_dependencies(monkeypatch)
    zones = zs.normalize_monitoring_zones([{
        'x': 0.2, 'y': 0.3, 'width': 0.4, 'height': 0.5,
        'points': [{'x': 0.2, 'y': 0.3}, {'x': 0.3, 'y': 0.4}],
    }])
    assert len(zones) == 1
    # The fallback rectangle has 4 corners -> len(points) == 4
    assert len(zones[0]['points']) == 4


def test_normalize_monitoring_zones_folds_legacy_monitor_motion_into_rule(monkeypatch, zs):
    """When ``zone.monitor_motion`` is True but no motion rule exists
    in ``object_rules``, a default motion rule is inserted at position 0.
    """
    _install_zone_dependencies(monkeypatch)
    zones = zs.normalize_monitoring_zones([{
        'monitor_motion': True, 'object_rules': [{'label': 'person'}],
    }])
    rules = zones[0]['object_rules']
    assert rules[0]['label'] == 'motion'  # inserted
    assert rules[0]['enabled'] is True
    assert zones[0]['monitor_motion'] is True


def test_normalize_monitoring_zones_does_not_insert_motion_when_already_present(monkeypatch, zs):
    """If a motion rule already exists in object_rules, DO NOT insert
    another -- even when ``monitor_motion`` is True."""
    _install_zone_dependencies(monkeypatch)
    zones = zs.normalize_monitoring_zones([{
        'monitor_motion': True,
        'object_rules': [
            {'label': 'motion', 'enabled': True, 'min_confidence': 0.5},
        ],
    }])
    motion_rules = [r for r in zones[0]['object_rules'] if r['label'] == 'motion']
    assert len(motion_rules) == 1  # only the original


def test_normalize_monitoring_zones_derives_monitor_motion_from_enabled_rule(monkeypatch, zs):
    """After the rebuild, ``monitor_motion`` reflects whether any motion
    rule with ``enabled=True`` exists -- not just the legacy field."""
    _install_zone_dependencies(monkeypatch)
    # Legacy monitor_motion=False (disabled by user) but a disabled motion
    # rule was added by migration -> final monitor_motion should be False.
    zones = zs.normalize_monitoring_zones([{
        'monitor_motion': False,
        'object_rules': [{'label': 'motion', 'enabled': False}],
    }])
    assert zones[0]['monitor_motion'] is False


# ---------------------------------------------------------------------------
# Umbrella label groups: canonical_label / label_matches / detection_label_in_allowed
# ---------------------------------------------------------------------------

def test_canonical_label_applies_aliases_and_lowercases(zs):
    assert zs.canonical_label('  Human ') == 'person'
    assert zs.canonical_label('CAT') == 'cat'
    assert zs.canonical_label(None) == ''


def test_label_matches_direct_and_alias(zs):
    assert zs.label_matches('cat', 'cat') is True
    assert zs.label_matches('human', 'person') is True
    assert zs.label_matches('cat', 'dog') is False


def test_label_matches_group_expands_only_on_configured_side(zs):
    # 'animal'/'pet' groups match member detections...
    assert zs.label_matches('cat', 'animal') is True
    assert zs.label_matches('dog', 'animal') is True
    assert zs.label_matches('bird', 'pet') is True
    # ...but a person is not an animal.
    assert zs.label_matches('person', 'animal') is False
    # Group expansion is one-directional: a detection that happens to be named
    # like a group never matches a concrete configured label.
    assert zs.label_matches('animal', 'cat') is False


def test_label_matches_empty_inputs_are_false(zs):
    assert zs.label_matches('', 'cat') is False
    assert zs.label_matches('cat', '') is False


def test_detection_label_in_allowed_direct_group_and_miss(zs):
    assert zs.detection_label_in_allowed('cat', {'cat', 'car'}) is True
    assert zs.detection_label_in_allowed('cat', {'animal'}) is True
    assert zs.detection_label_in_allowed('dog', {'pet'}) is True
    assert zs.detection_label_in_allowed('person', {'animal', 'pet'}) is False
    assert zs.detection_label_in_allowed('', {'animal'}) is False


def test_normalize_label_list_preserves_group_names(zs):
    # Group names are not aliases, so they pass through normalization unchanged
    # and can be stored on a zone/rule like any other configured label.
    assert 'animal' in zs.normalize_label_list('animal, cat')
    assert 'pet' in zs.normalize_label_list(['Pet'])
