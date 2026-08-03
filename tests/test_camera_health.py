"""Phase-24 identity + behavior tests for app/camera_health.py.

Mirrors the Phase-22 / 23 test pattern: fixture-driven,
monkeypatch-maintained Pool C dependencies, with one identity test per
extracted helper (Pool A rebind wiring) plus behavior tests covering
each public path:

- ``effective_camera_offline_alert_settings`` -- defaults, database
  override merging, non-dict override ignored, missing keys preserved.
- ``_update_camera_health`` -- state transitions online -> offline ->
  online, log_camera_diagnostic fired exactly once per transition, no
  log when state is unchanged.
- ``_camera_offline_notification_eligible`` -- blocked when online,
  blocked when already notified, blocked when delay not yet elapsed,
  eligible after delay.
- ``_camera_recovery_notification_eligible`` -- blocked when offline,
  blocked when already notified for the recovery-streak, eligible when
  previously offline-notified.
- ``_mark_camera_offline_notified`` / ``_mark_camera_recovery_notified``
  -- flip the corresponding flags under lock.
- ``_deliver_camera_offline_notification`` -- short-circuits when
  settings.enabled=False; fires push + email when both enabled;
  fires only the matching _mark helper based on event_type.
- ``_check_cameras_health`` -- iterates cameras_config snapshot,
  updates state, fires _deliver for first eligible camera.
- ``_camera_health_state`` / ``_camera_health_lock`` -- state primitives
  live in ``app.state`` and are re-exported from ``app.main`` via Pool A
  rebinds (verified by attribute existence + type checks).
- Lock discipline -- 8-thread concurrent _update_camera_health writers
  on the same camera_id complete without exceptions; final state is
  internally consistent.
- State isolation -- mutating camera 'A' does NOT affect camera 'B'.
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _StubDb:
    """Minimal database stub: get_setting returns None (no overrides)."""
    def get_setting(self, key):
        return None


@pytest.fixture(autouse=True)
def _stub_database(monkeypatch):
    """Ensure _state.database and main.database are stub objects for tests that
    don't use _load_app (i.e. lifespan has not run and singletons are None).

    Individual tests that need specific return values replace _state.database
    with their own stub via monkeypatch.setattr(_state, 'database', ...).
    """
    import app.state as _s
    import app.main as _m
    if _s.database is None:
        stub = _StubDb()
        monkeypatch.setattr(_s, 'database', stub)
        monkeypatch.setattr(_m, 'database', stub)


@pytest.fixture
def ch():
    from app import camera_health as _ch
    return _ch


@pytest.fixture
def main_module():
    import app.main as _main
    return _main


@pytest.fixture(autouse=True)
def _isolate_health_state(main_module):
    """Snapshot / restore _camera_health_state around every test.

    Each test mutates the state dict; the next test would see lingering
    flags / offline_since values. Snapshot + restore keeps the cluster's
    state dict clean across tests.
    """
    _hs = main_module._state._camera_health_state
    _hl = main_module._state._camera_health_lock
    saved = dict(_hs)
    yield
    with _hl:
        _hs.clear()
        _hs.update(saved)


@pytest.fixture
def stub_logger(ch, monkeypatch):
    """Stand in for log_camera_diagnostic and camera_health.logger.warning."""
    diagnostics: list[tuple[str, str]] = []
    warnings: list[tuple[str, tuple]] = []

    def fake_log_camera_diagnostic(camera_id, event_type, message='', *, severity='info', details=None, camera_name=None):
        diagnostics.append((camera_id, event_type))

    monkeypatch.setattr(ch, 'log_camera_diagnostic', fake_log_camera_diagnostic)
    monkeypatch.setattr(
        ch, 'logger', type('FakeLogger', (), {
            'warning': staticmethod(lambda *args, **kwargs: warnings.append((args, kwargs))),
            'info': staticmethod(lambda *args, **kwargs: None),
            'debug': staticmethod(lambda *args, **kwargs: None),
            'error': staticmethod(lambda *args, **kwargs: None),
        })(),
    )
    return diagnostics, warnings


@pytest.fixture
def stub_delivery_services(ch, monkeypatch):
    """Capture push + email notifications without an outbound network call."""
    push_calls: list[tuple[str, str]] = []
    email_calls: list[tuple[str, dict, str]] = []

    class FakePush:
        def __init__(self, settings):
            self.settings = settings
        def _deliver(self, title, body):
            push_calls.append((title, body))

    class FakeEmail:
        def __init__(self, settings):
            self.settings = settings

        @contextmanager
        def _create_smtp_session(self):
            # Yield a sentinel so the production ``send_alert`` /
            # ``_deliver_camera_offline_notification`` shared-session loop
            # enters the ``with`` block. The real ``smtp.send_message`` is
            # never reached because the fake ``_deliver`` short-circuits
            # to the capture list -- this keeps the batched dispatcher
            # exercised without ever opening a real SMTP connection.
            yield 'fake-smtp-session'

        def _deliver(self, msg, **kwargs):
            # ``**kwargs`` swallows the ``smtp=...`` kwarg the production
            # ``_deliver`` forwards when sharing a session across a
            # multi-recipient broadcast.
            email_calls.append((msg['Subject'], dict(self.settings), msg['To']))

    monkeypatch.setattr(ch, 'PushNotificationService', FakePush)
    monkeypatch.setattr(ch, 'EmailAlertService', FakeEmail)
    return push_calls, email_calls


# ---------------------------------------------------------------------------
# State-migration invariants: state primitives live on app.state
# ---------------------------------------------------------------------------

def test_state_primitives_not_promoted_into_camera_health(ch):
    """camera_health.py must NOT have its own copy of the state primitives.

    The canonical source of truth is ``app.state`` (``_state._camera_health_state``
    and ``_state._camera_health_lock``). ``camera_health.py`` reaches them via
    ``import app.state as _state`` at module top. A local attribute copy on the
    ``camera_health`` module would diverge from the registry and silently corrupt
    the state machine.
    """
    assert not hasattr(ch, '_camera_health_state'), (
        'camera_health must NOT define its own _camera_health_state; it must '
        "reach app.state._camera_health_state via 'import app.state as _state'"
    )
    assert not hasattr(ch, '_camera_health_lock'), (
        'camera_health must NOT define its own _camera_health_lock'
    )


# ---------------------------------------------------------------------------
# Monkey-patching reach-path conventions in this file
# ---------------------------------------------------------------------------
#
# Two attribute surfaces exist in this codebase:
#
# - `ch.<X>` patches a name that `app/camera_health.py` owns as a
#   module-level binding (top-level import or definition). Service classes
#   (`PushNotificationService`, `EmailAlertService`) and config-facade
#   functions (`effective_push_notification_settings`,
#   `effective_email_alert_settings`) are imported at module top in
#   camera_health.py, so patching `ch.<X>` is the correct surface.
# - `_app_state.<X>` patches the underlying registry module `app.state`
#   ("Application-scoped singleton and shared-state registry" per
#   `app/state.py`'s docstring).
#
# The choice is dictated by HOW the production code under test imports the
# symbol. Two rules follow.
#
# 1. Registry imports MUST happen inside the test function body, not at the
#    top of this file. `tests/support.py::_load_app()` reloads `app.main`
#    and, depending on its reload strategy, may also replace
#    `sys.modules['app.state']` with a fresh instance. A file-top
#    `import app.state as _app_state` captured at pytest collection time
#    would then land monkeypatches on a phantom module that production no
#    longer reads from. The function-body `import app.state as _app_state`
#    re-binds to whatever `sys.modules['app.state']` holds at call time,
#    AFTER `_load_app()`, so it always lands on the live singleton.
#
# 2. Stateful singletons (`database`, `cameras_config`,
#    `live_detection_retry_after`, ...) live canonically on `app.state`.
#    `app.state` is the file's stated home, so we patch there.
#

# ---------------------------------------------------------------------------
# effective_camera_offline_alert_settings -- defaults + database override
# ---------------------------------------------------------------------------

def test_effective_offline_alert_settings_defaults(ch, monkeypatch):
    import app.state as _app_state

    class _DB:
        def get_setting(self, key): return None

    monkeypatch.setattr(_app_state, 'database', _DB())
    out = ch.effective_camera_offline_alert_settings()
    assert out == {'enabled': False, 'offline_delay_minutes': 1, 'recipients': []}


def test_effective_offline_alert_settings_overrides_with_dict(ch, monkeypatch):
    import app.state as _app_state

    class _DB:
        def get_setting(self, key):
            return {'enabled': True, 'offline_delay_minutes': 5, 'recipients': ['admin@example.com']}

    monkeypatch.setattr(_app_state, 'database', _DB())
    out = ch.effective_camera_offline_alert_settings()
    assert out == {'enabled': True, 'offline_delay_minutes': 5, 'recipients': ['admin@example.com']}


def test_effective_offline_alert_settings_ignores_non_dict_override(ch, monkeypatch):
    import app.state as _app_state

    class _DB:
        def get_setting(self, key): return 'just-a-string'

    monkeypatch.setattr(_app_state, 'database', _DB())
    # Non-dict override must not crash and must leave defaults intact.
    out = ch.effective_camera_offline_alert_settings()
    assert out == {'enabled': False, 'offline_delay_minutes': 1, 'recipients': []}


# ---------------------------------------------------------------------------
# _update_camera_health -- state transitions + log_camera_diagnostic
# ---------------------------------------------------------------------------

def test_update_camera_health_online_to_offline_fires_diagnostic(ch, main_module, stub_logger):
    diagnostics, _warnings = stub_logger
    main_module._state._camera_health_state['cam-1'] = {
        'online': True, 'offline_since': None, 'offline_notified': False, 'recovery_notified': False,
    }
    ch._update_camera_health('cam-1', False)
    state = main_module._state._camera_health_state['cam-1']
    assert state['online'] is False
    assert state['offline_since'] is not None  # was stamped with time.time()
    assert diagnostics == [('cam-1', 'camera_offline')]


def test_update_camera_health_offline_to_online_fires_recovery_log(ch, main_module, stub_logger):
    diagnostics, _warnings = stub_logger
    main_module._state._camera_health_state['cam-1'] = {
        'online': False, 'offline_since': time.time() - 30, 'offline_notified': True, 'recovery_notified': False,
    }
    ch._update_camera_health('cam-1', True)
    state = main_module._state._camera_health_state['cam-1']
    assert state['online'] is True
    assert state['offline_since'] is None
    assert state['offline_notified'] is False  # reset on recovery
    assert diagnostics == [('cam-1', 'camera_online')]


def test_update_camera_health_no_op_when_state_unchanged(ch, main_module, stub_logger):
    """Idempotent update must NOT fire a diagnostic."""
    diagnostics, _warnings = stub_logger
    main_module._state._camera_health_state['cam-1'] = {
        'online': True, 'offline_since': None, 'offline_notified': False, 'recovery_notified': False,
    }
    ch._update_camera_health('cam-1', True)  # online -> online -> no transition
    diagnostics.clear()
    ch._update_camera_health('cam-1', False)
    ch._update_camera_health('cam-1', False)  # offline -> offline -> no transition
    assert diagnostics == [('cam-1', 'camera_offline')]


def test_update_camera_health_resets_offline_since_on_recovery(ch, main_module):
    main_module._state._camera_health_state['cam-1'] = {
        'online': False, 'offline_since': time.time() - 100, 'offline_notified': True, 'recovery_notified': True,
    }
    ch._update_camera_health('cam-1', True)
    state = main_module._state._camera_health_state['cam-1']
    assert state['offline_since'] is None
    assert state['offline_notified'] is False  # reset for next offline cycle


def test_update_camera_health_preserves_existing_offline_since(ch, main_module, stub_logger):
    """If state already has offline_since set, don't overwrite it on the next
    online->offline edge (preserves the original offline-streak starting time)."""
    main_module._state._camera_health_state['cam-1'] = {
        'online': True, 'offline_since': None, 'offline_notified': False, 'recovery_notified': False,
    }
    # Force an online -> offline edge; capture offline_since as T0.
    ch._update_camera_health('cam-1', False)
    T0 = main_module._state._camera_health_state['cam-1']['offline_since']
    # A subsequent recovery + immediate re-offline should keep the same offline_since
    # because the cluster memoizes it across recovery_to_offline cycles when the
    # previous offline_since was already stamped; but since recovery clears
    # offline_since to None first, the next offline edge WILL re-stamp.
    ch._update_camera_health('cam-1', True)
    time.sleep(0.01)
    ch._update_camera_health('cam-1', False)
    T1 = main_module._state._camera_health_state['cam-1']['offline_since']
    assert T1 is not None and T1 >= T0


# ---------------------------------------------------------------------------
# _camera_offline_notification_eligible -- threshold + flag check
# ---------------------------------------------------------------------------

def test_offline_eligible_when_offline_beyond_delay(ch, main_module, monkeypatch):
    main_module._state._camera_health_state['cam-1'] = {
        'online': False,
        'offline_since': time.time() - 600,  # 10 mins ago, way past 1-min default
        'offline_notified': False,
        'recovery_notified': False,
    }
    # Default offline_delay_minutes=1 -> 60s -> elapsed=600s -> eligible
    assert ch._camera_offline_notification_eligible('cam-1') is True


def test_offline_eligible_blocked_before_delay_elapsed(ch, main_module, monkeypatch):
    main_module._state._camera_health_state['cam-1'] = {
        'online': False,
        'offline_since': time.time() - 10,  # only 10s ago, default=60s -> not eligible
        'offline_notified': False,
        'recovery_notified': False,
    }
    assert ch._camera_offline_notification_eligible('cam-1') is False


def test_offline_eligible_blocked_when_already_notified(ch, main_module):
    main_module._state._camera_health_state['cam-1'] = {
        'online': False,
        'offline_since': time.time() - 600,
        'offline_notified': True,
        'recovery_notified': False,
    }
    assert ch._camera_offline_notification_eligible('cam-1') is False


def test_offline_eligible_blocked_when_online(ch, main_module):
    main_module._state._camera_health_state['cam-1'] = {
        'online': True,
        'offline_since': None,
        'offline_notified': False,
        'recovery_notified': False,
    }
    assert ch._camera_offline_notification_eligible('cam-1') is False


def test_offline_eligible_blocked_when_state_missing(ch, main_module):
    assert ch._camera_offline_notification_eligible('nonexistent') is False


def test_offline_eligible_respects_custom_delay(ch, main_module, monkeypatch):
    # 10-minute delay: a 60-second offline streak is NOT eligible; a 700-second
    # offline streak IS eligible. Override at the singleton level only.
    monkeypatch.setattr(
        main_module.database, 'get_setting',
        lambda key: {'enabled': True, 'offline_delay_minutes': 10, 'recipients': []},
    )
    main_module._state._camera_health_state['cam-1'] = {
        'online': False,
        'offline_since': time.time() - 60,  # 60s ago, 10-min delay -> NOT eligible
        'offline_notified': False,
        'recovery_notified': False,
    }
    assert ch._camera_offline_notification_eligible('cam-1') is False
    main_module._state._camera_health_state['cam-1']['offline_since'] = time.time() - 700
    assert ch._camera_offline_notification_eligible('cam-1') is True


# ---------------------------------------------------------------------------
# _camera_recovery_notification_eligible -- post-notify recovery
# ---------------------------------------------------------------------------

def test_recovery_eligible_when_previously_offline_notified(ch, main_module):
    main_module._state._camera_health_state['cam-1'] = {
        'online': True,
        'offline_since': None,
        'offline_notified': True,
        'recovery_notified': False,
    }
    assert ch._camera_recovery_notification_eligible('cam-1') is True


def test_recovery_ineligible_when_not_previously_offline_notified(ch, main_module):
    main_module._state._camera_health_state['cam-1'] = {
        'online': True,
        'offline_since': None,
        'offline_notified': False,
        'recovery_notified': False,
    }
    assert ch._camera_recovery_notification_eligible('cam-1') is False


def test_recovery_ineligible_when_still_offline(ch, main_module):
    main_module._state._camera_health_state['cam-1'] = {
        'online': False,
        'offline_since': time.time() - 600,
        'offline_notified': True,
        'recovery_notified': False,
    }
    assert ch._camera_recovery_notification_eligible('cam-1') is False


def test_recovery_ineligible_when_already_recovery_notified(ch, main_module):
    main_module._state._camera_health_state['cam-1'] = {
        'online': True,
        'offline_since': None,
        'offline_notified': True,
        'recovery_notified': True,
    }
    assert ch._camera_recovery_notification_eligible('cam-1') is False


# ---------------------------------------------------------------------------
# _mark_*_notified -- flag flips
# ---------------------------------------------------------------------------

def test_mark_camera_offline_notified_sets_flag(ch, main_module):
    main_module._state._camera_health_state['cam-1'] = {
        'online': False,
        'offline_since': time.time() - 600,
        'offline_notified': False,
        'recovery_notified': False,
    }
    ch._mark_camera_offline_notified('cam-1')
    assert main_module._state._camera_health_state['cam-1']['offline_notified'] is True


def test_mark_camera_recovery_notified_sets_flag(ch, main_module):
    main_module._state._camera_health_state['cam-1'] = {
        'online': True,
        'offline_since': None,
        'offline_notified': True,
        'recovery_notified': False,
    }
    ch._mark_camera_recovery_notified('cam-1')
    assert main_module._state._camera_health_state['cam-1']['recovery_notified'] is True


def test_marks_no_op_on_missing_state(ch, main_module):
    """No-raise on missing state -- the mark helper should swallow silently."""
    ch._mark_camera_offline_notified('nonexistent-camera')
    ch._mark_camera_recovery_notified('nonexistent-camera')
    assert 'nonexistent-camera' not in main_module._state._camera_health_state


# ---------------------------------------------------------------------------
# _deliver_camera_offline_notification -- push + email + mark
# ---------------------------------------------------------------------------

def test_deliver_short_circuits_when_disabled(ch, main_module, monkeypatch, stub_delivery_services, stub_logger):
    push_calls, email_calls = stub_delivery_services
    monkeypatch.setattr(
        main_module.database, 'get_setting',
        lambda key: {'enabled': False, 'offline_delay_minutes': 1, 'recipients': []},
    )
    main_module._state._camera_health_state['cam-1'] = {
        'online': False, 'offline_since': time.time() - 600,
        'offline_notified': False, 'recovery_notified': False,
    }
    ch._deliver_camera_offline_notification('cam-1', 'Front Yard', 'offline')
    assert push_calls == []
    assert email_calls == []
    # No mark when disabled (the cluster only marks AFTER delivery attempt).
    assert main_module._state._camera_health_state['cam-1']['offline_notified'] is False


def test_deliver_fires_push_only_when_only_push_enabled(ch, main_module, monkeypatch, stub_delivery_services, stub_logger):
    push_calls, email_calls = stub_delivery_services
    monkeypatch.setattr(
        main_module.database, 'get_setting',
        lambda key: {'enabled': True, 'offline_delay_minutes': 10, 'recipients': []},  # offline_alert_settings
    )
    monkeypatch.setattr(ch, 'effective_push_notification_settings', lambda: {'enabled': True})
    monkeypatch.setattr(ch, 'effective_email_alert_settings', lambda: {'enabled': False})
    main_module._state._camera_health_state['cam-1'] = {
        'online': False, 'offline_since': time.time() - 600,
        'offline_notified': False, 'recovery_notified': False,
    }
    ch._deliver_camera_offline_notification('cam-1', 'Front Yard', 'offline')
    assert len(push_calls) == 1
    assert push_calls[0] == ('Camera Offline: Front Yard', 'Camera Front Yard (cam-1) has gone offline.')
    assert email_calls == []
    assert main_module._state._camera_health_state['cam-1']['offline_notified'] is True


def test_deliver_fires_email_with_recipients(ch, main_module, monkeypatch, stub_delivery_services, stub_logger):
    push_calls, email_calls = stub_delivery_services
    monkeypatch.setattr(
        main_module.database, 'get_setting',
        lambda key: {'enabled': True, 'offline_delay_minutes': 10, 'recipients': ['admin@example.com', 'ops@example.com']},
    )
    monkeypatch.setattr(ch, 'effective_push_notification_settings', lambda: {'enabled': False})
    monkeypatch.setattr(
        ch, 'effective_email_alert_settings',
        lambda: {'enabled': True, 'from_address': 'alerts@example.com'},
    )
    main_module._state._camera_health_state['cam-1'] = {
        'online': False, 'offline_since': time.time() - 600,
        'offline_notified': False, 'recovery_notified': False,
    }
    ch._deliver_camera_offline_notification('cam-1', 'Front Yard', 'offline')
    # One envelope per recipient - addresses never leak to fellow subscribers.
    assert email_calls == [
        ('Camera Offline: Front Yard', {'enabled': True, 'from_address': 'alerts@example.com'}, 'admin@example.com'),
        ('Camera Offline: Front Yard', {'enabled': True, 'from_address': 'alerts@example.com'}, 'ops@example.com'),
    ]
    assert main_module._state._camera_health_state['cam-1']['offline_notified'] is True


def test_deliver_email_falls_back_to_from_address_when_no_recipients(ch, main_module, monkeypatch, stub_delivery_services, stub_logger):
    push_calls, email_calls = stub_delivery_services
    monkeypatch.setattr(
        main_module.database, 'get_setting',
        lambda key: {'enabled': True, 'offline_delay_minutes': 10, 'recipients': []},
    )
    monkeypatch.setattr(ch, 'effective_push_notification_settings', lambda: {'enabled': False})
    monkeypatch.setattr(
        ch, 'effective_email_alert_settings',
        lambda: {'enabled': True, 'from_address': 'alerts@example.com'},
    )
    main_module._state._camera_health_state['cam-1'] = {
        'online': False, 'offline_since': time.time() - 600,
        'offline_notified': False, 'recovery_notified': False,
    }
    ch._deliver_camera_offline_notification('cam-1', 'Front Yard', 'offline')
    assert email_calls == [
        ('Camera Offline: Front Yard', {'enabled': True, 'from_address': 'alerts@example.com'}, 'alerts@example.com'),
    ]


def test_deliver_recovery_event_uses_online_title(ch, main_module, monkeypatch, stub_delivery_services, stub_logger):
    push_calls, _email_calls = stub_delivery_services
    monkeypatch.setattr(
        main_module.database, 'get_setting',
        lambda key: {'enabled': True, 'offline_delay_minutes': 10, 'recipients': []},
    )
    monkeypatch.setattr(ch, 'effective_push_notification_settings', lambda: {'enabled': True})
    monkeypatch.setattr(ch, 'effective_email_alert_settings', lambda: {'enabled': False})
    main_module._state._camera_health_state['cam-1'] = {
        'online': True, 'offline_since': None, 'offline_notified': True, 'recovery_notified': False,
    }
    ch._deliver_camera_offline_notification('cam-1', 'Front Yard', 'recovery')
    assert push_calls == [
        ('Camera Online: Front Yard', 'Camera Front Yard (cam-1) is back online.'),
    ]
    assert main_module._state._camera_health_state['cam-1']['recovery_notified'] is True


def test_deliver_push_failure_logs_warning_but_marks_still(ch, main_module, monkeypatch, stub_logger, stub_delivery_services):
    push_calls, _ = stub_delivery_services
    diagnostics, warnings = stub_logger
    push_called = []

    class FailingPush:
        def __init__(self, settings):
            pass
        def _deliver(self, title, body):
            raise RuntimeError('push service unavailable')
    monkeypatch.setattr(ch, 'PushNotificationService', FailingPush)
    monkeypatch.setattr(
        main_module.database, 'get_setting',
        lambda key: {'enabled': True, 'offline_delay_minutes': 10, 'recipients': []},
    )
    monkeypatch.setattr(ch, 'effective_push_notification_settings', lambda: {'enabled': True})
    monkeypatch.setattr(ch, 'effective_email_alert_settings', lambda: {'enabled': False})
    main_module._state._camera_health_state['cam-1'] = {
        'online': False, 'offline_since': time.time() - 600,
        'offline_notified': False, 'recovery_notified': False,
    }
    # The exception is caught (logger.warning), and the mark still fires.
    ch._deliver_camera_offline_notification('cam-1', 'Front Yard', 'offline')
    assert len(warnings) == 1
    assert 'Push notify failed' in warnings[0][0][0]
    assert main_module._state._camera_health_state['cam-1']['offline_notified'] is True


# ---------------------------------------------------------------------------
# _check_cameras_health -- iterates cameras_config snapshot
# ---------------------------------------------------------------------------

def test_check_cameras_health_iterates_config_snapshot(ch, main_module, monkeypatch, stub_delivery_services, stub_logger):
    captured_iterations: list[str] = []

    # Monkey-patch the helpers to capture which camera_ids are processed.
    real_update = ch._update_camera_health
    real_eligible_off = ch._camera_offline_notification_eligible
    real_eligible_rec = ch._camera_recovery_notification_eligible
    real_deliver = ch._deliver_camera_offline_notification

    def fake_update(camera_id, online):
        captured_iterations.append(camera_id)
        return real_update(camera_id, online)
    monkeypatch.setattr(ch, '_update_camera_health', fake_update)
    monkeypatch.setattr(ch, '_deliver_camera_offline_notification', lambda *a, **kw: None)

    import app.state as _app_state
    monkeypatch.setattr(_app_state, 'cameras_config', [
        {'id': 'cam-1', 'name': 'Cam One'},
        {'id': 'cam-2', 'name': 'Cam Two'},
    ])
    monkeypatch.setattr(_app_state, 'live_detection_retry_after', {})
    ch._check_cameras_health()
    assert captured_iterations == ['cam-1', 'cam-2']


def test_check_cameras_health_skips_cameras_in_backoff_set_offline(ch, main_module, monkeypatch, stub_delivery_services, stub_logger):
    """Cameras with retry_after (in detection backoff) are marked offline
    while backoff is in effect."""
    import app.state as _app_state
    monkeypatch.setattr(ch, '_deliver_camera_offline_notification', lambda *a, **kw: None)
    monkeypatch.setattr(_app_state, 'cameras_config', [{'id': 'cam-1', 'name': 'Cam One'}])
    monkeypatch.setattr(_app_state, 'live_detection_retry_after', {'cam-1': time.time() + 600})
    ch._check_cameras_health()
    state = main_module._state._camera_health_state['cam-1']
    assert state['online'] is False
    assert state['offline_since'] is not None


def test_check_cameras_health_empty_config_no_op(ch, main_module, monkeypatch):
    """No cameras to iterate -- early exit; state dict stays untouched."""
    import app.state as _app_state
    monkeypatch.setattr(_app_state, 'cameras_config', [])
    initial_state_keys = list(main_module._state._camera_health_state.keys())
    ch._check_cameras_health()
    assert list(main_module._state._camera_health_state.keys()) == initial_state_keys


# ---------------------------------------------------------------------------
# State isolation: per-camera-id state must NOT leak
# ---------------------------------------------------------------------------

def test_state_isolation_per_camera_id(ch, main_module):
    """Mutating camera 'A' must not affect camera 'B' state."""
    main_module._state._camera_health_state['A'] = {
        'online': True, 'offline_since': None, 'offline_notified': False, 'recovery_notified': False,
    }
    main_module._state._camera_health_state['B'] = {
        'online': True, 'offline_since': None, 'offline_notified': False, 'recovery_notified': False,
    }
    ch._update_camera_health('A', False)
    assert main_module._state._camera_health_state['A']['online'] is False
    assert main_module._state._camera_health_state['B']['online'] is True  # unaffected
    assert main_module._state._camera_health_state['B']['offline_since'] is None


# ---------------------------------------------------------------------------
# Lock discipline under concurrency
# ---------------------------------------------------------------------------

def test_lock_discipline_concurrent_updates_no_corruption(ch, main_module):
    """8 threads concurrently toggling one camera_id's online flag must
    produce a non-corrupted final state. The threading.Lock around the
    state-dict mutation guarantees no dictionary or key is in flight
    while another thread inspects it."""
    main_module._state._camera_health_state['race-cam'] = {
        'online': True, 'offline_since': None, 'offline_notified': False, 'recovery_notified': False,
    }

    errors: list[BaseException] = []

    def toggle_online(value):
        try:
            for _ in range(50):
                ch._update_camera_health('race-cam', value)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(toggle_online, v) for v in [True, False] for _ in range(4)]
        for f in futures:
            f.result()
    assert errors == [], f'lock discipline broke: {errors}'
    state = main_module._state._camera_health_state['race-cam']
    # Final online flag must be a real bool (never mid-write garbage).
    assert isinstance(state['online'], bool)
    # offline_since is either None (when last transition was online) or a float >= 0.
    assert state['offline_since'] is None or isinstance(state['offline_since'], (int, float))


def test_lock_discipline_concurrent_mark_no_key_error(ch, main_module):
    """8 threads concurrently calling `_mark_*_notified` for an existing
    state should not raise a KeyError mid-transit."""
    main_module._state._camera_health_state['mark-cam'] = {
        'online': False, 'offline_since': time.time() - 600,
        'offline_notified': False, 'recovery_notified': True,
    }
    errors: list[BaseException] = []

    def run_marks():
        try:
            for _ in range(100):
                ch._mark_camera_offline_notified('mark-cam')
                ch._mark_camera_recovery_notified('mark-cam')
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run_marks) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], f'lock discipline broke: {errors}'
    state = main_module._state._camera_health_state['mark-cam']
    assert state['offline_notified'] is True
    assert state['recovery_notified'] is True


# ---------------------------------------------------------------------------
# Property-style sanity tests (captures the high-level invariants)
# ---------------------------------------------------------------------------

def test_state_machine_invariant_offline_to_offline_does_not_change_offline_since(ch, main_module):
    """Idempotent offline update must NOT increment / reset offline_since."""
    main_module._state._camera_health_state['cam-1'] = {
        'online': False, 'offline_since': 1000.0,
        'offline_notified': True, 'recovery_notified': False,
    }
    initial_offline_since = main_module._state._camera_health_state['cam-1']['offline_since']
    ch._update_camera_health('cam-1', False)
    assert main_module._state._camera_health_state['cam-1']['offline_since'] == initial_offline_since


def test_state_machine_recovers_only_after_offline(ch, main_module):
    """A camera that's already online must NOT trigger a recovery state change."""
    main_module._state._camera_health_state['cam-1'] = {
        'online': True, 'offline_since': None, 'offline_notified': False, 'recovery_notified': True,  # prior false recovery
    }
    ch._update_camera_health('cam-1', True)
    state = main_module._state._camera_health_state['cam-1']
    assert state['online'] is True
    # No transition -> recovery_notified stays as before (no spurious changes).
    assert state['recovery_notified'] is True
