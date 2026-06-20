"""Sound-monitor helpers extracted from ``app/main.py`` (Phase-E).

Cluster membership:
- ``_sound_status_reason(diagnostics)`` — pick the most-relevant class from a
  diagnostic snapshot for status surface
- ``_on_sound_detected(camera_id, class_id, rule_name, confidence, meta)`` —
  per-camera SoundDetector callback: record event, fire recording, queue alerts
- ``_make_sound_detect_callback(camera_id)`` — factory that closes over camera_id
- ``apply_sound_settings()`` — start one SoundDetector per enabled camera
- ``stop_sound_monitor()`` — stop all running SoundDetectors

State lives in ``app.state``:
- ``_sound_detectors`` / ``_sound_detectors_lock``
- ``_sound_statuses`` / ``_sound_statuses_lock``

Pool-C reach (resolved lazily via lazy imports inside function bodies):
- ``app.main.camera_event_recording_config``
- ``app.main.build_stream_url``  (transitional; can move to app.utils once
  main.py's apply_cameras_settings no longer uses it as a bare name)
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

import app.state as _state
from app.alert_dispatch import (
    _rule_notify_active_now,
    deliver_sound_alert_notifications as _deliver_sound_alert_notifications,
)
from app.sound_detector import SOUND_CLASSES, SoundDetector
from app.utils import normalize_bool_setting, normalize_email_recipients, build_stream_url

logger = logging.getLogger('daygle.ai')


def _sound_status_reason(diagnostics: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the single most relevant class to explain the current listening state.

    Mirrors how the live object status surfaces an alert reason: prefer the
    loudest class at/above its threshold (would alert, possibly held back by
    cooldown), otherwise the loudest class heard below threshold. Returns None
    when nothing notable is being heard.
    """
    if not diagnostics:
        return None
    above = [d for d in diagnostics if d['confidence'] > 0 and d['confidence'] >= d['threshold']]
    if above:
        top = above[0]
        code = 'cooldown' if top['in_cooldown'] else 'detected'
    else:
        below = [d for d in diagnostics if 0 < d['confidence'] < d['threshold']]
        if not below:
            return None
        top = below[0]
        code = 'below_threshold'
    return {'code': code, 'class': top['class'], 'class_label': top['label'], 'confidence': top['confidence'], 'threshold': top['threshold'], 'cooldown_remaining': top['cooldown_remaining']}


def _on_sound_detected(camera_id: str, class_id: str, rule_name: str, confidence: float, meta: dict[str, Any]) -> None:
    """Callback invoked by a per-camera SoundDetector when a sound class is detected."""
    from app.main import camera_event_recording_config
    from app.recording_extension import attach_event_recording
    class_label = SOUND_CLASSES.get(class_id, {}).get('label', class_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    with _state._sound_statuses_lock:
        status = _state._sound_statuses.setdefault(camera_id, {})
        status['state'] = 'detected'
        status['last_detected_at'] = now_iso
        status['last_class'] = class_id
        status['last_class_label'] = class_label
        status['last_confidence'] = round(confidence, 3)
        status['backend'] = meta.get('backend', 'unknown')
    logger.info('Sound detected on %s: %s (confidence=%.2f, backend=%s)', camera_id, class_label, confidence, meta.get('backend'))
    cam_settings = next((c for c in _state.cameras_config if str(c.get('id') or '') == camera_id), None)
    sound_rules = cam_settings.get('detection', {}).get('sound', {}).get('rules', []) if cam_settings else []
    fired_rule = next((r for r in sound_rules if r.get('class') == class_id), {})
    email_enabled = normalize_bool_setting(fired_rule.get('email_enabled'), False)
    email_recipients = normalize_email_recipients(fired_rule.get('email_recipients', []))
    push_enabled = normalize_bool_setting(fired_rule.get('push_enabled'), False)
    notify_enabled = email_enabled or push_enabled
    event_id = _state.database.add_event(created_at=now_iso, source='sound', snapshot_path=None, detections=[], alert_triggered=notify_enabled, metadata={'source': 'sound-detection', 'sound_source': 'rtsp', 'camera_id': camera_id, 'camera_name': str((cam_settings or {}).get('name') or '').strip() or None, 'label': class_id, 'class_label': class_label, 'confidence': round(confidence, 3)})
    sound_detection = {'label': class_id, 'confidence': confidence, 'alert_triggered': True}
    should_record = normalize_bool_setting(fired_rule.get('record_on_detect'), True)
    recording_ids: list[int] = []
    if should_record and cam_settings:
        stream_url = build_stream_url(cam_settings)
        if stream_url:
            cam_rec_config = camera_event_recording_config(cam_settings)
            _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config)
            rid = attach_event_recording(event_id, now_iso, 'rtsp', [sound_detection], camera_id=camera_id, recording_config=cam_rec_config)
            if rid is not None:
                recording_ids.append(rid)
                logger.debug('Sound event %s linked to recording %s (camera %s)', event_id, rid, camera_id)
    message = f'{class_label} detected ({confidence:.0%} confidence)'
    if notify_enabled and _rule_notify_active_now(fired_rule):
        _state.database.add_alert(created_at=now_iso, rule_name=rule_name, event_id=event_id, label=class_id, confidence=confidence, message=message, recording_id=recording_ids[0] if recording_ids else None)
    alert_payload = {'rule_name': rule_name, 'label': class_id, 'confidence': confidence, 'message': message}
    notify_rule = {'name': rule_name, 'email_enabled': email_enabled, 'push_enabled': push_enabled, 'email_recipients': email_recipients, 'notify_start': str(fired_rule.get('notify_start') or '').strip() or None, 'notify_end': str(fired_rule.get('notify_end') or '').strip() or None}
    if notify_enabled:
        notify_thread = threading.Thread(target=_deliver_sound_alert_notifications, args=([alert_payload], event_id, notify_rule), name=f'sound-alert-notify-{event_id}', daemon=True)
        with _state._notification_threads_lock:
            _state._notification_threads[:] = [t for t in _state._notification_threads if t.is_alive()]
            _state._notification_threads.append(notify_thread)
        notify_thread.start()


def _make_sound_detect_callback(camera_id: str):
    def _callback(class_id: str, rule_name: str, confidence: float, meta: dict[str, Any]) -> None:
        _on_sound_detected(camera_id, class_id, rule_name, confidence, meta)
    return _callback


def apply_sound_settings() -> None:
    """Start one SoundDetector per RTSP camera that has sound detection enabled."""
    from app.main import camera_event_recording_config
    with _state._sound_detectors_lock:
        for det in list(_state._sound_detectors.values()):
            det.stop()
        _state._sound_detectors.clear()
    for cam in list(_state.cameras_config):
        cam_id = str(cam.get('id') or '')
        stream_url = build_stream_url(cam)
        if not cam_id or not stream_url:
            continue
        sound_cfg = cam.get('detection', {}).get('sound', {})
        if not normalize_bool_setting(sound_cfg.get('enabled'), False):
            with _state._sound_statuses_lock:
                _state._sound_statuses[cam_id] = {'state': 'disabled', 'last_detected_at': None, 'last_confidence': 0.0, 'backend': None}
            continue
        enabled_rules = [r for r in sound_cfg.get('rules') or [] if r.get('enabled')]
        if not enabled_rules:
            with _state._sound_statuses_lock:
                _state._sound_statuses[cam_id] = {'state': 'disabled', 'last_detected_at': None, 'last_confidence': 0.0, 'backend': None}
            continue
        _state.recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=cam_id, recording_config=camera_event_recording_config(cam))
        det = SoundDetector(on_detect=_make_sound_detect_callback(cam_id), rules=enabled_rules, source='ingest', sample_duration_seconds=1.0, audio_segment_provider=lambda after, _cid=cam_id: _state.recording_service.audio_segments_after(_cid, after))
        det.start()
        with _state._sound_detectors_lock:
            _state._sound_detectors[cam_id] = det
        with _state._sound_statuses_lock:
            _state._sound_statuses[cam_id] = {'state': 'listening', 'last_detected_at': None, 'last_confidence': 0.0, 'backend': det.backend}
        logger.info('Sound monitor started for camera %s (rules=%s)', cam_id, [r.get('class') for r in enabled_rules])


def stop_sound_monitor() -> None:
    with _state._sound_detectors_lock:
        for det in list(_state._sound_detectors.values()):
            det.stop()
        _state._sound_detectors.clear()
    with _state._sound_statuses_lock:
        for cam_id in list(_state._sound_statuses.keys()):
            _state._sound_statuses[cam_id]['state'] = 'stopped'
