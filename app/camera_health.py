"""Camera offline/health lifecycle helpers extracted from ``app/main.py`` (Phase-24).

The 8 helpers shipped here cluster around the per-camera **health-state
machine**: tracking each camera's online/offline presence, deciding when
to send an offline-notification or a recovery-notification, and
delivering those notifications through the existing email + push
sidecar services.

This is the FIRST hybrid-pattern extraction that involves **module-level
mutable state**. The state primitives are:

- ``_camera_health_state`` -- ``dict[str, dict[str, Any]]`` mapping
  ``camera_id`` -> ``{'online', 'offline_since', 'offline_notified',
  'recovery_notified'}``. Lived on ``app.main`` since 2024-05 (Phase-9).
- ``_camera_health_lock`` -- ``threading.Lock`` guarding every read /
  write of ``_camera_health_state``.

Both primitives **stay on ``app.main``** for this extraction (Phase-24
does NOT migrate them). This is the **state-migration template**:

- State stays on the host module so the live-detection history rebuild
  pattern (e.g. ``camera_config.py:_migrate_camera_id`` rewriting
  ``main.live_detection_history`` across a camera rename) continues to
  work without a cross-module write barrier.
- Helpers read/write via ``main._camera_health_state`` /
  ``main._camera_health_lock`` **at call time**, never as default args,
  to dodge the Phase-23 circular-import trap (default-arg evaluation
  fires during the Pool A rebind loop before main.py finishes defining
  the primitives).
- Lock discipline is preserved verbatim: every mutation is inside
  ``with main._camera_health_lock:`` and the lock is RELEASED before any
  cross-module side effect (e.g. ``log_camera_diagnostic`` writes to
  SQLite -- releasing the lock first prevents waiting on a DB write
  while holding the in-memory state lock).

The cluster's only callers live inside ``process_live_stream_alerts``
background thread (``live_alert_monitor_loop`` invokes
``_check_cameras_health`` every cycle) and in main.py's tests via
``main.<attr>`` references. **Zero cross-router reach** from
``app/api/*.py``.

Like the prior-cluster extractions (``app/auth_gates.py`` Phase-16,
``app/config_facades.py`` Phase-17, ``app/camera_config.py`` Phase-18,
``app/recording_settings.py`` Phase-19, ``app/ai_settings.py`` Phase-20,
``app/zone_schema.py`` Phase-21, ``app/payload_validators.py`` Phase-22,
``app/zone_detection.py`` Phase-23), these are extracted using the
**hybrid-pattern template**:

- Cluster functions reach ``main.<attr>`` at *call time* for their
  cross-module dependencies.
- The Pool A from-import rebinds live at the TOP of ``app/main.py``
  in the existing rebind section so the eager-evaluation order at
  module load has ``main.<name>`` wired correctly before any sibling
  body references it as a bare name.

Cluster membership (8 helpers, 108 original lines):

- ``effective_camera_offline_alert_settings`` -- returns the merged
  ``{'enabled', 'offline_delay_minutes', 'recipients'}`` dict, defaulting
  to ``enabled=False`` and reading the database override via
  ``main.database.get_setting('camera_offline_alert')``.

- ``_update_camera_health`` -- state-machine transition (online ->
  offline or offline -> online); takes the lock, mutates
  ``main._camera_health_state[camera_id]``, releases the lock, then
  logs the transition via ``main.log_camera_diagnostic``.

- ``_camera_offline_notification_eligible`` -- reads state under lock
  to decide whether the camera has been offline long enough and has not
  yet been notified for the current offline-streak. Threshold is the
  ``offline_delay_minutes`` value from the effective settings.

- ``_camera_recovery_notification_eligible`` -- reads state under lock
  to decide whether the camera has come back online after a previously
  notified offline-streak.

- ``_mark_camera_offline_notified`` -- sets ``state['offline_notified']``
  on the locked state dict.

- ``_mark_camera_recovery_notified`` -- sets ``state['recovery_notified']``
  on the locked state dict. Equivalent to ``_mark_camera_offline_notified``
  but for the recovery-streak.

- ``_deliver_camera_offline_notification`` -- builds the email + push
  message through ``main.EmailAlertService`` /
  ``main.PushNotificationService`` and stamps the notification flag on
  the state dict via the two ``_mark_*`` helpers so subsequent cycles
  of ``_check_cameras_health`` don't re-send.

- ``_check_cameras_health`` -- the periodic monitor loop entry point
  (called from ``live_alert_monitor_loop``). Iterates ``main.cameras_config``
  (point-in-time ``list()`` snapshot so concurrent ``apply_cameras_settings``
  mutation cannot raise ``RuntimeError``), gates on
  ``main.live_detection_retry_after`` to mark detection-backoff
  cameras as offline, then runs the helpers above.

Pool C reach sites (resolved via ``main.<attr>`` at call time):

- ``main.database`` (``effective_camera_offline_alert_settings``)
- ``main._camera_health_state`` + ``main._camera_health_lock`` (every
  state-touching helper; resolved at call time inside the helper body
  -- never as default arg)
- ``main.log_camera_diagnostic`` (``_update_camera_health`` -- the
  logger is called OUTSIDE the lock block to avoid holding the in-memory
  state lock while waiting on a SQLite write)
- ``main.cameras_config`` (``_check_cameras_health`` -- iterated as
  ``for cfg in list(main.cameras_config)`` to thread-safely snapshot)
- ``main.live_detection_retry_after`` (``_check_cameras_health`` --
  read inside the loop, point-in-time get())
- ``main.logger`` (``_deliver_camera_offline_notification`` -- warning
  on push/email delivery failures)
- ``main.effective_push_notification_settings`` (``_deliver_camera_offline_notification``)
- ``main.effective_email_alert_settings`` (``_deliver_camera_offline_notification``)
- ``main.PushNotificationService`` / ``main.EmailAlertService``
  (``_deliver_camera_offline_notification`` -- instantiated per-call
  from the effective settings)

Stdlib imports at module top (NOT Pool C):
- ``time.time()`` for offline_since stamping + delay-elapsed math
- ``email.mime.text.MIMEText`` for the email body
"""

from __future__ import annotations

import time
from email.mime.text import MIMEText
from typing import Any

import app.main as main


def effective_camera_offline_alert_settings() -> dict[str, Any]:
    settings = {'enabled': False, 'offline_delay_minutes': 1, 'recipients': []}
    override = main.database.get_setting('camera_offline_alert')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def _update_camera_health(camera_id: str, online: bool) -> None:
    with main._camera_health_lock:
        state = main._camera_health_state.get(camera_id, {'online': True, 'offline_since': None, 'offline_notified': False, 'recovery_notified': False})
        was_online = state.get('online', True)
        state['online'] = online
        transition: str | None = None
        if not online and was_online:
            state['offline_since'] = state.get('offline_since') or time.time()
            state['recovery_notified'] = False
            transition = 'offline'
        elif online and (not was_online):
            state['offline_since'] = None
            state['offline_notified'] = False
            transition = 'online'
        main._camera_health_state[camera_id] = state
    if transition == 'offline':
        main.log_camera_diagnostic(camera_id, 'camera_offline', 'Camera went offline (detection unavailable).', severity='warning')
    elif transition == 'online':
        main.log_camera_diagnostic(camera_id, 'camera_online', 'Camera recovered and is back online.', severity='info')


def _camera_offline_notification_eligible(camera_id: str) -> bool:
    delay_minutes = int(effective_camera_offline_alert_settings().get('offline_delay_minutes', 1))
    delay_seconds = max(0, delay_minutes * 60)
    with main._camera_health_lock:
        state = main._camera_health_state.get(camera_id)
        if not state or state.get('online', True):
            return False
        if state.get('offline_notified'):
            return False
        offline_since = state.get('offline_since')
        if offline_since is None:
            return False
        return time.time() - offline_since >= delay_seconds


def _camera_recovery_notification_eligible(camera_id: str) -> bool:
    with main._camera_health_lock:
        state = main._camera_health_state.get(camera_id)
        if not state or not state.get('online', True):
            return False
        if state.get('recovery_notified'):
            return False
        return state.get('offline_notified', False)


def _mark_camera_offline_notified(camera_id: str) -> None:
    with main._camera_health_lock:
        state = main._camera_health_state.get(camera_id)
        if state:
            state['offline_notified'] = True


def _mark_camera_recovery_notified(camera_id: str) -> None:
    with main._camera_health_lock:
        state = main._camera_health_state.get(camera_id)
        if state:
            state['recovery_notified'] = True


def _deliver_camera_offline_notification(camera_id: str, camera_name: str, event_type: str) -> None:
    settings = effective_camera_offline_alert_settings()
    if not settings.get('enabled'):
        return
    if event_type == 'offline':
        title = f'Camera Offline: {camera_name}'
        body = f'Camera {camera_name} ({camera_id}) has gone offline.'
    else:
        title = f'Camera Online: {camera_name}'
        body = f'Camera {camera_name} ({camera_id}) is back online.'
    push_settings_obj = main.effective_push_notification_settings()
    if push_settings_obj.get('enabled'):
        try:
            notifier = main.PushNotificationService(push_settings_obj)
            notifier._deliver(title, body)
        except Exception as exc:
            main.logger.warning('Push notify failed for camera %s %s: %s', camera_id, event_type, exc)
    email_settings_obj = main.effective_email_alert_settings()
    if email_settings_obj.get('enabled'):
        try:
            mailer = main.EmailAlertService(email_settings_obj)
            recipients = [r for r in settings.get('recipients') or [] if isinstance(r, str) and '@' in r]
            if not recipients:
                fallback = str(email_settings_obj.get('from_address') or '').strip()
                if fallback and '@' in fallback:
                    recipients = [fallback]
            if recipients:
                msg = MIMEText(body, 'plain', 'utf-8')
                msg['Subject'] = title
                msg['From'] = str(email_settings_obj.get('from_address'))
                msg['To'] = ', '.join(recipients)
                mailer._deliver(msg)
        except Exception as exc:
            main.logger.warning('Email notify failed for camera %s %s: %s', camera_id, event_type, exc)
    if event_type == 'offline':
        _mark_camera_offline_notified(camera_id)
    else:
        _mark_camera_recovery_notified(camera_id)


def _check_cameras_health() -> None:
    for cfg in list(main.cameras_config):
        cam_id = str(cfg.get('id') or '')
        cam_name = str(cfg.get('name') or cam_id or 'Unknown')
        if not cam_id:
            continue
        retry_after = main.live_detection_retry_after.get(cam_id, 0)
        now = time.time()
        camera_online = not (retry_after and now < retry_after)
        _update_camera_health(cam_id, camera_online)
        if _camera_offline_notification_eligible(cam_id):
            _deliver_camera_offline_notification(cam_id, cam_name, 'offline')
        elif _camera_recovery_notification_eligible(cam_id):
            _deliver_camera_offline_notification(cam_id, cam_name, 'recovery')
