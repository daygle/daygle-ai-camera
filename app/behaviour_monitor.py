"""Behavioural intelligence monitor (Tier 1): tripwire crossing emission.

Turns the pure line-crossing detections from ``app/behaviour.py`` into events,
optional recordings, and email/push alerts, mirroring the self-contained sound
alert path (``app/sound_monitor._on_sound_detected``). Kept out of
``app/behaviour.py`` so that module stays dependency-free and unit-testable; the
heavy alert/recording dependencies here are imported lazily inside the functions
so this module also imports cleanly on its own.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import app.state as _state
from app import behaviour

logger = logging.getLogger('daygle.ai')

# Per-crossing cooldown: last wall-clock fire time keyed by
# ``camera|zone|direction|track`` so a subject straddling the line does not
# emit a burst of duplicate crossings.
_tripwire_last_fired: dict[str, float] = {}
_tripwire_lock = threading.Lock()


def _enabled_zones_with_tripwire(settings: Any) -> list[dict[str, Any]]:
    zones = (settings.get('detection') or {}).get('zones', []) if isinstance(settings, dict) else []
    out: list[dict[str, Any]] = []
    for zone in zones or []:
        if not isinstance(zone, dict) or zone.get('enabled') is False:
            continue
        wire = zone.get('tripwire')
        if isinstance(wire, dict) and wire.get('enabled') is not False:
            out.append(zone)
    return out


def emit_tripwire_crossings(camera_id: str, settings: dict[str, Any], detections: list[dict[str, Any]]) -> int:
    """Detect and emit tripwire crossings for one detection cycle.

    Returns the number of crossings that fired an event (i.e. were past their
    cooldown). Best-effort: an individual crossing's recording/notification
    failure is logged and never raised, so behavioural code cannot break the
    detection loop.
    """
    zones = _enabled_zones_with_tripwire(settings)
    if not zones:
        return 0
    crossings = behaviour.zone_tripwire_crossings(detections, zones)
    if not crossings:
        return 0
    wires = {str(z.get('id') or z.get('name') or ''): z.get('tripwire') for z in zones}
    now = time.time()
    fired = 0
    for crossing in crossings:
        wire = wires.get(crossing['zone_id']) or {}
        try:
            cooldown = max(0, int(wire.get('cooldown_seconds') or 0))
        except (TypeError, ValueError):
            cooldown = 0
        key = f"{camera_id}|{crossing['zone_id']}|{crossing['direction']}|{crossing['track_id']}"
        with _tripwire_lock:
            if not behaviour.cooldown_passed(_tripwire_last_fired.get(key), now, cooldown):
                continue
            _tripwire_last_fired[key] = now
            # Track ids climb forever, so the cooldown map would grow without
            # bound; opportunistically drop entries older than an hour once it
            # gets large. An expired entry only means "no cooldown", which is
            # already the default for a never-seen key.
            if len(_tripwire_last_fired) > 2048:
                cutoff = now - 3600
                for stale in [k for k, ts in _tripwire_last_fired.items() if ts < cutoff]:
                    _tripwire_last_fired.pop(stale, None)
        try:
            _emit_one(camera_id, settings, crossing, wire)
            fired += 1
        except Exception as exc:  # noqa: BLE001 - one crossing must not break the loop
            logger.warning('Tripwire crossing emit failed on %s: %s', camera_id, exc)
    return fired


def _emit_one(camera_id: str, settings: dict[str, Any], crossing: dict[str, Any], wire: dict[str, Any]) -> None:
    from app.alert_dispatch import _rule_notify_active_now, deliver_alert_notifications
    from app.utils import normalize_bool_setting, normalize_email_recipients

    now_iso = datetime.now(timezone.utc).isoformat()
    zone_name = crossing.get('zone_name') or 'zone'
    wire_name = crossing.get('tripwire_name') or 'Tripwire'
    direction = crossing['direction']
    label = crossing.get('label') or 'object'
    try:
        confidence = float(crossing.get('confidence') or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    email_enabled = normalize_bool_setting(wire.get('email_enabled'), False)
    push_enabled = normalize_bool_setting(wire.get('push_enabled'), False)
    email_recipients = normalize_email_recipients(wire.get('email_recipients') or [])
    notify_enabled = email_enabled or push_enabled
    camera_name = str((settings or {}).get('name') or '').strip() or None

    metadata = {
        'source': 'line-crossing',
        'camera_id': camera_id,
        'camera_name': camera_name,
        'zone_id': crossing.get('zone_id'),
        'zone_name': crossing.get('zone_name'),
        'tripwire_name': wire_name,
        'direction': direction,
        'label': label,
        'track_id': crossing.get('track_id'),
        'confidence': round(confidence, 3),
    }
    event_id = _state.database.add_event(
        created_at=now_iso, source='behaviour', snapshot_path=None,
        detections=[], alert_triggered=notify_enabled, metadata=metadata,
    )

    recording_id = None
    if normalize_bool_setting(wire.get('record_on_detect'), True):
        recording_id = _attach_recording(camera_id, settings, event_id, now_iso, label, confidence)

    rule_name = f'{zone_name} · {wire_name}'
    message = f'{str(label).title()} crossed {wire_name} ({direction}) in {zone_name}'
    notify_rule = {
        'name': rule_name,
        'email_enabled': email_enabled,
        'push_enabled': push_enabled,
        'email_recipients': email_recipients,
        'notify_start': str(wire.get('notify_start') or '').strip() or None,
        'notify_end': str(wire.get('notify_end') or '').strip() or None,
    }
    if notify_enabled and _rule_notify_active_now(notify_rule):
        _state.database.add_alert(
            created_at=now_iso, rule_name=rule_name, event_id=event_id,
            label=label, confidence=confidence, message=message, recording_id=recording_id,
        )
        alert_payload = {'rule_name': rule_name, 'label': label, 'confidence': confidence, 'message': message}
        thread = threading.Thread(
            target=deliver_alert_notifications, args=([alert_payload], event_id, [notify_rule]),
            name=f'tripwire-notify-{event_id}', daemon=True,
        )
        with _state._notification_threads_lock:
            _state._notification_threads[:] = [t for t in _state._notification_threads if t.is_alive()]
            _state._notification_threads.append(thread)
        thread.start()
    logger.info('Tripwire crossing on %s: %s %s -> %s (event %s)', camera_id, label, wire_name, direction, event_id)


def _attach_recording(camera_id: str, settings: dict[str, Any], event_id: int, now_iso: str, label: str, confidence: float) -> int | None:
    try:
        from app.recording_extension import attach_event_recording
        from app.utils import build_stream_url

        stream_url = build_stream_url(settings)
        if not stream_url:
            return None
        rec_config = _state.camera_event_recording_config(settings)
        _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=rec_config)
        detection = {'label': label, 'confidence': confidence, 'alert_triggered': True}
        return attach_event_recording(event_id, now_iso, 'rtsp', [detection], camera_id=camera_id, recording_config=rec_config)
    except Exception as exc:  # noqa: BLE001 - recording is best-effort
        logger.warning('Tripwire recording attach failed on %s: %s', camera_id, exc)
        return None
