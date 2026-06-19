from __future__ import annotations
import asyncio
import copy
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import logging
import logging.handlers
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import gc
import time
import urllib.error
import urllib.request
from collections import deque
from email.mime.text import MIMEText
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from app.alerts import AlertEngine
from app.auth import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, AuthError, AuthService, utc_now
from app.database import EventDatabase
from app.detector import DetectorUnavailableError, create_detector, load_labels
from app.email_alerts import EmailAlertError, EmailAlertService
from app.push_notifications import PushNotificationError, PushNotificationService
from app.camera_backend import OpenCvStreamCamera
from app.ptz import send_ptz_command, VALID_COMMANDS as PTZ_VALID_COMMANDS
from app.recordings import RecordingService
from app.settings import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, load_settings
from app.sound_detector import SoundDetector, SOUND_CLASSES, DEFAULT_RULES
from app.storage import Storage
logger = logging.getLogger('daygle.ai')

def _configure_file_logging() -> None:
    log_dir = Path(__file__).resolve().parent.parent / 'data' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / 'app.log'
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, logging.handlers.RotatingFileHandler) and existing.baseFilename == str(log_path):
            return
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    if not root.handlers:
        logging.basicConfig(level=logging.INFO)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
_configure_file_logging()
_FFPROBE: str | None = shutil.which('ffprobe')
_FFMPEG: str | None = shutil.which('ffmpeg')
YOLO_MODELS: dict[str, dict[str, Any]] = {'yolov8n': {'pt': 'yolov8n.pt', 'onnx': 'yolov8n.onnx', 'label': 'YOLOv8n · Nano', 'approx_mb': 6, 'description': 'Fastest inference, lowest accuracy. Best for low-power or embedded hardware.'}, 'yolov8s': {'pt': 'yolov8s.pt', 'onnx': 'yolov8s.onnx', 'label': 'YOLOv8s · Small', 'approx_mb': 22, 'description': 'Good balance of speed and accuracy for most systems.'}, 'yolov8m': {'pt': 'yolov8m.pt', 'onnx': 'yolov8m.onnx', 'label': 'YOLOv8m · Medium', 'approx_mb': 52, 'description': 'Significantly better accuracy. Recommended for IR or night-vision cameras.'}, 'yolov8l': {'pt': 'yolov8l.pt', 'onnx': 'yolov8l.onnx', 'label': 'YOLOv8l · Large', 'approx_mb': 87, 'description': 'High accuracy. Requires a capable CPU or GPU.'}, 'yolov8x': {'pt': 'yolov8x.pt', 'onnx': 'yolov8x.onnx', 'label': 'YOLOv8x · Extra Large', 'approx_mb': 131, 'description': 'Best possible accuracy. GPU strongly recommended.'}}
ONE_PIXEL_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'
config = load_settings()
auth_config = config.get('auth', {})
auth_enabled = bool(auth_config.get('enabled', True))

@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    removed = database.cleanup_incomplete_recordings()
    if removed:
        logger.info(f'Cleaned up {len(removed)} incomplete recording(s) from previous session')
    log_detector_initialization()
    start_live_alert_monitor()
    apply_sound_settings()
    try:
        yield
    finally:
        recording_service.stop_prebuffer_workers()
        recording_service.stop_all_continuous_recordings()
        stop_live_alert_monitor()
        stop_sound_monitor()
app = FastAPI(title='Daygle AI Camera', lifespan=app_lifespan)
BASE_DIR = Path(__file__).resolve().parent.parent
web_dir = BASE_DIR / 'web'
static_dir = web_dir
if static_dir.exists():
    app.mount('/static', StaticFiles(directory=static_dir), name='static')

def effective_ai_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('ai', {}))
    override = database.get_setting('ai')
    if isinstance(override, dict):
        settings.update(override)
    return settings
_min_rule_confidence_cache: tuple[float, float] | None = None
_MIN_RULE_CONFIDENCE_TTL = 5.0
_min_rule_confidence_lock = threading.Lock()

def compute_minimum_rule_confidence(fallback: float | None=None) -> float:
    """Return the lowest min_confidence across all enabled object rules so YOLO's floor never silently suppresses per-rule thresholds.

    Falls back to the configured global AI confidence when no zone rules define a
    lower threshold, so the model detection threshold always matches user expectation.

    Result is cached for _MIN_RULE_CONFIDENCE_TTL seconds to avoid a database
    read on every detection frame (called at ~4 Hz per camera from the hot path).
    """
    global _min_rule_confidence_cache
    cached = _min_rule_confidence_cache
    if cached is not None:
        cached_value, cached_at = cached
        if time.time() - cached_at < _MIN_RULE_CONFIDENCE_TTL:
            return cached_value
    with _min_rule_confidence_lock:
        cached = _min_rule_confidence_cache
        if cached is not None:
            cached_value, cached_at = cached
            if time.time() - cached_at < _MIN_RULE_CONFIDENCE_TTL:
                return cached_value
        if fallback is None:
            fallback = float(effective_ai_config().get('confidence') or 0.45)
        min_conf: float = fallback
        for camera in effective_cameras_config():
            for zone in camera.get('detection', {}).get('zones', []):
                for rule in zone.get('object_rules', []):
                    if not rule.get('enabled', True):
                        continue
                    if str(rule.get('label') or '').strip().lower() == 'motion':
                        continue
                    try:
                        conf = float(rule.get('min_confidence', fallback))
                        if conf < min_conf:
                            min_conf = conf
                    except (TypeError, ValueError):
                        pass
        result = min_conf
        _min_rule_confidence_cache = (result, time.time())
        return result

def effective_recording_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('recording', {}))
    override = database.get_setting('recording')
    if isinstance(override, dict):
        settings.update(override)
    return settings

def effective_live_config() -> dict[str, Any]:
    settings = {'snapshot_refresh_ms': 500, 'detection_status_refresh_ms': 2000, 'detection_interval_seconds': 0.5, 'event_debounce_seconds': 10.0, 'background_detection_enabled': True, 'detection_history_minutes': 10, 'motion_pixel_threshold': 30, 'motion_gate_fraction': 0.003, 'motion_scale_fraction': 0.1, 'motion_background_alpha': 0.05, 'periodic_scan_interval_seconds': 0}
    config_live = config.get('live', {})
    if isinstance(config_live, dict):
        settings.update(config_live)
    override = database.get_setting('live')
    if isinstance(override, dict):
        settings.update(override)
    return settings

def camera_event_recording_config(settings: dict[str, Any]) -> dict[str, Any]:
    base = effective_recording_config()
    camera_recording = normalize_camera_recording_settings(settings.get('recording'))
    base.update({'continuous': camera_recording['continuous']})
    return base

def effective_storage_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('storage', {}))
    override = database.get_setting('storage')
    if isinstance(override, dict):
        database_path = settings.get('database')
        settings.update(override)
        settings['database'] = database_path
    return settings

def effective_auth_config() -> dict[str, Any]:
    settings = copy.deepcopy(auth_config)
    override = database.get_setting('auth')
    if isinstance(override, dict):
        settings.update(override)
    return settings

def effective_email_alert_settings() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('alerts', {}).get('email', {}))
    override = database.get_setting('alert_email')
    if isinstance(override, dict):
        settings.update(override)
    return settings

def effective_push_notification_settings() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('alerts', {}).get('push_notification', {}))
    override = database.get_setting('alert_push')
    if isinstance(override, dict):
        settings.update(override)
    return settings
database = EventDatabase(config['storage']['database'])
camera_config: dict[str, Any] = {}
cameras_config: list[dict[str, Any]] = []
camera_instances: dict[str, Any] = {}
camera = None
storage = Storage({**config, 'storage': effective_storage_config()})
recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})
auth = AuthService(config['storage']['database'], effective_auth_config())
SESSION_COOKIE_NAME = str(effective_auth_config().get('cookie_name', SESSION_COOKIE))
detector = create_detector(effective_ai_config())
last_detector_error: str | None = getattr(detector, 'unavailable_reason', None)
alerts = AlertEngine([])
live_detection_last_checked: dict[str, float] = {}
live_detection_status: dict[str, dict[str, Any]] = {}
live_detection_status_lock = threading.Lock()
live_detection_history: dict[str, deque] = {}
live_detection_history_lock = threading.Lock()
live_event_last_emitted: dict[str, dict[str, Any]] = {}
live_event_last_emitted_lock = threading.Lock()
live_detection_retry_after: dict[str, float] = {}
live_detection_failure_count: dict[str, int] = {}
_live_backoff_lock = threading.Lock()
active_rtsp_recordings: dict[str, dict[str, Any]] = {}
active_rtsp_recordings_lock = threading.Lock()
live_detection_worker_lock = threading.Lock()
active_live_detection_cameras: set[str] = set()
_frame_motion_prev: dict[str, Any] = {}
_frame_motion_lock = threading.Lock()
_frame_motion_error_cameras: set[str] = set()
_periodic_scan_last_ts: dict[str, float] = {}
_MOTION_FRAME_W = 160
_MOTION_FRAME_H = 120
_MOTION_PIXEL_THRESHOLD = 30
_MOTION_GATE_FRACTION = 0.003
_MOTION_SCALE_FRACTION = 0.1
_MOTION_BACKGROUND_ALPHA = 0.05
live_alert_monitor_stop = threading.Event()
live_alert_monitor_thread: threading.Thread | None = None
_sound_detectors: dict[str, SoundDetector] = {}
_sound_detectors_lock = threading.Lock()
_sound_statuses: dict[str, dict[str, Any]] = {}
_sound_statuses_lock = threading.Lock()

def _sound_status_reason(diagnostics: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the single most relevant class to explain the current listening state.

    Mirrors how the live object status surfaces an alert reason: prefer the
    loudest class at/above its threshold (would alert, possibly held back by
    cooldown), otherwise the loudest class heard below threshold. Returns None
    when nothing notable is being heard.

    Kept here (and not solely on app.api.sound_router) so tests that do
    ``import app.main as main; main._sound_status_reason(...)`` keep working.
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
_camera_health_state: dict[str, dict[str, Any]] = {}
_camera_health_lock = threading.Lock()

def effective_camera_offline_alert_settings() -> dict[str, Any]:
    settings = {'enabled': False, 'offline_delay_minutes': 1, 'recipients': []}
    override = database.get_setting('camera_offline_alert')
    if isinstance(override, dict):
        settings.update(override)
    return settings

def _alert_datetime_prefs() -> tuple[str, str, str]:
    """Return (timezone_name, date_format, time_format) from the primary admin user."""
    try:
        users = auth.list_users()
        admin = next((u for u in users if u.get('role') == 'admin' and u.get('is_active')), None)
        if admin is None:
            admin = next(iter(users), None)
        if admin:
            return (str(admin.get('timezone') or 'UTC'), str(admin.get('date_format') or 'iso'), str(admin.get('time_format') or '24h'))
    except Exception:
        pass
    return ('UTC', 'iso', '24h')

def _rule_notify_active_now(rule: dict[str, Any]) -> bool:
    """Return True if a rule's email/push window (notify_start/notify_end) covers now.

    This window only limits email and push delivery - detection, recording and
    in-app alerts are gated by the separate detection/active window and always
    fire regardless of this one. An empty or partial window means "notify any
    time". It is evaluated in the admin user's configured timezone so a setting
    like 22:00 -> 05:00 lines up with the local clock, and windows that wrap past
    midnight (start > end) are supported.
    """
    start = str(rule.get('notify_start') or '').strip()
    end = str(rule.get('notify_end') or '').strip()
    if not start or not end:
        return True
    tz_name, _, _ = _alert_datetime_prefs()
    try:
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, KeyError):
        now_local = datetime.now(timezone.utc)
    now_hm = now_local.strftime('%H:%M')
    if start <= end:
        return start <= now_hm <= end
    return now_hm >= start or now_hm <= end

def _format_alert_datetime(iso_str: str) -> str:
    """Format a UTC ISO timestamp for display in alerts using the admin user's preferences."""
    tz_name, date_fmt, time_fmt = _alert_datetime_prefs()
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt = dt.astimezone(ZoneInfo(tz_name))
        tz_label = dt.strftime('%Z')
    except (ZoneInfoNotFoundError, KeyError):
        dt = dt.astimezone(timezone.utc)
        tz_label = 'UTC'
    date_str = dt.strftime({'us': '%m/%d/%Y', 'au': '%d/%m/%Y'}.get(date_fmt, '%Y-%m-%d'))
    if time_fmt == '12h':
        hour = str(int(dt.strftime('%I')))
        time_str = f"{hour}{dt.strftime(':%M:%S %p')}"
    else:
        time_str = dt.strftime('%H:%M:%S')
    return f'{date_str} {time_str} {tz_label}'

def _update_camera_health(camera_id: str, online: bool) -> None:
    with _camera_health_lock:
        state = _camera_health_state.get(camera_id, {'online': True, 'offline_since': None, 'offline_notified': False, 'recovery_notified': False})
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
        _camera_health_state[camera_id] = state
    if transition == 'offline':
        log_camera_diagnostic(camera_id, 'camera_offline', 'Camera went offline (detection unavailable).', severity='warning')
    elif transition == 'online':
        log_camera_diagnostic(camera_id, 'camera_online', 'Camera recovered and is back online.', severity='info')

def _camera_offline_notification_eligible(camera_id: str) -> bool:
    delay_minutes = int(effective_camera_offline_alert_settings().get('offline_delay_minutes', 1))
    delay_seconds = max(0, delay_minutes * 60)
    with _camera_health_lock:
        state = _camera_health_state.get(camera_id)
        if not state or state.get('online', True):
            return False
        if state.get('offline_notified'):
            return False
        offline_since = state.get('offline_since')
        if offline_since is None:
            return False
        return time.time() - offline_since >= delay_seconds

def _camera_recovery_notification_eligible(camera_id: str) -> bool:
    with _camera_health_lock:
        state = _camera_health_state.get(camera_id)
        if not state or not state.get('online', True):
            return False
        if state.get('recovery_notified'):
            return False
        return state.get('offline_notified', False)

def _mark_camera_offline_notified(camera_id: str) -> None:
    with _camera_health_lock:
        state = _camera_health_state.get(camera_id)
        if state:
            state['offline_notified'] = True

def _mark_camera_recovery_notified(camera_id: str) -> None:
    with _camera_health_lock:
        state = _camera_health_state.get(camera_id)
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
    push_settings_obj = effective_push_notification_settings()
    if push_settings_obj.get('enabled'):
        try:
            notifier = PushNotificationService(push_settings_obj)
            notifier._deliver(title, body)
        except Exception as exc:
            logger.warning('Push notify failed for camera %s %s: %s', camera_id, event_type, exc)
    email_settings_obj = effective_email_alert_settings()
    if email_settings_obj.get('enabled'):
        try:
            mailer = EmailAlertService(email_settings_obj)
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
            logger.warning('Email notify failed for camera %s %s: %s', camera_id, event_type, exc)
    if event_type == 'offline':
        _mark_camera_offline_notified(camera_id)
    else:
        _mark_camera_recovery_notified(camera_id)

def _check_cameras_health() -> None:
    for cfg in list(cameras_config):
        cam_id = str(cfg.get('id') or '')
        cam_name = str(cfg.get('name') or cam_id or 'Unknown')
        if not cam_id:
            continue
        retry_after = live_detection_retry_after.get(cam_id, 0)
        now = time.time()
        camera_online = not (retry_after and now < retry_after)
        _update_camera_health(cam_id, camera_online)
        if _camera_offline_notification_eligible(cam_id):
            _deliver_camera_offline_notification(cam_id, cam_name, 'offline')
        elif _camera_recovery_notification_eligible(cam_id):
            _deliver_camera_offline_notification(cam_id, cam_name, 'recovery')

def _non_empty_setting(settings: dict[str, Any], key: str) -> str:
    return str(settings.get(key) or '').strip()

def build_stream_url(settings: dict[str, Any]) -> str:
    stream_url = _non_empty_setting(settings, 'stream_url')
    if stream_url:
        username = _non_empty_setting(settings, 'username')
        password = _non_empty_setting(settings, 'password')
        parsed = urlsplit(stream_url)
        if username and parsed.scheme in {'rtsp', 'rtsps'} and parsed.netloc and ('@' not in parsed.netloc):
            credentials = quote(username, safe='')
            if password:
                credentials += f":{quote(password, safe='')}"
            return urlunsplit((parsed.scheme, f'{credentials}@{parsed.netloc}', parsed.path, parsed.query, parsed.fragment))
        return stream_url
    host = _non_empty_setting(settings, 'host')
    if not host:
        return ''
    username = _non_empty_setting(settings, 'username')
    password = _non_empty_setting(settings, 'password')
    try:
        port = int(settings.get('port') or 554)
    except (TypeError, ValueError):
        port = 554
    path = _non_empty_setting(settings, 'path') or 'stream1'
    path = path.lstrip('/')
    credentials = ''
    if username:
        credentials = quote(username, safe='')
        if password:
            credentials += f":{quote(password, safe='')}"
        credentials += '@'
    return f'rtsp://{credentials}{host}:{port}/{path}'

def camera_default_name(settings: dict[str, Any], fallback: str='Primary Camera') -> str:
    return str(settings.get('name') or settings.get('device') or fallback).strip() or fallback

def normalize_camera_id(value: Any, fallback: str='camera-1') -> str:
    camera_id = re.sub('[^a-zA-Z0-9_-]+', '-', str(value or '').strip().lower()).strip('-')
    return camera_id or fallback

def default_camera_detection_settings() -> dict[str, Any]:
    return {'object_detection_enabled': True, 'zones': []}

def normalize_bool_setting(value: Any, default: bool=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}
_LABEL_ALIASES: dict[str, str] = {'human': 'person', 'people': 'person', 'pedestrian': 'person'}

def normalize_label_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_labels = value.split(',')
    elif isinstance(value, list):
        raw_labels = value
    else:
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for raw_label in raw_labels:
        label = _LABEL_ALIASES.get(str(raw_label).strip().lower(), str(raw_label).strip().lower())
        if label and label not in seen:
            labels.append(label)
            seen.add(label)
    return labels

def normalize_email_recipients(value: Any) -> list[str]:
    raw_recipients = value.split(',') if isinstance(value, str) else value
    if not isinstance(raw_recipients, list):
        return []
    recipients: list[str] = []
    seen: set[str] = set()
    for raw_recipient in raw_recipients:
        recipient = str(raw_recipient).strip()
        if recipient and '@' in recipient and (recipient.lower() not in seen):
            recipients.append(recipient)
            seen.add(recipient.lower())
    return recipients

def normalize_zone_object_rules(zone: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rules = zone.get('object_rules')
    if isinstance(raw_rules, list):
        source_rules = raw_rules
    else:
        source_rules = [{'label': label} for label in normalize_label_list(zone.get('object_labels', []))]
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in source_rules:
        if not isinstance(rule, dict):
            continue
        labels = normalize_label_list(rule.get('label') or '')
        if not labels:
            continue
        label = labels[0]
        if label in seen:
            continue
        seen.add(label)
        try:
            min_confidence = float(rule.get('min_confidence', 0.5))
        except (TypeError, ValueError):
            min_confidence = 0.5
        try:
            cooldown_seconds = int(rule.get('cooldown_seconds', 60))
        except (TypeError, ValueError):
            cooldown_seconds = 60
        rules.append({'label': label, 'enabled': normalize_bool_setting(rule.get('enabled'), True), 'record_on_detect': normalize_bool_setting(rule.get('record_on_detect'), True), 'min_confidence': max(0.0, min(1.0, min_confidence)), 'cooldown_seconds': max(0, cooldown_seconds), 'email_enabled': normalize_bool_setting(rule.get('email_enabled'), False), 'email_recipients': normalize_email_recipients(rule.get('email_recipients', [])), 'active_start': str(rule.get('active_start') or '').strip() or None, 'active_end': str(rule.get('active_end') or '').strip() or None, 'notify_start': str(rule.get('notify_start') or '').strip() or None, 'notify_end': str(rule.get('notify_end') or '').strip() or None, 'push_enabled': normalize_bool_setting(rule.get('push_enabled'), False)})
    return rules

def zone_motion_min_confidence(zone: dict[str, Any]) -> float:
    for rule in zone.get('object_rules', []):
        if str(rule.get('label') or '').strip().lower() == 'motion' and rule.get('enabled', True):
            try:
                return max(0.0, min(1.0, float(rule.get('min_confidence', 0.45))))
            except (TypeError, ValueError):
                return 0.45
    return 0.45

def normalize_camera_recording_settings(settings: Any) -> dict[str, Any]:
    raw = settings if isinstance(settings, dict) else {}
    return {'continuous': normalize_bool_setting(raw.get('continuous'), False)}

def normalize_camera_ptz_settings(settings: Any) -> dict[str, Any]:
    raw = settings if isinstance(settings, dict) else {}
    protocol = str(raw.get('protocol') or 'onvif').strip().lower()
    if protocol not in {'onvif', 'tcp_pelcod'}:
        protocol = 'onvif'

    def _int(value: Any, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(value or default)))
        except (TypeError, ValueError):
            return default
    return {'enabled': normalize_bool_setting(raw.get('enabled'), False), 'protocol': protocol, 'http_port': _int(raw.get('http_port'), 80, 1, 65535), 'port': _int(raw.get('port'), 6060, 1, 65535), 'address': _int(raw.get('address'), 1, 1, 255), 'speed': _int(raw.get('speed'), 5, 1, 8)}

def normalize_zone_point(point: Any) -> dict[str, float] | None:
    if not isinstance(point, dict):
        return None
    try:
        x = max(0.0, min(1.0, float(point.get('x') or 0)))
        y = max(0.0, min(1.0, float(point.get('y') or 0)))
    except (TypeError, ValueError):
        return None
    return {'x': round(x, 4), 'y': round(y, 4)}

def rectangle_zone_points(x: float, y: float, width: float, height: float) -> list[dict[str, float]]:
    return [{'x': round(x, 4), 'y': round(y, 4)}, {'x': round(x + width, 4), 'y': round(y, 4)}, {'x': round(x + width, 4), 'y': round(y + height, 4)}, {'x': round(x, 4), 'y': round(y + height, 4)}]

def zone_bounds(points: list[dict[str, float]]) -> tuple[float, float, float, float]:
    xs = [point['x'] for point in points]
    ys = [point['y'] for point in points]
    left = min(xs)
    top = min(ys)
    right = max(xs)
    bottom = max(ys)
    return (left, top, max(0.01, right - left), max(0.01, bottom - top))

def normalize_monitoring_zones(zones: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(zones, list):
        return normalized
    for index, zone in enumerate(zones, start=1):
        if not isinstance(zone, dict):
            continue
        try:
            x = max(0.0, min(1.0, float(zone.get('x') or 0)))
        except (TypeError, ValueError):
            x = 0.0
        try:
            y = max(0.0, min(1.0, float(zone.get('y') or 0)))
        except (TypeError, ValueError):
            y = 0.0
        try:
            width = max(0.01, min(1.0 - x, float(zone.get('width') or 0)))
        except (TypeError, ValueError):
            width = 0.01
        try:
            height = max(0.01, min(1.0 - y, float(zone.get('height') or 0)))
        except (TypeError, ValueError):
            height = 0.01
        points = [point for point in (normalize_zone_point(point) for point in zone.get('points') or []) if point is not None]
        if len(points) < 3:
            points = rectangle_zone_points(x, y, width, height)
        x, y, width, height = zone_bounds(points)
        object_rules = normalize_zone_object_rules(zone)
        had_monitor_motion = 'monitor_motion' in zone and bool(zone['monitor_motion'])
        if had_monitor_motion and (not any((str(r.get('label') or '').strip().lower() == 'motion' for r in object_rules))):
            object_rules.insert(0, {'label': 'motion', 'enabled': True, 'record_on_detect': True, 'min_confidence': 0.45, 'cooldown_seconds': 60, 'email_enabled': False, 'email_recipients': [], 'active_start': None, 'active_end': None, 'notify_start': None, 'notify_end': None, 'push_enabled': False})
        monitor_motion = any((str(r.get('label') or '').strip().lower() == 'motion' and r.get('enabled', True) for r in object_rules))
        normalized.append({'id': normalize_camera_id(zone.get('id'), f'zone-{index}'), 'name': str(zone.get('name') or f'Zone {index}').strip() or f'Zone {index}', 'x': round(x, 4), 'y': round(y, 4), 'width': round(width, 4), 'height': round(height, 4), 'points': points, 'enabled': bool(zone.get('enabled', True)), 'monitor_motion': monitor_motion, 'monitor_objects': bool(zone.get('monitor_objects', True)), 'object_labels': [rule['label'] for rule in object_rules if str(rule.get('label') or '').strip().lower() != 'motion'], 'object_rules': object_rules})
    return normalized

def _normalize_camera_sound_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    enabled = normalize_bool_setting(raw.get('enabled'), False)
    raw_rules = raw.get('rules') if isinstance(raw.get('rules'), list) else []
    saved: dict[str, dict[str, Any]] = {}
    for r in raw_rules:
        if not isinstance(r, dict):
            continue
        cls = str(r.get('class') or '').strip()
        if cls in SOUND_CLASSES:
            saved[cls] = r
    defaults_by_class: dict[str, dict[str, Any]] = {d['class']: d for d in DEFAULT_RULES}
    rules = []
    for cls, r in saved.items():
        default = defaults_by_class.get(cls)
        if not default:
            continue
        try:
            threshold = max(0.1, min(1.0, float(r.get('confidence_threshold', default['confidence_threshold']))))
        except (TypeError, ValueError):
            threshold = default['confidence_threshold']
        try:
            cooldown = max(5.0, float(r.get('cooldown_seconds', default['cooldown_seconds'])))
        except (TypeError, ValueError):
            cooldown = float(default['cooldown_seconds'])
        rules.append({'class': cls, 'name': str(r.get('name') or SOUND_CLASSES[cls]['label']), 'enabled': normalize_bool_setting(r.get('enabled'), False), 'record_on_detect': normalize_bool_setting(r.get('record_on_detect'), True), 'confidence_threshold': threshold, 'cooldown_seconds': cooldown, 'email_enabled': normalize_bool_setting(r.get('email_enabled'), False), 'email_recipients': normalize_email_recipients(r.get('email_recipients', [])), 'push_enabled': normalize_bool_setting(r.get('push_enabled'), False), 'active_start': str(r.get('active_start') or '').strip() or None, 'active_end': str(r.get('active_end') or '').strip() or None, 'notify_start': str(r.get('notify_start') or '').strip() or None, 'notify_end': str(r.get('notify_end') or '').strip() or None})
    return {'enabled': enabled, 'rules': rules}

def _migrate_legacy_camera_motion(detection: dict[str, Any]) -> None:
    """Fold the removed camera-level motion master switch into each zone's
    motion rule, then drop the legacy fields.

    Motion is configured per zone via each zone's 'motion' object rule; there
    is no camera-level motion setting any more. If a stored config still has
    the old camera-level switch turned off (either the short-lived
    ``detection.motion.enabled`` dict or the older flat ``motion_enabled``
    field), disable the motion rule in every zone so motion stays off after
    the upgrade. The legacy record/email/push flags are dropped: the zone
    rule's own checkboxes are the single source of truth.
    """
    legacy = detection.pop('motion', None)
    flat_enabled = detection.pop('motion_enabled', None)
    detection.pop('motion_email_enabled', None)
    enabled = True
    if isinstance(legacy, dict):
        enabled = normalize_bool_setting(legacy.get('enabled'), True)
    elif flat_enabled is not None:
        enabled = normalize_bool_setting(flat_enabled, True)
    if enabled:
        return
    for zone in detection.get('zones', []):
        zone['monitor_motion'] = False
        for rule in zone.get('object_rules', []):
            if str(rule.get('label') or '').strip().lower() == 'motion':
                rule['enabled'] = False

def normalize_camera_settings(settings: dict[str, Any], index: int=1) -> dict[str, Any]:
    camera_settings = dict(settings or {})
    camera_settings['id'] = normalize_camera_id(camera_settings.get('id'), f'camera-{index}')
    camera_settings['name'] = camera_default_name(camera_settings, f'Camera {index}')
    camera_settings['backend'] = str(camera_settings.get('backend') or 'onvif').lower()
    camera_settings['width'] = int(camera_settings.get('width') or 1280)
    camera_settings['height'] = int(camera_settings.get('height') or 720)
    camera_settings['fps'] = int(camera_settings.get('fps') or 15)
    raw_stale = camera_settings.get('stale_frame_grabs')
    camera_settings['stale_frame_grabs'] = int(raw_stale) if raw_stale is not None else None
    detection = default_camera_detection_settings()
    if isinstance(camera_settings.get('detection'), dict):
        detection.update(camera_settings['detection'])
    detection['object_detection_enabled'] = bool(detection.get('object_detection_enabled', True))
    detection['object_labels'] = normalize_label_list(detection.get('object_labels', []))
    detection['zones'] = normalize_monitoring_zones(detection.get('zones', []))
    detection['sound'] = _normalize_camera_sound_settings(detection.get('sound'))
    _migrate_legacy_camera_motion(detection)
    camera_settings['detection'] = detection
    camera_settings['recording'] = normalize_camera_recording_settings(camera_settings.get('recording'))
    camera_settings['ptz'] = normalize_camera_ptz_settings(camera_settings.get('ptz'))
    return camera_settings

def effective_cameras_config() -> list[dict[str, Any]]:
    override = database.get_setting('cameras')
    if isinstance(override, list) and override:
        return [normalize_camera_settings(camera_settings, index) for index, camera_settings in enumerate(override, start=1)]
    return []

def get_camera_config(camera_id: str | None=None) -> dict[str, Any]:
    if not cameras_config:
        return camera_config
    if camera_id:
        normalized = normalize_camera_id(camera_id)
        for configured in cameras_config:
            if configured.get('id') == normalized:
                return configured
        raise HTTPException(status_code=404, detail='Camera not found')
    return cameras_config[0]

def get_camera_instance(camera_id: str | None=None):
    configured = get_camera_config(camera_id)
    instance = camera_instances.get(str(configured['id']))
    if instance is None:
        raise HTTPException(status_code=404, detail='Camera not found')
    return instance

def detection_center_in_zone(detection: dict[str, Any], zone: dict[str, Any]) -> bool:
    box = detection.get('box') or {}
    center_x = float(box.get('x') or 0) + float(box.get('width') or 0) / 2
    center_y = float(box.get('y') or 0) + float(box.get('height') or 0) / 2
    points = zone.get('points') or []
    if isinstance(points, list) and len(points) >= 3:
        return point_in_polygon(center_x, center_y, points)
    return float(zone['x']) <= center_x <= float(zone['x']) + float(zone['width']) and float(zone['y']) <= center_y <= float(zone['y']) + float(zone['height'])

def detection_overlap_ratio_with_zone_rect(detection: dict[str, Any], zone: dict[str, Any]) -> float:
    box = detection.get('box') or {}
    x = float(box.get('x') or 0)
    y = float(box.get('y') or 0)
    width = max(0.0, float(box.get('width') or 0))
    height = max(0.0, float(box.get('height') or 0))
    if width <= 0 or height <= 0:
        return 0.0
    dx1 = x
    dy1 = y
    dx2 = x + width
    dy2 = y + height
    zx1 = float(zone.get('x') or 0)
    zy1 = float(zone.get('y') or 0)
    zw = max(0.0, float(zone.get('width') or 0))
    zh = max(0.0, float(zone.get('height') or 0))
    zx2 = zx1 + zw
    zy2 = zy1 + zh
    ix1 = max(dx1, zx1)
    iy1 = max(dy1, zy1)
    ix2 = min(dx2, zx2)
    iy2 = min(dy2, zy2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    detection_area = width * height
    return intersection / detection_area if detection_area > 0 else 0.0

def detection_matches_zone(detection: dict[str, Any], zone: dict[str, Any], *, min_overlap_ratio: float=0.2) -> bool:
    if detection_center_in_zone(detection, zone):
        return True
    points = zone.get('points') or []
    if isinstance(points, list) and len(points) >= 3:
        return False
    return detection_overlap_ratio_with_zone_rect(detection, zone) >= min_overlap_ratio

def point_in_polygon(x: float, y: float, points: list[dict[str, Any]]) -> bool:
    if len(points) < 3:
        return False
    inside = False
    previous = points[-1]
    for current in points:
        try:
            current_x = float(current.get('x') or 0)
            current_y = float(current.get('y') or 0)
            previous_x = float(previous.get('x') or 0)
            previous_y = float(previous.get('y') or 0)
        except (TypeError, ValueError):
            previous = current
            continue
        if point_on_segment(x, y, previous_x, previous_y, current_x, current_y):
            return True
        intersects = (current_y > y) != (previous_y > y)
        if intersects:
            slope_x = (previous_x - current_x) * (y - current_y) / (previous_y - current_y or 1e-12) + current_x
            if x < slope_x:
                inside = not inside
        previous = current
    return inside

def point_on_segment(x: float, y: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    cross = (y - y1) * (x2 - x1) - (x - x1) * (y2 - y1)
    if abs(cross) > 1e-09:
        return False
    return min(x1, x2) - 1e-09 <= x <= max(x1, x2) + 1e-09 and min(y1, y2) - 1e-09 <= y <= max(y1, y2) + 1e-09

def filter_detections_for_camera(detections: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    if not detection_settings.get('object_detection_enabled', True):
        return []
    return filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_objects')

def _zone_pixel_motion_fraction(diff_mask: Any, zone: dict[str, Any]) -> float:
    """Return the fraction of pixels inside a zone's bounding box that changed.

    ``diff_mask`` is the boolean (H×W) array from ``detect_frame_motion`` at
    ``_MOTION_FRAME_H × _MOTION_FRAME_W`` resolution.  Zone coordinates are
    normalised (0–1) and are converted to pixel indices before slicing.
    """
    try:
        import numpy as np
        x = zone.get('x')
        y = zone.get('y')
        w = zone.get('width')
        h = zone.get('height')
        points = zone.get('points') or []
        if (x is None or w is None) and isinstance(points, list) and (len(points) >= 2):
            xs = [float(p.get('x', 0)) for p in points if isinstance(p, dict)]
            ys = [float(p.get('y', 0)) for p in points if isinstance(p, dict)]
            if xs and ys:
                x = x if x is not None else min(xs)
                y = y if y is not None else min(ys)
                w = w if w is not None else max(xs) - float(x)
                h = h if h is not None else max(ys) - float(y)
        x = float(x if x is not None else 0)
        y = float(y if y is not None else 0)
        w = float(w if w is not None else 1)
        h = float(h if h is not None else 1)
        px1 = max(0, int(x * _MOTION_FRAME_W))
        py1 = max(0, int(y * _MOTION_FRAME_H))
        px2 = min(_MOTION_FRAME_W, max(px1 + 1, int(round((x + w) * _MOTION_FRAME_W))))
        py2 = min(_MOTION_FRAME_H, max(py1 + 1, int(round((y + h) * _MOTION_FRAME_H))))
        return float(np.mean(diff_mask[py1:py2, px1:px2]))
    except Exception:
        return 0.0

def zone_motion_detections(settings: dict[str, Any], frame_motion_confidence: float=0.5, *, diff_mask: Any=None, gate_fraction: float=_MOTION_GATE_FRACTION, scale_fraction: float=_MOTION_SCALE_FRACTION) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get('monitor_motion', True)]
    if not zones:
        return []
    seen_zones: set[str] = set()
    result: list[dict[str, Any]] = []
    for zone in zones:
        zone_id = str(zone.get('id') or zone.get('name') or id(zone))
        if zone_id in seen_zones:
            continue
        if diff_mask is not None:
            zone_fraction = _zone_pixel_motion_fraction(diff_mask, zone)
            if zone_fraction < gate_fraction:
                continue
            zone_confidence = round(min(1.0, zone_fraction / max(scale_fraction, 1e-09)), 3)
        else:
            zone_confidence = frame_motion_confidence
        conf_threshold = zone_motion_min_confidence(zone)
        if zone_confidence < conf_threshold:
            continue
        seen_zones.add(zone_id)
        result.append({'confidence': zone_confidence, 'zone_id': zone_id, 'box': {'x': float(zone.get('x', 0)), 'y': float(zone.get('y', 0)), 'width': float(zone.get('width', 1)), 'height': float(zone.get('height', 1))}})
    return result

def detection_label_allowed_for_zone(detection: dict[str, Any], zone: dict[str, Any], camera_labels: set[str]) -> bool:
    zone_labels = set(normalize_label_list(zone.get('object_labels', [])))
    allowed_labels = zone_labels or camera_labels
    if not allowed_labels:
        return True
    label = str(detection.get('label') or '').strip().lower()
    return _LABEL_ALIASES.get(label, label) in allowed_labels

def filter_detections_for_camera_zones(detections: list[dict[str, Any]], settings: dict[str, Any], *, zone_monitor_key: str, require_zones: bool=False) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get(zone_monitor_key, True)]
    camera_labels = set(normalize_label_list(detection_settings.get('object_labels', [])))
    if not zones:
        if zone_monitor_key == 'monitor_objects' and camera_labels and (not require_zones):
            return [detection for detection in detections if str(detection.get('label') or '').strip().lower() in camera_labels]
        return [] if require_zones else detections
    return [detection for detection in detections if any((detection_matches_zone(detection, zone) and (zone_monitor_key != 'monitor_objects' or detection_label_allowed_for_zone(detection, zone, camera_labels)) for zone in zones))]

def zone_object_rule_matches(settings: dict[str, Any], detection: dict[str, Any], *, action: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get('monitor_objects', True)]
    label = str(detection.get('label') or '').strip().lower()
    label = _LABEL_ALIASES.get(label, label)
    if not label:
        return []
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for zone in zones:
        if not detection_matches_zone(detection, zone):
            continue
        for rule in zone.get('object_rules') or []:
            if not rule.get('enabled', True):
                continue
            if action == 'alert' and (not (rule.get('email_enabled') or rule.get('push_enabled'))):
                continue
            if action == 'record' and (not rule.get('record_on_detect', True)):
                continue
            if str(rule.get('label') or '').strip().lower() != label:
                continue
            if float(detection.get('confidence') or 0) < float(rule.get('min_confidence', 0.5)):
                continue
            matches.append((zone, rule))
    return matches

def zone_object_alert_rules(settings: dict[str, Any]) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get('monitor_objects', True)]
    rules: list[dict[str, Any]] = []
    camera_key = str(settings.get('id') or settings.get('name') or 'camera').strip() or 'camera'
    for zone in zones:
        zone_id = str(zone.get('id') or zone.get('name') or 'zone')
        for rule in zone.get('object_rules') or []:
            if not rule.get('enabled', True) or not (rule.get('email_enabled') or rule.get('push_enabled')):
                continue
            label = str(rule.get('label') or '').strip().lower()
            if not label:
                continue
            rules.append({'name': zone_rule_name(settings, zone, rule), 'cooldown_key': f'{camera_key}::{zone_id}::{label}', 'object': label, 'zone_id': zone_id, 'min_confidence': rule.get('min_confidence', 0.5), 'cooldown_seconds': rule.get('cooldown_seconds', 60), 'enabled': True, 'email_enabled': bool(rule.get('email_enabled', False)), 'email_recipients': normalize_email_recipients(rule.get('email_recipients', [])), 'push_enabled': bool(rule.get('push_enabled', False)), 'active_start': rule.get('active_start'), 'active_end': rule.get('active_end'), 'notify_start': rule.get('notify_start'), 'notify_end': rule.get('notify_end')})
    return rules

def zone_rule_name(settings: dict[str, Any], zone: dict[str, Any], rule: dict[str, Any]) -> str:
    camera_name = str(settings.get('name') or settings.get('id') or 'Camera')
    zone_name = str(zone.get('name') or zone.get('id') or 'Zone')
    label = str(rule.get('label') or '').strip().lower()
    return f'{camera_name} / {zone_name} / {label}'

def zone_alert_detections(settings: dict[str, Any], detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for index, detection in enumerate(detections):
        for zone, _rule in zone_object_rule_matches(settings, detection, action='alert'):
            zone_id = str(zone.get('id') or zone.get('name') or 'zone')
            key = (index, zone_id)
            if key in seen:
                continue
            seen.add(key)
            matched.append({**detection, 'zone_id': zone_id, 'zone_name': zone.get('name') or zone_id})
    return matched

def zone_name_for_detection(settings: dict[str, Any], detection: dict[str, Any]) -> str | None:
    for action in ('alert', 'record'):
        matches = zone_object_rule_matches(settings, detection, action=action)
        if matches:
            zone = matches[0][0]
            zone_name = str(zone.get('name') or zone.get('id') or '').strip()
            return zone_name or None
    return None

def zone_record_on_detect(detection: dict[str, Any], settings: dict[str, Any]) -> bool:
    return bool(zone_object_rule_matches(settings, detection, action='record'))

def zone_motion_record_on_detect(settings: dict[str, Any]) -> bool:
    """Return True if any enabled motion-monitoring zone has a motion rule with record_on_detect=True.

    zone_record_on_detect / zone_object_rule_matches filter by monitor_objects=True and therefore
    skip motion-only zones (monitor_objects=False, monitor_motion=True). This helper checks the
    correct monitor_motion axis so motion-only zones are not silently excluded from recording.
    """
    detection_settings = settings.get('detection') or {}
    for zone in detection_settings.get('zones', []):
        if not zone.get('enabled', True) or not zone.get('monitor_motion', True):
            continue
        for rule in zone.get('object_rules') or []:
            if not rule.get('enabled', True):
                continue
            if str(rule.get('label') or '').strip().lower() == 'motion' and rule.get('record_on_detect', True):
                return True
    return False

def zone_detection_alert_rule_names(settings: dict[str, Any], detection: dict[str, Any]) -> set[str]:
    return {zone_rule_name(settings, zone, rule) for zone, rule in zone_object_rule_matches(settings, detection, action='alert')}

def detection_has_matching_record_rule(detection: dict[str, Any], rules: list[dict[str, Any]]) -> bool:
    """Return True if any enabled alert rule covers this detection by label and confidence.

    Cooldown and time-window are intentionally ignored so a recording is created on every
    matching detection, not only when a new alert notification is emitted.
    """
    label = str(detection.get('label') or '').strip().lower()
    label = _LABEL_ALIASES.get(label, label)
    if not label:
        return False
    confidence = float(detection.get('confidence') or 0)
    for rule in rules:
        if not rule.get('enabled', True):
            continue
        rule_object = str(rule.get('object') or '').strip().lower()
        rule_object = _LABEL_ALIASES.get(rule_object, rule_object)
        if rule_object != label:
            continue
        try:
            min_conf = float(rule.get('min_confidence', 0.0 if label == 'motion' else 0.5))
        except (TypeError, ValueError):
            min_conf = 0.0 if label == 'motion' else 0.5
        if confidence >= min_conf:
            return True
    return False

def normalize_detection_boxes_for_frame(detections: list[dict[str, Any]], frame: dict[str, Any]) -> list[dict[str, Any]]:
    width = float(frame.get('width') or 0)
    height = float(frame.get('height') or 0)
    if width <= 0 or height <= 0:
        return detections
    normalized: list[dict[str, Any]] = []
    for detection in detections:
        box = detection.get('box') or {}
        if not isinstance(box, dict):
            normalized.append(detection)
            continue
        box_x = float(box.get('x') or 0)
        box_y = float(box.get('y') or 0)
        box_width = float(box.get('width') or 0)
        box_height = float(box.get('height') or 0)
        if max(box_x, box_y, box_width, box_height) <= 1:
            normalized.append(detection)
            continue
        normalized.append({**detection, 'box': {'x': round(box_x / width, 4), 'y': round(box_y / height, 4), 'width': round(box_width / width, 4), 'height': round(box_height / height, 4)}})
    return normalized

def update_live_detection_status(camera_id: str, **updates: Any) -> None:
    with live_detection_status_lock:
        live_detection_status[camera_id] = {**live_detection_status.get(camera_id, {}), **updates, 'camera_id': camera_id, 'updated_at': datetime.now(timezone.utc).isoformat()}

def record_live_detection_history(camera_id: str, detections: list[dict[str, Any]], sample_ts: float | None=None, *, live_config: dict[str, Any] | None=None) -> None:
    """Append one monitor cycle's detections to the camera's rolling history.

    ``sample_ts`` must be when the analyzed frame was CAPTURED, not when
    inference finished: tracks sliced from this history are replayed against
    the recorded video, and stamping at completion shifts every box late by
    the inference duration - the playback overlay then trails moving objects.

    Empty cycles are recorded too: a recording track sliced from the history
    needs "nothing in frame" samples so playback overlays clear when an object
    leaves instead of holding the last box."""
    sample = [{'label': detection.get('label'), 'confidence': detection.get('confidence'), 'box': detection.get('box')} for detection in detections if isinstance(detection.get('box'), dict)]
    if sample_ts is None:
        sample_ts = time.time()
    history_minutes = max(1, int((live_config or effective_live_config()).get('detection_history_minutes', 10)))
    history_maxlen = max(120, history_minutes * 120)
    with live_detection_history_lock:
        history = live_detection_history.get(camera_id)
        if history is None:
            history = deque(maxlen=history_maxlen)
            live_detection_history[camera_id] = history
        history.append((sample_ts, sample))

def build_track_from_live_history(camera_id: str | None, start_ts: float, end_ts: float) -> list[dict[str, Any]] | None:
    """Slice the monitor's detection history into a clip-relative track.

    Returns ``[{"t": seconds_from_start, "detections": [...]}]`` or ``None``
    when the history has no samples inside the window (camera idle, monitor
    disabled, or the clip predates the in-memory history)."""
    if not camera_id or end_ts <= start_ts:
        return None
    with live_detection_history_lock:
        samples = list(live_detection_history.get(str(camera_id), ()))
    track = [{'t': round(sample_ts - start_ts, 3), 'detections': sample_detections} for sample_ts, sample_detections in samples if start_ts <= sample_ts <= end_ts]
    return track or None

def detection_label_set(detections: list[dict[str, Any]]) -> set[str]:
    return {str(detection.get('label') or '').strip().lower() for detection in detections if str(detection.get('label') or '').strip()}

def detect_frame_motion(camera_id: str, image: Any, *, pixel_threshold: float=_MOTION_PIXEL_THRESHOLD, gate_fraction: float=_MOTION_GATE_FRACTION, scale_fraction: float=_MOTION_SCALE_FRACTION, background_alpha: float=_MOTION_BACKGROUND_ALPHA) -> tuple[bool, float, Any]:
    """Adaptive-background motion gate. Returns (has_motion, confidence 0-1, diff_mask).

    ``image`` may be a BGR numpy array (from ``read_frame``) or JPEG bytes
    (legacy callers).  When a numpy array is provided the PIL decode is
    skipped, saving ~5-15 ms per cycle.

    Threshold parameters default to module-level constants but can be
    overridden via live settings so operators can tune sensitivity without
    touching code.

    Returns ``(has_motion, frame_confidence, diff_mask)`` where ``diff_mask``
    is a boolean (H×W) numpy array indicating which thumbnail pixels changed by
    more than ``pixel_threshold``.  Callers can slice ``diff_mask`` to compute
    per-zone confidence scores instead of using the frame-wide value.
    ``diff_mask`` is ``None`` on the first frame or when an error occurs.
    """
    try:
        import numpy as np
        if hasattr(image, 'shape') and hasattr(image, 'dtype'):
            import cv2
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            resized = cv2.resize(gray, (_MOTION_FRAME_W, _MOTION_FRAME_H), interpolation=cv2.INTER_NEAREST)
            current = resized.astype(np.float32)
        else:
            from PIL import Image as _Image
            img = _Image.open(io.BytesIO(image)).convert('L').resize((_MOTION_FRAME_W, _MOTION_FRAME_H), _Image.NEAREST)
            current = np.array(img, dtype=np.float32)
        with _frame_motion_lock:
            background = _frame_motion_prev.get(camera_id)
            if background is None:
                _frame_motion_prev[camera_id] = current
                _frame_motion_error_cameras.discard(camera_id)
                return (False, 0.0, None)
            diff_mask = np.abs(current - background) > pixel_threshold
            changed_fraction = float(np.mean(diff_mask))
            if changed_fraction < gate_fraction:
                updated_bg = (1.0 - background_alpha) * background + background_alpha * current
                _frame_motion_prev[camera_id] = updated_bg
        _frame_motion_error_cameras.discard(camera_id)
        if changed_fraction < gate_fraction:
            return (False, 0.0, diff_mask)
        return (True, round(min(1.0, changed_fraction / scale_fraction), 3), diff_mask)
    except Exception as exc:
        if camera_id not in _frame_motion_error_cameras:
            logger.warning('Motion gate unavailable for camera %s: %s; failing open', camera_id, exc)
            _frame_motion_error_cameras.add(camera_id)
        return (True, 0.4, None)

def live_event_is_debounced(camera_id: str, labels: set[str], debounce_seconds: float) -> bool:
    if debounce_seconds <= 0 or not labels:
        return False
    with live_event_last_emitted_lock:
        previous = live_event_last_emitted.get(camera_id)
    if not previous:
        return False
    elapsed = time.time() - float(previous.get('timestamp', 0))
    if elapsed > debounce_seconds:
        return False
    if labels <= {'motion'}:
        return True
    previous_labels = {str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()}
    return bool(previous_labels & labels)

def remember_live_event(camera_id: str, labels: set[str], *, merge: bool=False) -> None:
    if not labels:
        return
    with live_event_last_emitted_lock:
        if merge:
            previous = live_event_last_emitted.get(camera_id) or {}
            labels = labels | {str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()}
        live_event_last_emitted[camera_id] = {'timestamp': time.time(), 'labels': sorted(labels)}

def clear_live_camera_backoff(camera_id: str) -> None:
    with _live_backoff_lock:
        was_backed_off = bool(live_detection_failure_count.get(camera_id))
        live_detection_retry_after.pop(camera_id, None)
        live_detection_failure_count.pop(camera_id, None)
    if was_backed_off:
        log_camera_diagnostic(camera_id, 'detection_recovered', 'Live detection resumed after a successful frame read.', severity='info')
    with _frame_motion_lock:
        _frame_motion_prev.pop(camera_id, None)
    _frame_motion_error_cameras.discard(camera_id)
    _periodic_scan_last_ts.pop(camera_id, None)

def extend_active_rtsp_recording(*, camera_id: str, event_time: str, recording_config: dict[str, Any] | None, detections: list[dict[str, Any]] | None=None) -> int | None:
    try:
        event_dt = datetime.fromisoformat(str(event_time))
    except ValueError:
        event_dt = datetime.now(timezone.utc)
    if event_dt.tzinfo is None:
        event_dt = event_dt.replace(tzinfo=timezone.utc)
    config = recording_config or effective_recording_config()
    extension_step_seconds = max(0, int(config.get('extension_step_seconds', config.get('post_event_seconds', 10))))
    extend_until = event_dt.timestamp() + extension_step_seconds
    with active_rtsp_recordings_lock:
        session = active_rtsp_recordings.get(camera_id)
        if not session:
            return None
        current_deadline = float(session.get('capture_deadline_ts') or 0)
        max_deadline = float(session.get('max_capture_deadline_ts') or current_deadline)
        new_deadline = min(max_deadline, max(current_deadline, extend_until))
        if new_deadline <= current_deadline:
            return int(session.get('recording_id'))
        session['capture_deadline_ts'] = new_deadline
        start_ts = float(session.get('start_capture_ts') or new_deadline)
        ended_at = datetime.fromtimestamp(new_deadline, tz=timezone.utc).isoformat()
        duration_seconds = max(1.0, new_deadline - start_ts)
        recording_id = int(session.get('recording_id'))
    database.update_recording_timing(recording_id, ended_at=ended_at, duration_seconds=duration_seconds)
    if detections:
        should_record, trigger_type, trigger_label = recording_service.should_record(detections, config)
        new_labels = detection_label_strings(detections)
        if new_labels:
            database.add_recording_labels(recording_id, new_labels, source='extension', confidences=detection_label_confidences(detections))
        if should_record and trigger_label:
            current_recording = database.get_recording(recording_id) or {}
            current_label = str(current_recording.get('trigger_label') or '').strip().lower()
            current_type = str(current_recording.get('trigger_type') or '').strip().lower()
            generic_labels = {'', 'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous'}
            candidate_label = str(trigger_label).strip().lower()
            if candidate_label not in generic_labels and (current_label in generic_labels or current_type in {'motion', 'human'}):
                database.update_recording_trigger(recording_id, trigger_type=trigger_type, trigger_label=candidate_label)
    return recording_id

def detection_label_strings(detections: list[dict[str, Any]]) -> list[str]:
    """Return the sorted, de-duplicated, non-generic labels from a detections list.

    Used to seed the recording_labels join table so a recording's "labels" field
    reflects every object that appeared inside it, not just the trigger_label.
    """
    if not detections:
        return []
    generic = {'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous', ''}
    seen: set[str] = set()
    out: list[str] = []
    for detection in detections:
        label = str(detection.get('label') or '').strip().lower()
        if not label or label in generic or label in seen:
            continue
        seen.add(label)
        out.append(label)
    out.sort()
    return out

def detection_label_confidences(detections: list[dict[str, Any]]) -> dict[str, float]:
    """Return the best confidence per non-generic label from a detections list.

    Used to persist a confidence alongside each recording label so the recordings
    list and timeline can show a percentage for secondary objects, not just the
    trigger object.
    """
    if not detections:
        return {}
    generic = {'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous', ''}
    best: dict[str, float] = {}
    for detection in detections:
        label = str(detection.get('label') or '').strip().lower()
        if not label or label in generic:
            continue
        try:
            confidence = float(detection.get('confidence'))
        except (TypeError, ValueError):
            continue
        if label not in best or confidence > best[label]:
            best[label] = confidence
    return best

def schedule_live_camera_backoff(camera_id: str, message: str) -> float:
    with _live_backoff_lock:
        failure_count = live_detection_failure_count.get(camera_id, 0) + 1
        live_detection_failure_count[camera_id] = failure_count
        backoff_seconds = min(300.0, max(10.0, 5.0 * 2 ** min(failure_count - 1, 5)))
        retry_after = time.time() + backoff_seconds
        live_detection_retry_after[camera_id] = retry_after
    update_live_detection_status(camera_id, state='error', reason=f'{message} Retrying in {int(backoff_seconds)}s.', detections=[])
    if failure_count == 1:
        log_camera_diagnostic(camera_id, 'detection_backoff', f'Live detection paused after error: {message}', severity='warning', details={'backoff_seconds': int(backoff_seconds)})
    return backoff_seconds

def live_detection_status_payload(camera_id: str | None=None) -> dict[str, Any]:
    selected_config = get_camera_config(camera_id)
    camera_key = str(selected_config.get('id') or camera_id or 'camera')
    ai_state = ai_status_payload()
    with live_detection_status_lock:
        status = live_detection_status.get(camera_key, {'state': 'waiting', 'reason': 'No live detection has run yet.'})
    return {'camera_id': camera_key, 'camera_name': selected_config.get('name'), 'ai_backend': ai_state['active_backend'], 'ai_configured_backend': ai_state['configured_backend'], 'ai_available': ai_state['inference_available'], 'ai_mode': ai_state['mode'], 'ai_error': ai_state['error'], **status}

def _camera_has_live_alert_stream(settings: dict[str, Any]) -> bool:
    return bool(build_stream_url(settings))

def read_ingest_frame(camera_id: str) -> tuple[Any, dict[str, Any]] | None:
    """Decode the latest frame the shared per-camera ingest wrote, as
    ``(bgr_image, frame_dict)``. This is how object detection and snapshots get
    frames without opening a second RTSP connection. Returns None when no fresh
    frame is available yet (ingest warming up or camera offline)."""
    sample = recording_service.latest_frame_jpeg(camera_id)
    if sample is None:
        return None
    jpeg_bytes, captured_ts = sample
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    frame = {'frame_number': 0, 'timestamp': captured_ts, 'width': int(width), 'height': int(height)}
    return (image, frame)

def run_live_alert_monitor_once(live_settings: dict[str, Any] | None=None) -> int:
    if live_settings is None:
        live_settings = effective_live_config()
    background_detection_enabled = normalize_bool_setting(live_settings.get('background_detection_enabled'), True)
    processed = 0
    for selected_config in list(cameras_config):
        camera_id = str(selected_config.get('id') or 'camera')
        if not _camera_has_live_alert_stream(selected_config):
            continue
        now = time.time()
        stream_url = build_stream_url(selected_config)
        cam_rec_config = camera_event_recording_config(selected_config)
        if stream_url:
            recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config)
            if cam_rec_config.get('continuous'):
                recording_service.start_continuous_chunk_recording(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config, on_chunk_complete=_make_continuous_chunk_callback(camera_id))
        if not background_detection_enabled:
            continue
        with _live_backoff_lock:
            retry_after = live_detection_retry_after.get(camera_id, 0)
        if retry_after and now < retry_after:
            continue
        detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.25))
        with live_detection_worker_lock:
            if camera_id in active_live_detection_cameras:
                continue
            if now - live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
                continue
            live_detection_last_checked[camera_id] = now
            active_live_detection_cameras.add(camera_id)

        def _detect_bg(cid: str=camera_id, cfg: dict[str, Any]=dict(selected_config)) -> None:
            try:
                sample = read_ingest_frame(cid)
                if sample is None:
                    if not recording_service.ingest_has_produced_frame(cid):
                        return
                    cam_instance = camera_instances.get(cid)
                    if cam_instance is not None and hasattr(cam_instance, 'read_jpeg'):
                        try:
                            import cv2
                            import numpy as np
                            jpeg_bytes, _frame_meta = cam_instance.read_jpeg()
                            img = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
                            if img is not None:
                                h, w = img.shape[:2]
                                sample = (img, {'frame_number': 0, 'timestamp': time.time(), 'width': w, 'height': h})
                        except Exception:
                            pass
                    if sample is None:
                        schedule_live_camera_backoff(cid, 'No fresh frame available from the camera ingest.')
                        return
                image, frame = sample
                clear_live_camera_backoff(cid)
                process_live_stream_alerts(image, frame, cfg, enforce_interval=False)
            except Exception as exc:
                logger.warning('Background live alert check failed for camera %s: %s', cid, exc)
                schedule_live_camera_backoff(cid, str(exc))
            finally:
                with live_detection_worker_lock:
                    active_live_detection_cameras.discard(cid)
        threading.Thread(target=_detect_bg, name=f'live-detection-{camera_id}', daemon=True).start()
        processed += 1
    return processed

def _prune_frame_motion_state() -> None:
    """Remove background model and scan timestamp entries for cameras no longer in the active config."""
    active_ids = {str(cfg.get('id') or '') for cfg in cameras_config if cfg.get('id')}
    with _frame_motion_lock:
        stale = [cid for cid in _frame_motion_prev if cid not in active_ids]
        for cid in stale:
            del _frame_motion_prev[cid]
    for cid in stale:
        _periodic_scan_last_ts.pop(cid, None)
        _frame_motion_error_cameras.discard(cid)
    if stale:
        logger.debug('Pruned stale motion state for cameras: %s', stale)

def live_alert_monitor_loop() -> None:
    _last_prune = 0.0
    while not live_alert_monitor_stop.is_set():
        live_settings = effective_live_config()
        run_live_alert_monitor_once(live_settings)
        _check_cameras_health()
        now = time.time()
        if now - _last_prune > 300:
            _prune_frame_motion_state()
            purge_camera_diagnostics_by_policy()
            _last_prune = now
        interval = max(0.1, float(live_settings.get('detection_interval_seconds', 0.25)))
        live_alert_monitor_stop.wait(interval)

def start_live_alert_monitor() -> None:
    global live_alert_monitor_thread
    if live_alert_monitor_thread and live_alert_monitor_thread.is_alive():
        return
    live_alert_monitor_stop.clear()
    live_alert_monitor_thread = threading.Thread(target=live_alert_monitor_loop, name='live-alert-monitor', daemon=True)
    live_alert_monitor_thread.start()

def stop_live_alert_monitor() -> None:
    global live_alert_monitor_thread
    live_alert_monitor_stop.set()
    if live_alert_monitor_thread and live_alert_monitor_thread.is_alive():
        live_alert_monitor_thread.join(timeout=5)
    live_alert_monitor_thread = None

def _on_sound_detected(camera_id: str, class_id: str, rule_name: str, confidence: float, meta: dict[str, Any]) -> None:
    """Callback invoked by a per-camera SoundDetector when a sound class is detected."""
    class_label = SOUND_CLASSES.get(class_id, {}).get('label', class_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    with _sound_statuses_lock:
        status = _sound_statuses.setdefault(camera_id, {})
        status['state'] = 'detected'
        status['last_detected_at'] = now_iso
        status['last_class'] = class_id
        status['last_class_label'] = class_label
        status['last_confidence'] = round(confidence, 3)
        status['backend'] = meta.get('backend', 'unknown')
    logger.info('Sound detected on %s: %s (confidence=%.2f, backend=%s)', camera_id, class_label, confidence, meta.get('backend'))
    cam_settings = next((c for c in cameras_config if str(c.get('id') or '') == camera_id), None)
    sound_rules = cam_settings.get('detection', {}).get('sound', {}).get('rules', []) if cam_settings else []
    fired_rule = next((r for r in sound_rules if r.get('class') == class_id), {})
    email_enabled = normalize_bool_setting(fired_rule.get('email_enabled'), False)
    email_recipients = normalize_email_recipients(fired_rule.get('email_recipients', []))
    push_enabled = normalize_bool_setting(fired_rule.get('push_enabled'), False)
    notify_enabled = email_enabled or push_enabled
    event_id = database.add_event(created_at=now_iso, source='sound', snapshot_path=None, detections=[], alert_triggered=notify_enabled, metadata={'source': 'sound-detection', 'sound_source': 'rtsp', 'camera_id': camera_id, 'camera_name': str((cam_settings or {}).get('name') or '').strip() or None, 'label': class_id, 'class_label': class_label, 'confidence': round(confidence, 3)})
    sound_detection = {'label': class_id, 'confidence': confidence, 'alert_triggered': True}
    should_record = normalize_bool_setting(fired_rule.get('record_on_detect'), True)
    recording_ids: list[int] = []
    if should_record and cam_settings:
        stream_url = build_stream_url(cam_settings)
        if stream_url:
            cam_rec_config = camera_event_recording_config(cam_settings)
            recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=cam_rec_config)
            rid = attach_event_recording(event_id, now_iso, 'rtsp', [sound_detection], camera_id=camera_id, recording_config=cam_rec_config)
            if rid is not None:
                recording_ids.append(rid)
                logger.debug('Sound event %s linked to recording %s (camera %s)', event_id, rid, camera_id)
    message = f'{class_label} detected ({confidence:.0%} confidence)'
    if notify_enabled and _rule_notify_active_now(fired_rule):
        database.add_alert(created_at=now_iso, rule_name=rule_name, event_id=event_id, label=class_id, confidence=confidence, message=message, recording_id=recording_ids[0] if recording_ids else None)
    alert_payload = {'rule_name': rule_name, 'label': class_id, 'confidence': confidence, 'message': message}
    notify_rule = {'name': rule_name, 'email_enabled': email_enabled, 'push_enabled': push_enabled, 'email_recipients': email_recipients, 'notify_start': str(fired_rule.get('notify_start') or '').strip() or None, 'notify_end': str(fired_rule.get('notify_end') or '').strip() or None}
    notify_thread = threading.Thread(target=_deliver_sound_alert_notifications, args=([alert_payload], event_id, notify_rule), name=f'sound-alert-notify-{event_id}', daemon=True)
    with _notification_threads_lock:
        _notification_threads[:] = [t for t in _notification_threads if t.is_alive()]
        _notification_threads.append(notify_thread)
    notify_thread.start()

def _make_sound_detect_callback(camera_id: str):

    def _callback(class_id: str, rule_name: str, confidence: float, meta: dict[str, Any]) -> None:
        _on_sound_detected(camera_id, class_id, rule_name, confidence, meta)
    return _callback

def _deliver_sound_alert_notifications(triggered: list[dict[str, Any]], event_id: int, rule: dict[str, Any]) -> None:
    if rule.get('email_enabled'):
        try:
            deliver_email_alerts(triggered, event_id, rules=[rule])
        except Exception as exc:
            logger.warning('Sound alert email delivery failed for event %s: %s', event_id, exc)
    if rule.get('push_enabled'):
        try:
            deliver_push_notifications(triggered, event_id, rules=[rule])
        except Exception as exc:
            logger.warning('Sound alert push delivery failed for event %s: %s', event_id, exc)

def apply_sound_settings() -> None:
    """Start one SoundDetector per RTSP camera that has sound detection enabled."""
    global _sound_detectors
    with _sound_detectors_lock:
        for det in list(_sound_detectors.values()):
            det.stop()
        _sound_detectors.clear()
    for cam in list(cameras_config):
        cam_id = str(cam.get('id') or '')
        stream_url = build_stream_url(cam)
        if not cam_id or not stream_url:
            continue
        sound_cfg = cam.get('detection', {}).get('sound', {})
        if not normalize_bool_setting(sound_cfg.get('enabled'), False):
            with _sound_statuses_lock:
                _sound_statuses[cam_id] = {'state': 'disabled', 'last_detected_at': None, 'last_confidence': 0.0, 'backend': None}
            continue
        enabled_rules = [r for r in sound_cfg.get('rules') or [] if r.get('enabled')]
        if not enabled_rules:
            with _sound_statuses_lock:
                _sound_statuses[cam_id] = {'state': 'disabled', 'last_detected_at': None, 'last_confidence': 0.0, 'backend': None}
            continue
        recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=cam_id, recording_config=camera_event_recording_config(cam))
        det = SoundDetector(on_detect=_make_sound_detect_callback(cam_id), rules=enabled_rules, source='ingest', sample_duration_seconds=1.0, audio_segment_provider=lambda after, _cid=cam_id: recording_service.audio_segments_after(_cid, after))
        det.start()
        with _sound_detectors_lock:
            _sound_detectors[cam_id] = det
        with _sound_statuses_lock:
            _sound_statuses[cam_id] = {'state': 'listening', 'last_detected_at': None, 'last_confidence': 0.0, 'backend': det.backend}
        logger.info('Sound monitor started for camera %s (rules=%s)', cam_id, [r.get('class') for r in enabled_rules])

def stop_sound_monitor() -> None:
    global _sound_detectors
    with _sound_detectors_lock:
        for det in list(_sound_detectors.values()):
            det.stop()
        _sound_detectors.clear()
    with _sound_statuses_lock:
        for cam_id in list(_sound_statuses.keys()):
            _sound_statuses[cam_id]['state'] = 'stopped'

def queue_live_stream_alerts(image_bytes: bytes, frame: dict[str, Any], settings: dict[str, Any]) -> None:
    camera_id = str(settings.get('id') or 'camera')
    stream_url = build_stream_url(settings)
    if stream_url:
        recording_service.prime_rtsp_prebuffer(stream_url=stream_url, camera_id=camera_id, recording_config=camera_event_recording_config(settings))
    live_cfg = effective_live_config()
    if normalize_bool_setting(live_cfg.get('background_detection_enabled'), True):
        return
    detection_interval_seconds = float(live_cfg.get('detection_interval_seconds', 0.25))
    now = time.time()
    with live_detection_worker_lock:
        if camera_id in active_live_detection_cameras:
            return
        if now - live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
            return
        live_detection_last_checked[camera_id] = now
        active_live_detection_cameras.add(camera_id)

    def detect() -> None:
        try:
            process_live_stream_alerts(image_bytes, frame, settings, enforce_interval=False)
        except Exception as exc:
            logger.warning('Live detection failed for camera %s: %s', camera_id, exc)
            update_live_detection_status(camera_id, state='error', reason=str(exc), detections=[])
        finally:
            with live_detection_worker_lock:
                active_live_detection_cameras.discard(camera_id)
    threading.Thread(target=detect, name=f'live-detection-{camera_id}', daemon=True).start()

def _encode_frame_jpeg(image: Any) -> bytes:
    """Encode a numpy BGR frame to JPEG bytes for snapshot storage."""
    import cv2
    _, buffer = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buffer.tobytes()

def process_live_stream_alerts(image: Any, frame: dict[str, Any], settings: dict[str, Any], *, enforce_interval: bool=True) -> int | None:
    camera_id = str(settings.get('id') or 'camera')
    live_settings = effective_live_config()
    detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.25))
    if not hasattr(detector, 'detect_image'):
        update_live_detection_status(camera_id, state='skipped', reason='Live stream alerts require ONNX AI mode.', detections=[])
        return None
    if enforce_interval:
        now = time.time()
        with live_detection_worker_lock:
            if now - live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
                return None
            live_detection_last_checked[camera_id] = now
    ai_state = ai_status_payload()
    if not ai_state['detector_loaded']:
        update_live_detection_status(camera_id, state='skipped', reason=ai_state['last_detector_error'] or 'ONNX detector is not loaded.', ai=ai_state, detections=[])
        return None
    frame_is_numpy = hasattr(image, 'shape') and hasattr(image, 'dtype')
    now = time.time()
    try:
        frame_capture_ts = float(frame.get('timestamp') or 0.0)
    except (TypeError, ValueError):
        frame_capture_ts = 0.0
    if not now - 300 <= frame_capture_ts <= now + 1:
        frame_capture_ts = now
    _pixel_threshold = float(live_settings.get('motion_pixel_threshold', _MOTION_PIXEL_THRESHOLD))
    _gate_fraction = float(live_settings.get('motion_gate_fraction', _MOTION_GATE_FRACTION))
    _scale_fraction = float(live_settings.get('motion_scale_fraction', _MOTION_SCALE_FRACTION))
    _background_alpha = float(live_settings.get('motion_background_alpha', _MOTION_BACKGROUND_ALPHA))
    _cam_motion = settings.get('motion') or {}
    if _cam_motion.get('pixel_threshold') is not None:
        try:
            _pixel_threshold = float(_cam_motion['pixel_threshold'])
        except (TypeError, ValueError):
            pass
    if _cam_motion.get('gate_fraction') is not None:
        try:
            _gate_fraction = float(_cam_motion['gate_fraction'])
        except (TypeError, ValueError):
            pass
    if _cam_motion.get('scale_fraction') is not None:
        try:
            _scale_fraction = float(_cam_motion['scale_fraction'])
        except (TypeError, ValueError):
            pass
    if _cam_motion.get('background_alpha') is not None:
        try:
            _background_alpha = float(_cam_motion['background_alpha'])
        except (TypeError, ValueError):
            pass
    periodic_scan_interval = float(live_settings.get('periodic_scan_interval_seconds', 0))
    force_scan = False
    if periodic_scan_interval > 0 and now - _periodic_scan_last_ts.get(camera_id, 0) >= periodic_scan_interval:
        force_scan = True
        _periodic_scan_last_ts[camera_id] = now
    frame_has_motion, frame_motion_confidence, diff_mask = detect_frame_motion(camera_id, image, pixel_threshold=_pixel_threshold, gate_fraction=_gate_fraction, scale_fraction=_scale_fraction, background_alpha=_background_alpha)
    if not frame_has_motion and (not force_scan):
        update_live_detection_status(camera_id, state='checked', reason='No motion detected; ONNX inference skipped.', detected_labels=[], matched_labels=[], detections=[])
        return None
    if not frame_has_motion:
        frame_motion_confidence = 0.0
        diff_mask = None
    min_conf = compute_minimum_rule_confidence()
    try:
        if frame_is_numpy and hasattr(detector, 'detect_frame'):
            detections = detector.detect_frame(image, confidence=min_conf)
        else:
            detections = detector.detect_image(image, confidence=min_conf)
    except (DetectorUnavailableError, ValueError) as exc:
        logger.warning('Live detection skipped for camera %s: %s', camera_id, exc)
        update_live_detection_status(camera_id, state='error', reason=str(exc), ai=ai_state, detections=[])
        return None
    detections = normalize_detection_boxes_for_frame(detections, frame)
    raw_labels = [str(detection.get('label')) for detection in detections if detection.get('label')]
    motion_detections = zone_motion_detections(settings, frame_motion_confidence, diff_mask=diff_mask, gate_fraction=_gate_fraction, scale_fraction=_scale_fraction)
    object_detections = filter_detections_for_camera(detections, settings)
    zone_rules = zone_object_alert_rules(settings)
    has_object_zone_rules = any((zone.get('enabled', True) and zone.get('monitor_objects', True) and any((rule.get('enabled', True) and str(rule.get('label') or '').strip() for rule in zone.get('object_rules') or [])) for zone in (settings.get('detection') or {}).get('zones', [])))
    object_alert_detections = zone_alert_detections(settings, object_detections) if has_object_zone_rules else list(object_detections)
    record_only_detections = [d for d in object_detections if zone_record_on_detect(d, settings) and (not zone_object_rule_matches(settings, d, action='alert'))] if has_object_zone_rules else []
    strongest_motion = max(motion_detections, key=lambda d: float(d.get('confidence', 0))) if motion_detections else None
    record_live_detection_history(camera_id, list(object_alert_detections) + record_only_detections + ([{**strongest_motion, 'label': 'motion', 'motion_event': True}] if strongest_motion is not None else []), sample_ts=frame_capture_ts, live_config=live_settings)
    alert_detections = list(object_alert_detections) + record_only_detections
    for _mot in motion_detections:
        alert_detections.append({**_mot, 'label': 'motion', 'motion_event': True})
    if not alert_detections:
        update_live_detection_status(camera_id, state='checked', reason='No detections matched this camera and its monitoring areas.', detected_labels=raw_labels, matched_labels=[], detections=list(detections))
        return None
    triggered = alerts.process(alert_detections, rules=zone_rules)
    triggered_rule_names = {str(alert.get('rule_name') or '') for alert in triggered}
    triggered_labels = {str(alert.get('label') or '').lower() for alert in triggered}
    _confident_object_detections: list[dict[str, Any]] = []
    if has_object_zone_rules:
        for _det in object_detections:
            _zone_name = zone_name_for_detection(settings, _det)
            if _zone_name or zone_record_on_detect(_det, settings):
                _confident_object_detections.append({**_det, 'zone_name': _zone_name or None})
    else:
        _confident_object_detections = list(object_detections)
    recording_detections = [{**detection, 'alert_matched': bool(zone_detection_alert_rule_names(settings, detection) & triggered_rule_names) if has_object_zone_rules else str(detection.get('label') or '').lower() in triggered_labels, 'alert_triggered': zone_record_on_detect(detection, settings)} for detection in _confident_object_detections]
    if motion_detections:
        _motion_record = zone_motion_record_on_detect(settings)
        recording_detections.append({**strongest_motion, 'label': 'motion', 'motion_event': True, 'alert_matched': 'motion' in triggered_labels, 'alert_triggered': 'motion' in triggered_labels or _motion_record or detection_has_matching_record_rule({**strongest_motion, 'label': 'motion'}, zone_rules)})
    matched_labels = [str(detection.get('label')) for detection in alert_detections if detection.get('label')]
    camera_recording_config = camera_event_recording_config(settings)
    should_record_event, _trigger_type, _trigger_label = recording_service.should_record(recording_detections, camera_recording_config)
    debounced_labels = detection_label_set([detection for detection in recording_detections if detection.get('alert_triggered')])
    if not debounced_labels:
        debounced_labels = detection_label_set(recording_detections)
    global_debounce = max(0.0, float(live_settings.get('event_debounce_seconds', 10.0)))
    label_cooldowns: dict[str, float] = {}
    for _zone in (settings.get('detection') or {}).get('zones', []):
        for _rule in _zone.get('object_rules') or []:
            if not _rule.get('enabled', True):
                continue
            _lbl = str(_rule.get('label') or '').strip().lower()
            if not _lbl:
                continue
            try:
                _cd = max(0.0, float(_rule.get('cooldown_seconds', 60)))
            except (TypeError, ValueError):
                _cd = 60.0
            if _lbl not in label_cooldowns or _cd > label_cooldowns[_lbl]:
                label_cooldowns[_lbl] = _cd
    _matching = [label_cooldowns[_lbl] for _lbl in debounced_labels if _lbl in label_cooldowns]
    debounce_seconds = max(_matching) if _matching else global_debounce
    frame_capture_time = datetime.fromtimestamp(frame_capture_ts, tz=timezone.utc).isoformat()
    if should_record_event and live_event_is_debounced(camera_id, debounced_labels, debounce_seconds):
        extended_recording_id = extend_active_rtsp_recording(camera_id=camera_id, event_time=frame_capture_time, recording_config=camera_recording_config, detections=recording_detections)
        remember_live_event(camera_id, debounced_labels, merge=True)
        update_live_detection_status(camera_id, state='checked', reason=f'Ongoing detection extended active recording and suppressed duplicate event for {debounce_seconds:.1f}s debounce window.' if extended_recording_id is not None else f'Ongoing detection suppressed for {debounce_seconds:.1f}s debounce window.', detected_labels=raw_labels, matched_labels=matched_labels, detections=recording_detections, recording_id=extended_recording_id)
        return None
    event_time = frame_capture_time
    if frame_is_numpy:
        image_bytes = _encode_frame_jpeg(image)
    else:
        image_bytes = image
    snapshot_path = storage.save_image_snapshot(image_bytes, f'{camera_id}.jpg')
    event_id = database.add_event(created_at=event_time, source='rtsp', snapshot_path=snapshot_path, detections=recording_detections, alert_triggered=bool(triggered), metadata={'camera_id': settings.get('id'), 'camera_name': settings.get('name'), 'ai_backend': ai_state['configured_backend'], 'detector_backend': ai_state['active_backend'], 'source': 'live-stream'})
    recording_id = attach_event_recording(event_id, event_time, 'rtsp', recording_detections, camera_id=camera_id, recording_config=camera_recording_config)
    if recording_id is not None:
        remember_live_event(camera_id, debounced_labels)
    _rule_by_name = {str(r.get('name') or ''): r for r in zone_rules or []}
    for alert in triggered:
        _rule = _rule_by_name.get(str(alert.get('rule_name') or ''), {})
        if not _rule_notify_active_now(_rule):
            continue
        database.add_alert(created_at=datetime.now(timezone.utc).isoformat(), rule_name=alert['rule_name'], event_id=event_id, label=alert['label'], confidence=alert['confidence'], message=alert['message'], recording_id=recording_id)
    if triggered:
        notify_thread = threading.Thread(target=_deliver_alert_notifications, args=(triggered, event_id, zone_rules), name=f'alert-notify-{event_id}', daemon=True)
        with _notification_threads_lock:
            _notification_threads[:] = [thread for thread in _notification_threads if thread.is_alive()]
            _notification_threads.append(notify_thread)
        notify_thread.start()
    email_rules = [rule for rule in zone_rules if rule.get('enabled', True) and rule.get('email_enabled') and _rule_notify_active_now(rule) and (str(rule.get('name') or '') in {str(alert.get('rule_name') or '') for alert in triggered})]
    email_recipients = sorted({recipient for rule in email_rules for recipient in rule.get('email_recipients', [])})
    update_live_detection_status(camera_id, state='alerted' if triggered else 'checked', reason='Alert matched.' if triggered else 'Detections found. No new alert event was created because no alert rule matched, or a matching rule is still in cooldown.', detected_labels=raw_labels, matched_labels=matched_labels, detections=recording_detections, triggered_alerts=triggered, event_id=event_id, recording_id=recording_id, recording_state='linked' if recording_id is not None else 'skipped', recording_reason='Recording linked.' if recording_id is not None else recording_skip_reason(recording_detections, camera_event_recording_config(settings)), email_enabled_rules=len(email_rules), email_recipients=email_recipients, email_attempted=bool(triggered and email_recipients and effective_email_alert_settings().get('enabled')))
    return event_id

def create_camera(settings: dict[str, Any]):
    width = int(settings.get('width', 1280))
    height = int(settings.get('height', 720))
    fps = int(settings.get('fps', 15))
    stale = settings.get('stale_frame_grabs')
    return OpenCvStreamCamera(build_stream_url(settings), width=width, height=height, fps=fps, stale_frame_grabs=stale)

def create_camera_instances(settings_list: list[dict[str, Any]]) -> dict[str, Any]:
    return {str(settings['id']): create_camera(settings) for settings in settings_list}
cameras_config = effective_cameras_config()
camera_config = cameras_config[0] if cameras_config else {}
camera_instances = create_camera_instances(cameras_config)
camera = camera_instances[camera_config['id']] if camera_config else None

def config_file_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)

def active_ai_config_source() -> str:
    if database.has_setting('ai'):
        return 'database'
    if config_file_path().exists():
        return 'config.yaml'
    return 'default'

def onnx_runtime_installed() -> bool:
    return importlib.util.find_spec('onnxruntime') is not None

def model_exists(ai_settings: dict[str, Any]) -> bool:
    model_path = str(ai_settings.get('model_path') or '')
    return bool(model_path) and Path(model_path).exists()

def detector_loaded_for(settings: dict[str, Any]) -> bool:
    configured_backend = str(settings.get('backend', 'onnx')).lower()
    active_backend = getattr(detector, 'backend', 'unknown')
    if configured_backend == 'onnx':
        return active_backend == 'onnx' and bool(getattr(detector, 'available', False))
    return False

def ai_status_payload(ai_settings: dict[str, Any] | None=None) -> dict[str, Any]:
    settings = ai_settings or effective_ai_config()
    active_backend = getattr(detector, 'backend', 'unknown')
    configured_backend = str(settings.get('backend', 'onnx')).lower()
    detector_loaded = detector_loaded_for(settings)
    model_loaded = bool(configured_backend == 'onnx' and active_backend == 'onnx' and getattr(detector, 'available', False))
    runtime_installed = onnx_runtime_installed()
    exists = model_exists(settings)
    detector_reason = getattr(detector, 'unavailable_reason', None)
    error = last_detector_error or detector_reason
    if configured_backend == 'onnx' and (not exists):
        mode = 'MODEL MISSING'
        error = error or f"ONNX model not found: {settings.get('model_path')}"
    elif configured_backend == 'onnx' and (not model_loaded):
        mode = 'MODEL FAILED'
    elif configured_backend == 'onnx':
        mode = 'ONNX ACTIVE'
        error = detector_reason
    else:
        mode = 'MODEL FAILED'
    inference_available = detector_loaded
    model_path_str = str(settings.get('model_path') or '')
    model_filename = Path(model_path_str).name if model_path_str else ''
    model_label = next((info['label'] for info in YOLO_MODELS.values() if info['onnx'] == model_filename), None)
    return {'active_backend': active_backend, 'configured_backend': configured_backend, 'mode': mode, 'model_loaded': model_loaded, 'detector_loaded': detector_loaded, 'model_path': model_path_str, 'model_name': model_label, 'labels_path': str(settings.get('labels_path') or ''), 'model_exists': exists, 'onnx_runtime_installed': runtime_installed, 'inference_available': inference_available, 'error': error, 'last_detector_error': error, 'active_config_source': active_ai_config_source()}

def log_detector_initialization(context: str='startup') -> None:
    ai_status = ai_status_payload()
    active_providers = getattr(detector, 'active_providers', None)
    providers_str = ','.join(active_providers) if active_providers else '<none>'
    logger.info('AI detector %s: active_backend=%s configured_backend=%s model_loaded=%s inference_available=%s providers=%s model_path=%s labels_path=%s error=%s', context, ai_status['active_backend'], ai_status['configured_backend'], ai_status['model_loaded'], ai_status['inference_available'], providers_str, ai_status['model_path'] or '<none>', ai_status['labels_path'] or '<none>', ai_status['error'] or '<none>')
PUBLIC_PREFIXES = ('/static/',)
PUBLIC_PATHS = {'/favicon.ico', '/login', '/setup'}
ADMIN_PATHS = {'/onnx', '/yamnet-tflite', '/ai', '/cameras', '/settings', '/users', '/zones', '/sounds', '/audit', '/camera-log'}
MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}

@app.middleware('http')
async def authentication_middleware(request: Request, call_next):
    if not effective_auth_config().get('enabled', True):
        return await call_next(request)
    path = request.url.path
    if path in PUBLIC_PATHS or any((path.startswith(prefix) for prefix in PUBLIC_PREFIXES)):
        return await call_next(request)
    has_users = auth.users_exist()
    if not has_users:
        if path.startswith('/api/'):
            return JSONResponse({'detail': 'Initial administrator setup is required.'}, status_code=403)
        return RedirectResponse('/setup', status_code=303)
    session = auth.get_session(request.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        if path.startswith('/api/'):
            return JSONResponse({'detail': 'Authentication required'}, status_code=401)
        return RedirectResponse('/login', status_code=303)
    request.state.session = session
    request.state.user = session['user']
    admin_required = path in ADMIN_PATHS or path.startswith('/api/users') or path.startswith('/api/settings/ai') or path.startswith('/api/settings/system') or path.startswith('/api/update/') or (path.startswith('/api/cameras') and request.method in MUTATING_METHODS) or (path.startswith('/api/settings/alert-email') and request.method in MUTATING_METHODS) or (path.startswith('/api/settings/alert-push') and request.method in MUTATING_METHODS) or (path.startswith('/api/settings/camera-offline') and request.method in MUTATING_METHODS) or ((path.startswith('/api/events') or path.startswith('/api/alerts')) and 'dismiss' in path and (request.method in MUTATING_METHODS))
    if admin_required and session['user']['role'] != 'admin':
        return JSONResponse({'detail': 'Admin access required'}, status_code=403)
    if (path.startswith('/api/') or path == '/logout') and request.method in MUTATING_METHODS:
        csrf_header = request.headers.get(CSRF_HEADER)
        if not csrf_header or csrf_header != session['csrf_token']:
            return JSONResponse({'detail': 'CSRF token missing or invalid'}, status_code=403)
    return await call_next(request)

@app.middleware('http')
async def app_navigation_middleware(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get('content-type', '')
    if request.url.path in PUBLIC_PATHS or not content_type.startswith('text/html'):
        return response
    body = b''
    async for chunk in response.body_iterator:
        body += chunk
    marker = b'</body>'
    script = b'<script src="/static/nav.js"></script>'
    if marker in body and script not in body:
        body = body.replace(marker, script + marker)
    headers = dict(response.headers)
    headers.pop('content-length', None)
    return Response(content=body, status_code=response.status_code, headers=headers, media_type='text/html')

def set_session_cookie(response: Response, request: Request, token: str, expires_at: str) -> None:
    session_hours = float(effective_auth_config().get('session_timeout_hours', 12))
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, secure=request.url.scheme == 'https', samesite='lax', expires=expires_at, max_age=int(session_hours * 3600))

def set_csrf_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=request.url.scheme == 'https', samesite='lax', max_age=3600)

def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE)

async def form_data(request: Request) -> dict[str, str]:
    body = (await request.body()).decode('utf-8')
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}

def auth_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />\n<title>{escape(title)} · Daygle AI Camera</title><link rel="stylesheet" href="/static/styles.css" /></head>\n<body><main class="auth-shell"><section class="card auth-card"><p class="eyebrow">Daygle AI Camera</p>{body}</section></main></body></html>')

def csrf_token_response(request: Request, title: str, body_template: str, *, status_code: int=200) -> HTMLResponse:
    token = secrets.token_urlsafe(32)
    response = auth_page(title, body_template.format(csrf=escape(token)))
    response.status_code = status_code
    set_csrf_cookie(response, token, request)
    return response

def require_user(request: Request) -> dict[str, Any]:
    return request.state.user

def require_session(request: Request) -> dict[str, Any]:
    return request.state.session

def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='Admin access required')
    return user
_LOOPBACK = {'127.0.0.1', '::1', 'localhost'}

def _request_ip(request: Request) -> str:
    direct = request.client.host if request.client else ''
    if direct in _LOOPBACK:
        forwarded = request.headers.get('x-forwarded-for')
        if forwarded:
            return forwarded.split(',')[0].strip()
    return direct or 'unknown'

def write_audit_log(request: Request, action: str, resource: str, resource_id: Any=None, details: dict[str, Any] | None=None, status: str='success') -> None:
    user: dict[str, Any] | None = getattr(request.state, 'user', None)
    user_id: int | None = int(user['id']) if user else None
    username: str = str(user['username']) if user else 'anonymous'
    try:
        database.add_audit_log(created_at=utc_now(), user_id=user_id, username=username, action=action, resource=resource, resource_id=str(resource_id) if resource_id is not None else None, details=details, ip_address=_request_ip(request), status=status)
    except Exception as exc:
        logger.warning('Failed to write audit log: %s', exc)

def log_camera_diagnostic(camera_id: str | None, event_type: str, message: str='', *, severity: str='info', details: dict[str, Any] | None=None, camera_name: str | None=None) -> None:
    """Record a system-generated camera/recording diagnostic event.

    Best-effort and never raises into the calling path - diagnostics must not
    be able to break recording or detection. Kept separate from the audit log
    so operational noise doesn't dilute the security trail.
    """
    try:
        if camera_name is None and camera_id:
            cfg = next((c for c in cameras_config if str(c.get('id') or '') == str(camera_id)), None)
            if cfg:
                camera_name = str(cfg.get('name') or '').strip() or None
        database.add_camera_diagnostic(created_at=utc_now(), camera_id=str(camera_id) if camera_id else None, camera_name=camera_name, event_type=event_type, severity=severity, message=message, details=details)
    except Exception as exc:
        logger.debug('Failed to write camera diagnostic (%s/%s): %s', camera_id, event_type, exc)
recording_service.diagnostic_callback = log_camera_diagnostic

def _parse_chunk_start_time(file_path: Path) -> datetime | None:
    stem = file_path.stem
    parts = stem.rsplit('_', 1)
    if len(parts) != 2:
        return None
    try:
        return datetime.strptime(parts[1], '%Y%m%dT%H%M%S').astimezone(timezone.utc)
    except ValueError:
        return None

def _make_continuous_chunk_callback(camera_id: str) -> Any:

    def on_chunk_complete(camera_key: str, file_path: Path) -> None:
        try:
            started_at_dt = _parse_chunk_start_time(file_path)
            stat = file_path.stat()
            ended_at_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if started_at_dt is None:
                started_at_dt = ended_at_dt - timedelta(seconds=effective_recording_config().get('chunk_duration_seconds', 3600))
            duration_seconds = max(1.0, (ended_at_dt - started_at_dt).total_seconds())
            chunk_track = build_track_from_live_history(camera_id, started_at_dt.timestamp(), ended_at_dt.timestamp())
            chunk_labels: list[str] = []
            chunk_confidences: dict[str, float] = {}
            if chunk_track:
                _seen: set[str] = set()
                for _sample in chunk_track:
                    for _det in _sample.get('detections') or []:
                        _lbl = str(_det.get('label') or '').strip().lower()
                        if not _lbl:
                            continue
                        if _lbl not in _seen:
                            _seen.add(_lbl)
                            chunk_labels.append(_lbl)
                        try:
                            _conf = float(_det.get('confidence'))
                        except (TypeError, ValueError):
                            _conf = None
                        if _conf is not None and (_lbl not in chunk_confidences or _conf > chunk_confidences[_lbl]):
                            chunk_confidences[_lbl] = _conf
            recording_id = database.add_recording(event_id=None, camera_id=camera_id, started_at=started_at_dt.isoformat(), ended_at=ended_at_dt.isoformat(), duration_seconds=duration_seconds, file_path=str(file_path), thumbnail_path=None, source='rtsp', created_at=utc_now(), trigger_type='continuous', trigger_label=None, labels=chunk_labels or None, label_confidences=chunk_confidences or None)
            write_live_history_detection_track(recording_id, file_path, camera_id, started_at_dt.timestamp(), ended_at_dt.timestamp())
            purge_recordings_by_policy()
        except Exception as exc:
            logger.warning('Failed to register continuous chunk %s for camera %s: %s', file_path.name, camera_id, exc)
    return on_chunk_complete

def attach_event_recording(event_id: int, event_time: str, source: str, detections: list[dict[str, Any]], camera_id: str | None=None, recording_config: dict[str, Any] | None=None) -> int | None:
    stream_url = ''
    if source == 'rtsp' and camera_id:
        stream_url = build_stream_url(get_camera_config(camera_id))
        extended_recording_id = extend_active_rtsp_recording(camera_id=camera_id, event_time=event_time, recording_config=recording_config, detections=detections)
        if extended_recording_id is not None:
            return extended_recording_id
    metadata = recording_service.event_recording_metadata(event_id, event_time, source, detections, write_clip=not stream_url, recording_config=recording_config)
    if metadata is None:
        return None
    if camera_id:
        metadata['camera_id'] = camera_id
    detection_labels = detection_label_strings(detections)
    recording_id = database.add_recording(created_at=utc_now(), labels=detection_labels or None, label_confidences=detection_label_confidences(detections) or None, **metadata)
    if stream_url:
        start_rtsp_recording_capture(stream_url, metadata, event_id, detections, recording_id=recording_id, camera_id=camera_id, event_time=event_time, recording_config=recording_config)
    else:
        window = _recording_capture_window(metadata)
        if window:
            write_live_history_detection_track(recording_id, Path(str(metadata.get('file_path') or '')), camera_id, window[0], window[1])
    purge_recordings_by_policy()
    return recording_id

def start_rtsp_recording_capture(stream_url: str, metadata: dict[str, Any], event_id: int, detections: list[dict[str, Any]], *, recording_id: int, camera_id: str | None=None, event_time: str | None=None, recording_config: dict[str, Any] | None=None) -> None:
    file_path = Path(str(metadata.get('file_path') or ''))
    duration_seconds = float(metadata.get('duration_seconds') or 1)
    trigger_type = str(metadata.get('trigger_type') or 'motion')
    trigger_label = metadata.get('trigger_label')
    pre_seconds = max(0, int((recording_config or {}).get('pre_event_seconds', 0)))
    post_seconds = max(0, int((recording_config or {}).get('post_event_seconds', 0)))
    try:
        triggered_at = datetime.fromisoformat(str(event_time or utc_now()))
    except ValueError:
        triggered_at = datetime.now(timezone.utc)
    if triggered_at.tzinfo is None:
        triggered_at = triggered_at.replace(tzinfo=timezone.utc)
    start_capture_ts = triggered_at.timestamp() - pre_seconds
    initial_deadline_ts = triggered_at.timestamp() + post_seconds
    max_clip_seconds = max(1, int((recording_config or effective_recording_config()).get('max_clip_seconds', 60)))
    max_deadline_ts = start_capture_ts + max(duration_seconds, float(max_clip_seconds))
    if camera_id:
        with active_rtsp_recordings_lock:
            active_rtsp_recordings[camera_id] = {'recording_id': recording_id, 'start_capture_ts': start_capture_ts, 'capture_deadline_ts': min(max_deadline_ts, initial_deadline_ts), 'max_capture_deadline_ts': max_deadline_ts}

    def write_generated_fallback() -> None:
        recording_service.write_event_clip(file_path, event_id, detections, duration_seconds, trigger_type, str(trigger_label) if trigger_label else None)

    def capture() -> None:
        try:
            final_deadline_ts = min(max_deadline_ts, initial_deadline_ts)
            if camera_id:
                while True:
                    with active_rtsp_recordings_lock:
                        session = active_rtsp_recordings.get(camera_id)
                        if not session or int(session.get('recording_id', -1)) != int(recording_id):
                            break
                        final_deadline_ts = float(session.get('capture_deadline_ts') or final_deadline_ts)
                    remaining = final_deadline_ts - time.time()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.5, max(0.05, remaining)))
            final_deadline_ts = min(final_deadline_ts, max_deadline_ts)
            actual_end_ts = min(max(time.time(), final_deadline_ts), max_deadline_ts)
            final_duration_seconds = max(1.0, actual_end_ts - start_capture_ts)
            dynamic_post_seconds = max(0, int(round(actual_end_ts - triggered_at.timestamp())))
            if camera_id and pre_seconds > 0:
                content_start_ts, content_seconds = recording_service.write_rtsp_clip_with_prebuffer(stream_url=stream_url, camera_id=camera_id, file_path=file_path, triggered_at=triggered_at, pre_seconds=pre_seconds, post_seconds=dynamic_post_seconds, max_duration_seconds=final_duration_seconds, buffer_seconds=recording_service.prebuffer_window_seconds(recording_config))
            else:
                content_start_ts = time.time()
                recording_service.write_rtsp_clip(stream_url, file_path, final_duration_seconds)
                content_seconds = final_duration_seconds
            database.update_recording_timing(recording_id, started_at=datetime.fromtimestamp(content_start_ts, tz=timezone.utc).isoformat(), ended_at=datetime.fromtimestamp(content_start_ts + content_seconds, tz=timezone.utc).isoformat(), duration_seconds=content_seconds)
            write_live_history_detection_track(recording_id, file_path, camera_id, content_start_ts, content_start_ts + content_seconds)
        except Exception as exc:
            logger.warning('RTSP recording capture failed for event %s, writing generated fallback: %s', event_id, exc)
            log_camera_diagnostic(camera_id, 'capture_failed', f'RTSP recording capture failed; wrote a generated placeholder clip instead: {exc}', severity='error', details={'event_id': event_id, 'recording_id': recording_id})
            write_generated_fallback()
            write_live_history_detection_track(recording_id, file_path, camera_id, start_capture_ts, start_capture_ts + duration_seconds)
        finally:
            if camera_id:
                with active_rtsp_recordings_lock:
                    session = active_rtsp_recordings.get(camera_id)
                    if session and int(session.get('recording_id', -1)) == int(recording_id):
                        active_rtsp_recordings.pop(camera_id, None)
    threading.Thread(target=capture, name=f'rtsp-recording-{event_id}', daemon=True).start()

def recording_skip_reason(detections: list[dict[str, Any]], recording_config: dict[str, Any] | None=None) -> str:
    should_record, trigger_type, trigger_label = recording_service.should_record(detections, recording_config)
    if should_record:
        return f"Recording policy matched {trigger_type}{(f' {trigger_label}' if trigger_label else '')}, but no recording was linked."
    labels = ', '.join((str(detection.get('label')) for detection in detections if detection.get('label'))) or 'none'
    return f'Recording is waiting for an enabled alert rule to trigger for this camera. Detected labels: {labels}.'
_notification_threads_lock = threading.Lock()
_notification_threads: list[threading.Thread] = []

def wait_for_pending_alert_notifications(timeout: float=10.0) -> None:
    """Block until in-flight alert email/push deliveries finish (used by tests)."""
    deadline = time.time() + max(0.0, timeout)
    with _notification_threads_lock:
        pending = [thread for thread in _notification_threads if thread.is_alive()]
    for thread in pending:
        thread.join(timeout=max(0.0, deadline - time.time()))

def _deliver_alert_notifications(triggered: list[dict[str, Any]], event_id: int, rules: list[dict[str, Any]] | None) -> None:
    try:
        deliver_email_alerts(triggered, event_id, rules=rules)
    except Exception as exc:
        logger.warning('Email alert delivery failed for event %s: %s', event_id, exc)
    try:
        deliver_push_notifications(triggered, event_id, rules=rules)
    except Exception as exc:
        logger.warning('Push notification delivery failed for event %s: %s', event_id, exc)

def deliver_email_alerts(triggered: list[dict[str, Any]], event_id: int, rules: list[dict[str, Any]] | None=None) -> None:
    if not triggered:
        return
    event = database.get_event(event_id) or {}
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    camera_name = str(metadata.get('camera_name') or '').strip() or None
    camera_id = str(metadata.get('camera_id') or '').strip() or None
    created_at_raw = str(event.get('created_at') or '').strip()
    detected_at = _format_alert_datetime(created_at_raw) if created_at_raw else None
    rules_by_name = {str(rule.get('name')): rule for rule in rules or []}
    any_email_enabled = any(((rule := rules_by_name.get(str(alert.get('rule_name')), {})).get('email_enabled') and _rule_notify_active_now(rule) for alert in triggered))
    snapshot_bytes: bytes | None = None
    snapshot_path = str(event.get('snapshot_path') or '')
    if any_email_enabled and snapshot_path:
        try:
            snap_path = Path(snapshot_path)
            if snap_path.exists():
                raw_bytes = snap_path.read_bytes()
                db_detections = event.get('detections') or []
                _email_min_conf = compute_minimum_rule_confidence()
                overlay_detections = [{'label': d.get('label'), 'confidence': d.get('confidence'), 'box': {'x': d.get('x', 0), 'y': d.get('y', 0), 'width': d.get('width', 0), 'height': d.get('height', 0)}} for d in db_detections if float(d.get('confidence') or 0) >= _email_min_conf]
                snapshot_bytes = render_live_snapshot_jpeg_overlay(raw_bytes, overlay_detections)
        except Exception as exc:
            logger.debug('Failed to annotate snapshot for email alert event %s: %s', event_id, exc)
    mailer = EmailAlertService(effective_email_alert_settings())
    all_triggered_labels = sorted({str(alert.get('label') or '').strip() for alert in triggered if str(alert.get('label') or '').strip()})
    for alert in triggered:
        rule = rules_by_name.get(str(alert.get('rule_name')))
        if not rule or not rule.get('email_enabled'):
            continue
        if not _rule_notify_active_now(rule):
            logger.debug('Email skipped for event %s rule %r: outside email/push window %s-%s', event_id, alert.get('rule_name'), rule.get('notify_start'), rule.get('notify_end'))
            continue
        try:
            mailer.send_alert(alert, event_id=event_id, recipients=rule.get('email_recipients', []), camera_name=camera_name, camera_id=camera_id, snapshot_bytes=snapshot_bytes, triggered_labels=all_triggered_labels, detected_at=detected_at)
        except EmailAlertError as exc:
            logger.warning('Failed to send email alert for event %s rule %s: %s', event_id, alert.get('rule_name'), exc)

def deliver_push_notifications(triggered: list[dict[str, Any]], event_id: int, rules: list[dict[str, Any]] | None=None) -> None:
    if not triggered:
        return
    push_settings = effective_push_notification_settings()
    if not push_settings.get('enabled'):
        logger.debug('Push notifications disabled globally; skipping event %s', event_id)
        return
    event = database.get_event(event_id) or {}
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    camera_name = str(metadata.get('camera_name') or '').strip() or None
    camera_id = str(metadata.get('camera_id') or '').strip() or None
    created_at_raw = str(event.get('created_at') or '').strip()
    detected_at = _format_alert_datetime(created_at_raw) if created_at_raw else None
    rules_by_name = {str(rule.get('name')): rule for rule in rules or []}
    notifier = PushNotificationService(push_settings)
    all_triggered_labels = sorted({str(alert.get('label') or '').strip() for alert in triggered if str(alert.get('label') or '').strip()})
    for alert in triggered:
        rule_name = str(alert.get('rule_name') or '')
        rule = rules_by_name.get(rule_name)
        if not rule:
            logger.debug('Push skipped for event %s: no rule found for %r', event_id, rule_name)
            continue
        if not rule.get('push_enabled'):
            logger.debug('Push skipped for event %s rule %r: push_enabled is False', event_id, rule_name)
            continue
        if not _rule_notify_active_now(rule):
            logger.debug('Push skipped for event %s rule %r: outside email/push window %s-%s', event_id, rule_name, rule.get('notify_start'), rule.get('notify_end'))
            continue
        try:
            notifier.send_alert(alert, event_id=event_id, camera_name=camera_name, camera_id=camera_id, triggered_labels=all_triggered_labels, detected_at=detected_at)
            logger.info('Push notification sent for event %s rule %r', event_id, rule_name)
        except PushNotificationError as exc:
            logger.error('Failed to send push notification for event %s rule %r: %s', event_id, rule_name, exc)
GITHUB_REPO = 'daygle/daygle-ai-camera'
PYPI_ULTRALYTICS_URL = 'https://pypi.org/pypi/ultralytics/json'
_update_in_progress = False
_update_lock = threading.Lock()
_installed_models_lock = threading.Lock()

def _installed_models_path() -> Path:
    return BASE_DIR / 'models' / 'installed.json'

def _read_installed_models() -> dict[str, Any]:
    p = _installed_models_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception as exc:
            logger.warning('Failed to read settings file: %s', exc)
            return {}
    return {}

def _write_installed_models(data: dict[str, Any]) -> None:
    p = _installed_models_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding='utf-8')

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def _installed_package_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'

def _fetch_ultralytics_version() -> str:
    req = urllib.request.Request(PYPI_ULTRALYTICS_URL, headers={'User-Agent': 'daygle-ai-camera-updater/1.0'})
    with urllib.request.urlopen(req, timeout=10) as response:
        payload = json.loads(response.read())
    version = str(payload.get('info', {}).get('version') or '').strip()
    if not version:
        raise RuntimeError('PyPI ultralytics response did not include a version.')
    return version

def _parse_semver(v: str) -> tuple[int, ...]:
    try:
        return tuple((int(x) for x in v.split('.')))
    except ValueError:
        return (0,)

def _fetch_models_manifest() -> dict[str, Any]:
    """Return remote YOLO export versions from the upstream Ultralytics package.

    The app exports ONNX files from Ultralytics YOLO weights, so the remote
    version that matters for update checks is the latest Ultralytics release,
    not a Daygle-maintained model manifest version.

    The effective version is capped at the locally installed package version:
    if PyPI has a newer release but the local package hasn't been upgraded yet,
    re-exporting the model would produce the same file, so no update is shown.
    """
    remote_version = _fetch_ultralytics_version()
    local_version = _installed_package_version('ultralytics')
    if local_version != 'unknown' and _parse_semver(local_version) < _parse_semver(remote_version):
        effective_version = local_version
    else:
        effective_version = remote_version
    return {'updated_at': None, 'source': 'pypi:ultralytics', 'models': {model_id: {'version': effective_version} for model_id in YOLO_MODELS}}

def render_live_snapshot_svg(frame: dict[str, Any], detections: list[dict[str, Any]], *, overlay: bool, camera_name: str='Camera', zones: list[dict[str, Any]] | None=None) -> str:
    width = int(frame.get('width') or 1280)
    height = int(frame.get('height') or 720)
    frame_number = int(frame.get('frame_number') or 0)
    timestamp = datetime.fromtimestamp(float(frame.get('timestamp') or 0), timezone.utc).strftime('%H:%M:%S UTC')
    grid_spacing = 80
    grid_lines = []
    for x in range(0, width + grid_spacing, grid_spacing):
        grid_lines.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{height}" />')
    for y in range(0, height + grid_spacing, grid_spacing):
        grid_lines.append(f'<line x1="0" y1="{y}" x2="{width}" y2="{y}" />')
    zone_markup: list[str] = []
    if overlay:
        for zone in zones or []:
            if not zone.get('enabled', True):
                continue
            points = zone.get('points') or rectangle_zone_points(max(0.0, min(1.0, float(zone.get('x') or 0))), max(0.0, min(1.0, float(zone.get('y') or 0))), max(0.01, min(1.0, float(zone.get('width') or 0))), max(0.01, min(1.0, float(zone.get('height') or 0))))
            svg_points = []
            for point in points:
                if not isinstance(point, dict):
                    continue
                svg_points.append(f"{max(0, float(point.get('x') or 0) * width):.1f},{max(0, float(point.get('y') or 0) * height):.1f}")
            if len(svg_points) < 3:
                continue
            label_x = max(0, float(points[0].get('x') or 0) * width) + 12
            label_y = max(30, float(points[0].get('y') or 0) * height + 30)
            zone_name = escape(str(zone.get('name') or 'Monitoring area'))
            zone_markup.append(f'''<g class="monitor-zone"><polygon points="{' '.join(svg_points)}" /><text x="{label_x:.1f}" y="{label_y:.1f}">{zone_name}</text></g>''')
    detection_markup: list[str] = []
    if overlay:
        for detection in detections:
            box = detection.get('box') or {}
            x = max(0, float(box.get('x') or 0) * width)
            y = max(0, float(box.get('y') or 0) * height)
            box_width = max(1, float(box.get('width') or 0) * width)
            box_height = max(1, float(box.get('height') or 0) * height)
            label = escape(str(detection.get('label') or 'object'))
            confidence = round(float(detection.get('confidence') or 0) * 100)
            label_y = max(28, y - 10)
            detection_markup.append(f'<g class="detection-box"><rect x="{x:.1f}" y="{y:.1f}" width="{box_width:.1f}" height="{box_height:.1f}" /><text x="{x:.1f}" y="{label_y:.1f}">{label} · {confidence}%</text></g>')
    overlay_state = 'ON' if overlay else 'OFF'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n  <defs>\n    <linearGradient id="camera-bg" x1="0" x2="1" y1="0" y2="1">\n      <stop offset="0" stop-color="#101827" />\n      <stop offset="0.52" stop-color="#0b1220" />\n      <stop offset="1" stop-color="#17223a" />\n    </linearGradient>\n    <radialGradient id="lens" cx="50%" cy="45%" r="68%">\n      <stop offset="0" stop-color="#47d6ff" stop-opacity="0.22" />\n      <stop offset="0.5" stop-color="#8b5cf6" stop-opacity="0.1" />\n      <stop offset="1" stop-color="#070b13" stop-opacity="0" />\n    </radialGradient>\n    <style>\n      .grid line {{ stroke: rgba(255,255,255,.08); stroke-width: 1; }}\n      .hud {{ fill: #edf3ff; font: 700 26px Inter, Arial, sans-serif; letter-spacing: .04em; }}\n      .muted {{ fill: #91a1ba; font: 700 20px Inter, Arial, sans-serif; }}\n      .monitor-zone polygon {{ fill: rgba(71,214,255,.08); stroke: #47d6ff; stroke-width: 3; stroke-dasharray: 12 10; }}\n      .monitor-zone text {{ fill: #47d6ff; font: 800 20px Inter, Arial, sans-serif; paint-order: stroke; stroke: rgba(7,11,19,.86); stroke-width: 4; stroke-linejoin: round; }}\n      .detection-box rect {{ fill: rgba(73,230,163,.08); stroke: #49e6a3; stroke-width: 4; rx: 18; }}\n      .detection-box text {{ fill: #49e6a3; font: 800 24px Inter, Arial, sans-serif; paint-order: stroke; stroke: rgba(7,11,19,.86); stroke-width: 5; stroke-linejoin: round; }}\n    </style>\n  </defs>\n  <rect width="100%" height="100%" fill="url(#camera-bg)" />\n  <rect width="100%" height="100%" fill="url(#lens)" />\n  <g class="grid">{''.join(grid_lines)}</g>\n  <circle cx="{width * 0.74:.1f}" cy="{height * 0.34:.1f}" r="{min(width, height) * 0.16:.1f}" fill="none" stroke="rgba(71,214,255,.16)" stroke-width="3" />\n  <circle cx="{width * 0.28:.1f}" cy="{height * 0.62:.1f}" r="{min(width, height) * 0.12:.1f}" fill="none" stroke="rgba(139,92,246,.16)" stroke-width="3" />\n  {''.join(zone_markup)}\n  {''.join(detection_markup)}\n  <rect x="24" y="24" width="520" height="116" rx="20" fill="rgba(7,11,19,.58)" stroke="rgba(255,255,255,.12)" />\n  <text x="48" y="70" class="hud">{escape(camera_name).upper()}</text>\n  <text x="48" y="112" class="muted">Frame #{frame_number} · {timestamp} · Overlay {overlay_state}</text>\n</svg>'''

def render_live_snapshot_jpeg_overlay(image_bytes: bytes, detections: list[dict[str, Any]]) -> bytes:
    if not detections:
        return image_bytes
    try:
        import cv2
        import numpy as np
    except ImportError:
        return image_bytes
    data = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        return image_bytes
    height, width = image.shape[:2]
    for detection in detections:
        if detection.get('alert_matched') is False and detection.get('alert_triggered') is False:
            continue
        box = detection.get('box') or {}
        x = int(max(0, min(1, float(box.get('x') or 0))) * width)
        y = int(max(0, min(1, float(box.get('y') or 0))) * height)
        box_width = int(max(0.001, min(1, float(box.get('width') or 0))) * width)
        box_height = int(max(0.001, min(1, float(box.get('height') or 0))) * height)
        x2 = min(width - 1, x + box_width)
        y2 = min(height - 1, y + box_height)
        label = str(detection.get('label') or 'object')
        confidence = round(float(detection.get('confidence') or 0) * 100)
        text = f'{label} {confidence}%'
        cv2.rectangle(image, (x, y), (x2, y2), (73, 230, 163), 2)
        text_y = max(22, y - 8)
        (text_width, text_height), _baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.62, 2)
        cv2.rectangle(image, (x, text_y - text_height - 8), (min(width - 1, x + text_width + 10), text_y + 4), (7, 11, 19), -1)
        cv2.putText(image, text, (x + 5, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (73, 230, 163), 2, cv2.LINE_AA)
    ok, encoded = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return encoded.tobytes() if ok else image_bytes

def delete_recording_files(recordings: list[dict[str, Any]]) -> None:
    for recording in recordings:
        raw_file_path = str(recording.get('file_path') or '')
        file_path = Path(raw_file_path)
        if file_path.exists() and file_path.is_file():
            file_path.unlink(missing_ok=True)
        if raw_file_path:
            playback_paths = [recording_playback_sidecar_path(file_path), recording_track_sidecar_path(file_path), file_path.with_name(f'{file_path.stem}.playback.failed'), file_path.with_name(f'{file_path.stem}.h264.mp4'), file_path.with_name(f'{file_path.stem}.browser.mp4'), file_path.with_name(f'{file_path.stem}.playback.mp4'), file_path.with_name(f'{file_path.name}.meta.json')]
            for playback_path in playback_paths:
                if playback_path.exists() and playback_path.is_file():
                    playback_path.unlink(missing_ok=True)
        thumbnail_path = recording.get('thumbnail_path')
        if thumbnail_path:
            thumbnail = Path(str(thumbnail_path))
            if thumbnail.exists() and thumbnail.is_file():
                thumbnail.unlink(missing_ok=True)

def clear_runtime_media_directory(path_value: str | None) -> int:
    if not path_value:
        return 0
    path = Path(str(path_value))
    if not path.exists() or not path.is_dir():
        return 0
    deleted = 0
    for child in path.iterdir():
        try:
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
                deleted += 1
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                deleted += 1
        except OSError:
            continue
    return deleted

def recording_playback_sidecar_path(file_path: Path) -> Path:
    return file_path.with_name(f'{file_path.stem}.h264-audio.mp4')

def recording_stream_path(file_path: Path) -> Path:
    playback_path = recording_playback_sidecar_path(file_path)
    if playback_path.exists() and file_path.exists() and (playback_path.stat().st_mtime >= file_path.stat().st_mtime):
        return playback_path
    if file_path.suffix.lower() == '.mp4' and mp4_is_browser_playable(file_path):
        return file_path
    failed_marker = file_path.with_name(f'{file_path.stem}.playback.failed')
    if failed_marker.exists() and file_path.exists() and (failed_marker.stat().st_mtime >= file_path.stat().st_mtime):
        return file_path
    try:
        transcode_recording_to_mp4(file_path, playback_path)
    except Exception as exc:
        logger.warning('Recording playback conversion failed for %s: %s', file_path, exc)
        try:
            failed_marker.write_bytes(b'')
        except OSError:
            pass
        return file_path
    failed_marker.unlink(missing_ok=True)
    return playback_path if playback_path.exists() else file_path

def recording_track_sidecar_path(file_path: Path) -> Path:
    return file_path.with_name(f'{file_path.stem}.track.json')

def write_recording_detection_track(file_path: Path, track: list[dict[str, Any]]) -> None:
    recording_track_sidecar_path(file_path).write_text(json.dumps(track), encoding='utf-8')

def load_recording_detection_track(file_path: Path) -> list[dict[str, Any]] | None:
    sidecar = recording_track_sidecar_path(file_path)
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    if not any((isinstance(sample, dict) and sample.get('detections') for sample in data)):
        return None
    return data

def write_live_history_detection_track(recording_id: int | None, file_path: Path, camera_id: str | None, start_ts: float, end_ts: float) -> bool:
    """Persist the monitor's detections over the capture window as the clip's track.

    This replaces the old post-recording "bake" that re-decoded the clip and ran
    detection on every sampled frame: the background monitor already analyzed
    these frames live, so slicing its history costs nothing. An all-empty slice
    is still written - it marks the clip as analyzed while the loader keeps
    reporting it as missing so playback falls back to the static event boxes.
    """
    if not str(file_path):
        return False
    track = build_track_from_live_history(camera_id, start_ts, end_ts)
    if track is None:
        logger.debug('No live detection history covers recording %s (%s); no track written.', recording_id, file_path.name)
        return False
    try:
        write_recording_detection_track(file_path, track)
    except OSError as exc:
        logger.warning('Could not write detection track for recording %s: %s', recording_id, exc)
        return False
    localized = sum((1 for sample in track if sample.get('detections')))
    logger.info('Saved detection track for recording %s from live history (%d samples, %d with detections).', recording_id, len(track), localized)
    return True

def _recording_capture_window(recording: dict[str, Any]) -> tuple[float, float] | None:
    """Return the recording's (start_ts, end_ts) from its stored timing."""
    try:
        started_at = datetime.fromisoformat(str(recording.get('started_at') or ''))
    except ValueError:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    start_ts = started_at.timestamp()
    try:
        ended_at = datetime.fromisoformat(str(recording.get('ended_at') or ''))
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=timezone.utc)
        end_ts = ended_at.timestamp()
    except ValueError:
        end_ts = start_ts + max(1.0, float(recording.get('duration_seconds') or 0))
    if end_ts <= start_ts:
        return None
    return (start_ts, end_ts)

def probe_video_codec(file_path: Path) -> str | None:
    """Return the first video stream's codec name (e.g. 'h264', 'hevc'), or None."""
    return probe_stream_codec(file_path, 'v:0')

def probe_audio_codec(file_path: Path) -> str | None:
    """Return the first audio stream's codec name (e.g. 'aac', 'pcm_mulaw'), or None."""
    return probe_stream_codec(file_path, 'a:0')

def probe_stream_codec(file_path: Path, stream_selector: str) -> str | None:
    if not file_path.exists() or file_path.stat().st_size <= 0:
        return None
    if not _FFPROBE:
        return None
    ffprobe = _FFPROBE
    command = [ffprobe, '-v', 'error', '-select_streams', stream_selector, '-show_entries', 'stream=codec_name', '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    codec = (result.stdout or '').strip().lower()
    return codec or None if result.returncode == 0 else None

def mp4_is_browser_playable(file_path: Path) -> bool:
    if probe_video_codec(file_path) != 'h264':
        return False
    audio_codec = probe_audio_codec(file_path)
    return audio_codec in {None, '', 'aac', 'mp3'}

def probe_video_duration(file_path: Path) -> float | None:
    if not _FFPROBE or not file_path.exists():
        return None
    ffprobe = _FFPROBE
    command = [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        return float((result.stdout or '').strip()) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

def transcode_recording_to_mp4(source_path: Path, output_path: Path) -> None:
    ffmpeg = _FFMPEG or shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required to convert recordings for browser playback.')
    tmp_path = output_path.with_name(f'{output_path.stem}.tmp{output_path.suffix}')
    if tmp_path.exists():
        tmp_path.unlink(missing_ok=True)
    command = [ffmpeg, '-y', '-fflags', '+discardcorrupt', '-err_detect', 'ignore_err', '-i', str(source_path), '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-c:a', 'aac', '-b:a', '128k', '-preset', 'veryfast', '-profile:v', 'main', '-level', '4.0', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(tmp_path)]
    duration = probe_video_duration(source_path) or 0.0
    timeout_seconds = max(120, int(duration * 3) + 60)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    if not tmp_path.exists():
        raise RuntimeError('MP4 conversion did not create an output file.')
    if result.returncode != 0 and (not mp4_has_video_stream(tmp_path)):
        tmp_path.unlink(missing_ok=True)
        error_detail = f'{result.stderr[:500]}\n...\n{result.stderr[-1000:]}'
        raise RuntimeError(f'ffmpeg failed to convert recording for browser playback: {error_detail}')
    if not mp4_has_video_stream(tmp_path):
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError('Converted MP4 does not contain a video stream.')
    tmp_path.replace(output_path)

def mp4_has_video_stream(file_path: Path) -> bool:
    if not _FFPROBE:
        return file_path.exists() and file_path.stat().st_size > 0
    command = [_FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_name', '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool((result.stdout or '').strip())
DATABASE_RESTORE_REQUIRED_TABLES = {'events', 'detections', 'app_settings', 'users'}

def backup_directory() -> Path:
    backups_dir = Path(str(effective_storage_config().get('data_dir') or 'data')) / 'backups'
    backups_dir.mkdir(parents=True, exist_ok=True)
    return backups_dir

def safe_backup_timestamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

def create_database_backup(prefix: str='daygle-database') -> Path:
    backup_path = backup_directory() / f'{prefix}-{safe_backup_timestamp()}-{secrets.token_hex(4)}.sqlite3'
    try:
        source = sqlite3.connect(database.database_path)
        try:
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path

def validate_restore_database(path: Path) -> None:
    try:
        db = sqlite3.connect(path)
        try:
            integrity = db.execute('PRAGMA integrity_check').fetchone()
            if not integrity or str(integrity[0]).lower() != 'ok':
                raise HTTPException(status_code=400, detail='Uploaded database failed SQLite integrity check.')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
            missing = sorted(DATABASE_RESTORE_REQUIRED_TABLES - tables)
            if missing:
                raise HTTPException(status_code=400, detail=f"Uploaded database is missing required table(s): {', '.join(missing)}.")
            admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()[0]
            if int(admin_count) < 1:
                raise HTTPException(status_code=400, detail='Uploaded database must include at least one active administrator account.')
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        raise HTTPException(status_code=400, detail='Uploaded file is not a valid SQLite database.') from exc
DATABASE_RESTORE_LOCK = threading.Lock()

def overwrite_database_from_file(restore_source: Path) -> None:
    source = sqlite3.connect(str(restore_source))
    try:
        destination = sqlite3.connect(str(database.database_path))
        try:
            source.backup(destination)
            checkpoint = destination.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint and checkpoint[0] != 0:
                logger.warning('Database restore WAL checkpoint returned error code %s', checkpoint[0])
        finally:
            destination.close()
    finally:
        source.close()

def refresh_runtime_after_database_restore() -> None:
    database.init()
    auth.init()
    apply_cameras_settings(effective_cameras_config())
    apply_storage_and_recording_settings()
    auth.apply_config(effective_auth_config())

def purge_recordings_by_policy(*, force: bool=False) -> dict[str, Any]:
    recording_settings = effective_recording_config()
    if not force and (not normalize_bool_setting(recording_settings.get('auto_purge_enabled', True), True)):
        return {'purged': 0, 'files_deleted': 0, 'bytes_deleted': 0, 'recordings': []}
    retention_days = int(recording_settings.get('retention_days', 14))
    max_storage_gb = int(recording_settings.get('max_storage_gb', 20))
    older_than = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    max_storage_bytes = max_storage_gb * 1024 * 1024 * 1024
    purged = database.purge_recordings(older_than=older_than, max_storage_bytes=max_storage_bytes)
    bytes_deleted = 0
    files_deleted = 0
    for recording in purged:
        file_path = Path(str(recording.get('file_path') or ''))
        if file_path.exists() and file_path.is_file():
            bytes_deleted += file_path.stat().st_size
            files_deleted += 1
    delete_recording_files(purged)
    return {'purged': len(purged), 'files_deleted': files_deleted, 'bytes_deleted': bytes_deleted, 'recordings': purged}

def purge_camera_diagnostics_by_policy() -> int:
    """Age out old camera diagnostic events.

    Two bounds keep the log from growing without limit: a hard row cap enforced
    on every insert (see EventDatabase.add_camera_diagnostic) and this
    time-based purge. Retention follows the same recording retention window
    (``retention_days``) so diagnostics age out alongside the recordings they
    explain.
    """
    try:
        retention_days = max(1, int(effective_recording_config().get('retention_days', 14)))
        older_than = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        return database.purge_camera_diagnostics_older_than(older_than)
    except Exception as exc:
        logger.debug('Camera diagnostics purge failed: %s', exc)
        return 0

@app.get('/login')
def login_page(request: Request, error: str | None=None):
    if auth_enabled and auth.users_exist() and auth.get_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return RedirectResponse('/', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return csrf_token_response(request, 'Login', f'\n<h1>Sign In</h1><p class="muted">Enter your Daygle AI Camera credentials.</p>{error_html}\n<form class="form-stack" method="post" action="/login">\n  <input type="hidden" name="csrf_token" value="{{csrf}}" />\n  <label>Username<input name="username" autocomplete="username" required /></label>\n  <label>Password<input name="password" type="password" autocomplete="current-password" required /></label>\n  <button class="primary" type="submit">Sign In</button>\n</form>')

@app.post('/login')
async def login(request: Request):
    data = await form_data(request)
    if data.get('csrf_token') != request.cookies.get(CSRF_COOKIE):
        return login_page(request, 'Security token expired. Try again.')
    username = data.get('username', '')
    ip = _request_ip(request)
    try:
        _user, token, _csrf_token, expires_at = auth.authenticate(username, data.get('password', ''), ip)
    except AuthError as exc:
        try:
            database.add_audit_log(created_at=utc_now(), user_id=None, username=username, action='login', resource='session', ip_address=ip, status='failed', details={'reason': str(exc)})
        except Exception as unexpected_exc:
            logger.warning('Unexpected error during login callback: %s', unexpected_exc)
        return login_page(request, str(exc))
    try:
        database.add_audit_log(created_at=utc_now(), user_id=int(_user['id']), username=str(_user['username']), action='login', resource='session', ip_address=ip, status='success')
    except Exception as unexpected_exc:
        logger.warning('Unexpected error during login: %s', unexpected_exc)
    response = RedirectResponse('/', status_code=303)
    set_session_cookie(response, request, token, expires_at)
    response.delete_cookie(CSRF_COOKIE)
    return response

@app.get('/setup')
def setup_page(request: Request, error: str | None=None):
    if auth_enabled and auth.users_exist():
        return RedirectResponse('/login', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return csrf_token_response(request, 'Initial setup', f'\n<h1>Create administrator</h1><p class="muted">This one-time setup is disabled after the first user is created.</p>{error_html}\n<form class="form-stack" method="post" action="/setup">\n  <input type="hidden" name="csrf_token" value="{{csrf}}" />\n  <label>First name<input name="first_name" autocomplete="given-name" /></label>\n  <label>Last name<input name="last_name" autocomplete="family-name" /></label>\n  <label>Email<input name="email" type="email" autocomplete="email" /></label>\n  <label>Username<input name="username" value="admin" autocomplete="username" required /></label>\n  <label>Password<input name="password" type="password" autocomplete="new-password" required /></label>\n  <label>Confirm password<input name="confirm_password" type="password" autocomplete="new-password" required /></label>\n  <button class="primary" type="submit">Create Admin Account</button>\n</form>')

@app.post('/setup')
async def setup(request: Request):
    if auth.users_exist():
        return RedirectResponse('/login', status_code=303)
    data = await form_data(request)
    if data.get('csrf_token') != request.cookies.get(CSRF_COOKIE):
        return setup_page(request, 'Security token expired. Try again.')
    if data.get('password') != data.get('confirm_password'):
        return setup_page(request, 'Passwords do not match.')
    try:
        auth.create_user(data.get('username', ''), data.get('password', ''), role='admin', first_name=data.get('first_name', ''), last_name=data.get('last_name', ''), email=data.get('email', ''))
    except AuthError as exc:
        return setup_page(request, str(exc))
    return RedirectResponse('/login', status_code=303)

@app.get('/logout')
def logout_get(request: Request):
    return RedirectResponse('/login', status_code=303)

@app.post('/logout')
def logout_post(request: Request):
    session = require_session(request)
    if request.headers.get(CSRF_HEADER) != session['csrf_token']:
        return JSONResponse({'detail': 'CSRF token missing or invalid'}, status_code=403)
    write_audit_log(request, 'logout', 'session')
    auth.delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({'ok': True})
    clear_auth_cookies(response)
    return response

def _parse_header_value(header: str, key: str) -> str | None:
    for part in header.split(';'):
        part = part.strip()
        if part.startswith(f'{key}='):
            return part.split('=', 1)[1].strip('"')
    return None

async def _read_uploaded_image(request: Request) -> tuple[bytes, str | None, str | None]:
    content_type = request.headers.get('content-type', '')
    body = await request.body()
    if content_type.startswith('image/'):
        return (body, None, content_type)
    boundary = _parse_header_value(content_type, 'boundary')
    if not boundary:
        raise HTTPException(status_code=400, detail='Expected multipart image upload')
    delimiter = ('--' + boundary).encode('utf-8')
    for part in body.split(delimiter):
        if b'Content-Disposition' not in part or b'name="file"' not in part:
            continue
        header_blob, separator, payload = part.partition(b'\r\n\r\n')
        if not separator:
            continue
        headers = header_blob.decode('utf-8', errors='replace')
        filename = _parse_header_value(headers, 'filename')
        uploaded_type = None
        for line in headers.splitlines():
            if line.lower().startswith('content-type:'):
                uploaded_type = line.split(':', 1)[1].strip()
                break
        if payload.endswith(b'\r\n'):
            payload = payload[:-2]
        return (payload, filename, uploaded_type)
    raise HTTPException(status_code=400, detail='Multipart upload must include a file field named file')

@app.get('/')
def root():
    index_path = web_dir / 'index.html'
    if index_path.exists():
        return FileResponse(index_path)
    return {'application': 'Daygle AI Camera', 'status': 'running'}

@app.get('/favicon.ico')
def favicon():
    favicon_path = web_dir / 'favicon.svg'
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type='image/svg+xml')
    raise HTTPException(status_code=404, detail='Favicon not found')

@app.get('/live')
def live_page():
    live_path = web_dir / 'live.html'
    if live_path.exists():
        return FileResponse(live_path)
    return root()

@app.get('/zones')
def zones_page():
    zones_path = web_dir / 'zones.html'
    if zones_path.exists():
        return FileResponse(zones_path)
    return root()

@app.get('/sounds')
def sounds_page():
    sounds_path = web_dir / 'sounds.html'
    if sounds_path.exists():
        return FileResponse(sounds_path)
    return root()

@app.get('/cameras')
def cameras_page():
    cameras_path = web_dir / 'cameras.html'
    if cameras_path.exists():
        return FileResponse(cameras_path)
    return root()

@app.get('/events')
@app.get('/alerts')
@app.get('/search')
def dashboard_aliases():
    return root()

@app.get('/recordings')
def recordings_page():
    recordings_path = web_dir / 'recordings.html'
    if recordings_path.exists():
        return FileResponse(recordings_path)
    return root()

@app.get('/recordings/timeline')
def recordings_timeline_page():
    timeline_path = web_dir / 'timeline.html'
    if timeline_path.exists():
        return FileResponse(timeline_path)
    return root()

@app.get('/onnx')
def onnx_page():
    ai_path = web_dir / 'onnx.html'
    if ai_path.exists():
        return FileResponse(ai_path)
    return root()

@app.get('/ai')
def ai_settings_page():
    return RedirectResponse('/onnx', status_code=308)

@app.get('/yamnet-tflite')
def yamnet_tflite_page():
    yamnet_path = web_dir / 'yamnet-tflite.html'
    if yamnet_path.exists():
        return FileResponse(yamnet_path)
    return root()

@app.get('/yamnet')
def yamnet_page():
    return RedirectResponse('/yamnet-tflite', status_code=308)

@app.get('/profile')
def profile_page():
    profile_path = web_dir / 'profile.html'
    if profile_path.exists():
        return FileResponse(profile_path)
    return root()

@app.get('/settings')
def system_settings_page():
    settings_path = web_dir / 'settings.html'
    if settings_path.exists():
        return FileResponse(settings_path)
    return root()

@app.get('/users')
def users_page():
    users_path = web_dir / 'users.html'
    if users_path.exists():
        return FileResponse(users_path)
    return root()

@app.get('/api/auth/me')
def me(request: Request):
    session = require_session(request)
    return {'user': session['user'], 'csrf_token': session['csrf_token'], 'expires_at': session['expires_at']}

@app.post('/api/detect/frame')
async def detect_frame(request: Request):
    image_bytes, _filename, _content_type = await _read_uploaded_image(request)
    if not image_bytes:
        raise HTTPException(status_code=400, detail='Uploaded image is empty')
    ai_settings = effective_ai_config()
    ai_state = ai_status_payload(ai_settings)
    ai_error: str | None = None
    min_confidence = compute_minimum_rule_confidence()

    def _run_detection() -> list:
        return detector.detect_image(image_bytes, confidence=min_confidence)
    try:
        detections = await asyncio.get_event_loop().run_in_executor(None, _run_detection)
    except DetectorUnavailableError as exc:
        detections = []
        ai_error = str(exc) or ai_state.get('last_detector_error') or ai_state.get('error') or 'Detector unavailable.'
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'detections': detections, 'count': len(detections), 'ai_backend': ai_state['active_backend'], 'ai_error': ai_error}

@app.get('/api/stats')
def stats():
    result = database.stats()
    result['total_cameras'] = len(cameras_config)
    return result

@app.delete('/api/objects')
def delete_all_objects(request: Request):
    require_admin(request)
    deleted = database.delete_all_objects()
    return {'ok': True, 'deleted': deleted}

@app.get('/api/config')
def runtime_config():
    ai_state = ai_status_payload()
    ai_cfg = effective_ai_config()
    return {'server': {'host': config.get('server', {}).get('host'), 'port': config.get('server', {}).get('port')}, 'camera': get_camera_config(None), 'cameras': effective_cameras_config(), 'ai': {'enabled': ai_cfg.get('enabled'), 'backend': ai_cfg.get('backend'), 'confidence': ai_cfg.get('confidence'), 'iou_threshold': ai_cfg.get('iou_threshold'), 'input_size': ai_cfg.get('input_size'), 'model_path': ai_cfg.get('model_path'), 'labels_path': ai_cfg.get('labels_path'), 'active_backend': ai_state['active_backend'], 'mode': ai_state['mode'], 'available': ai_state['inference_available'], 'model_loaded': ai_state['model_loaded'], 'error': ai_state['error'], 'categories': ai_cfg.get('categories', [])}, 'alerts': config.get('alerts', {}), 'auth': {'enabled': auth_enabled, 'session_timeout_hours': effective_auth_config().get('session_timeout_hours'), 'max_login_attempts': effective_auth_config().get('max_login_attempts'), 'lockout_minutes': effective_auth_config().get('lockout_minutes')}, 'storage': {'database': effective_storage_config().get('database'), 'snapshots_dir': effective_storage_config().get('snapshots_dir'), 'recordings_dir': effective_storage_config().get('recordings_dir')}, 'live': effective_live_config(), 'recording': effective_recording_config()}

@app.get('/api/labels')
def available_labels():
    """Return available labels for the recordings filter dropdown."""
    object_labels: list[str] = []
    ai_config = effective_ai_config()
    labels_path = ai_config.get('labels_path', 'models/coco.names')
    try:
        p = Path(labels_path)
        if p.exists():
            object_labels = [line.strip() for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    except Exception:
        pass
    sound_labels = [{'id': class_id, 'label': meta['label'], 'description': meta.get('description', '')} for class_id, meta in SOUND_CLASSES.items()]
    return {'objects': object_labels, 'sounds': sound_labels}

def _parse_iso_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ''))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _recording_timeline_segment(recording: dict[str, Any], day_start: datetime, day_end: datetime) -> dict[str, Any] | None:
    started_at = _parse_iso_datetime(recording.get('started_at'))
    ended_at = _parse_iso_datetime(recording.get('ended_at'))
    duration_seconds = max(0.0, float(recording.get('duration_seconds') or 0.0))
    if started_at is None:
        return None
    if ended_at is None or ended_at <= started_at:
        ended_at = started_at + timedelta(seconds=max(duration_seconds, 1.0))
    visible_start = max(started_at, day_start)
    visible_end = min(ended_at, day_end)
    if visible_end <= visible_start:
        return None
    trigger_type = str(recording.get('trigger_type') or 'motion').lower()
    trigger_label = str(recording.get('trigger_label') or '').strip().lower()
    color_key = trigger_label if trigger_type in {'human', 'object', 'alert'} and trigger_label else trigger_type
    return {**recording, 'timeline_start_seconds': max(0.0, (visible_start - day_start).total_seconds()), 'timeline_end_seconds': min(86400.0, (visible_end - day_start).total_seconds()), 'timeline_duration_seconds': max(1.0, (visible_end - visible_start).total_seconds()), 'color_key': color_key, 'color_label': color_key}

@app.delete('/api/system/runtime-data')
def delete_runtime_data(request: Request):
    require_admin(request)
    recordings = database.delete_all_recordings()
    delete_recording_files(recordings)
    deleted_events = database.delete_all_events()
    deleted_alerts = database.delete_all_alerts()
    deleted_objects = database.delete_all_objects()
    deleted_diagnostics = database.delete_all_camera_diagnostics()
    storage_config = effective_storage_config()
    deleted_snapshots = clear_runtime_media_directory(storage_config.get('snapshots_dir'))
    deleted_event_artifacts = clear_runtime_media_directory(storage_config.get('events_dir'))
    with active_rtsp_recordings_lock:
        active_rtsp_recordings.clear()
    result = {'ok': True, 'deleted': {'recordings': len(recordings), 'events': deleted_events, 'alerts': deleted_alerts, 'objects': deleted_objects, 'camera_diagnostics': deleted_diagnostics, 'snapshot_files': deleted_snapshots, 'event_artifacts': deleted_event_artifacts}, 'preserved': ['settings', 'users', 'sessions', 'rules']}
    write_audit_log(request, 'delete_all', 'runtime_data', details=result['deleted'])
    return result

def validate_ai_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_ai_config()
    allowed = {'enabled', 'backend', 'confidence', 'iou_threshold', 'input_size', 'model_path', 'labels_path', 'device', 'gpu_mem_limit', 'inference_threads', 'max_concurrent_inferences'}
    updated = {key: current.get(key) for key in allowed if key in current}
    for key, value in payload.items():
        if key in allowed:
            updated[key] = value
    enabled_value = updated.get('enabled', True)
    if isinstance(enabled_value, str):
        updated['enabled'] = enabled_value.lower() in {'1', 'true', 'yes', 'on'}
    else:
        updated['enabled'] = bool(enabled_value)
    backend = str(updated.get('backend', 'onnx')).lower()
    if backend != 'onnx':
        raise HTTPException(status_code=400, detail='AI backend must be onnx.')
    updated['backend'] = backend
    for field in ('confidence', 'iou_threshold'):
        try:
            updated[field] = float(updated.get(field, 0.45))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f'{field} must be a number.') from exc
        if not 0 <= updated[field] <= 1:
            raise HTTPException(status_code=400, detail=f'{field} must be between 0 and 1.')
    try:
        updated['input_size'] = int(updated.get('input_size', 640))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='input_size must be an integer.') from exc
    if updated['input_size'] < 32 or updated['input_size'] > 2048:
        raise HTTPException(status_code=400, detail='input_size must be between 32 and 2048.')
    device = str(updated.get('device', 'auto')).lower()
    if device not in ('auto', 'cpu', 'cuda'):
        raise HTTPException(status_code=400, detail="device must be 'auto', 'cpu', or 'cuda'.")
    updated['device'] = device
    if 'gpu_mem_limit' in payload:
        gpu_mem_limit = payload['gpu_mem_limit']
        if gpu_mem_limit is not None and gpu_mem_limit != '':
            try:
                gpu_mem_limit = int(gpu_mem_limit)
                if gpu_mem_limit < 0:
                    raise ValueError
                updated['gpu_mem_limit'] = gpu_mem_limit
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail='gpu_mem_limit must be a non-negative integer (bytes), or 0 for unlimited.') from exc
        else:
            updated['gpu_mem_limit'] = 0
    for field, min_val, max_val in (('inference_threads', 1, 32), ('max_concurrent_inferences', 1, 16)):
        raw = payload.get(field)
        if raw is not None and raw != '':
            try:
                val = int(raw)
                if not min_val <= val <= max_val:
                    raise ValueError
                updated[field] = val
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f'{field} must be an integer between {min_val} and {max_val}.') from exc
        else:
            updated.pop(field, None)
    updated['model_path'] = str(updated.get('model_path') or current.get('model_path') or 'models/yolov8n.onnx')
    updated['labels_path'] = str(updated.get('labels_path') or current.get('labels_path') or 'models/coco.names')
    return updated

def detector_status(ai_settings: dict[str, Any]) -> dict[str, Any]:
    ai_status = ai_status_payload(ai_settings)
    categories = ai_settings.get('categories', config.get('ai', {}).get('categories', []))
    labels = load_labels(ai_settings.get('labels_path'), categories) or list(categories)
    return {**ai_settings, 'active_backend': ai_status['active_backend'], 'configured_backend': ai_status['configured_backend'], 'mode': ai_status['mode'], 'available': ai_status['inference_available'], 'model_loaded': ai_status['model_loaded'], 'detector_loaded': ai_status['detector_loaded'], 'model_exists': ai_status['model_exists'], 'onnx_runtime_installed': ai_status['onnx_runtime_installed'], 'active_config_source': ai_status['active_config_source'], 'error': ai_status['error'], 'last_detector_error': ai_status['last_detector_error'], 'categories': categories, 'available_labels': labels}

def validate_alert_email_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_email_alert_settings()
    allowed = {'enabled', 'host', 'port', 'username', 'password', 'from_address', 'use_tls', 'use_ssl'}
    updated = {key: current.get(key) for key in allowed if key in current}
    for key, value in payload.items():
        if key in allowed:
            updated[key] = value
    for key in ('enabled', 'use_tls', 'use_ssl'):
        value = updated.get(key, False)
        updated[key] = value.lower() in {'1', 'true', 'yes', 'on'} if isinstance(value, str) else bool(value)
    try:
        updated['port'] = int(updated.get('port') or 587)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='SMTP port must be an integer.') from exc
    if not 1 <= updated['port'] <= 65535:
        raise HTTPException(status_code=400, detail='SMTP port must be between 1 and 65535.')
    for key in ('host', 'username', 'password', 'from_address'):
        updated[key] = str(updated.get(key) or '').strip()
    if updated['enabled'] and (not updated['host']):
        raise HTTPException(status_code=400, detail='SMTP host is required when email alerts are enabled.')
    if updated['enabled'] and (not updated['from_address']):
        raise HTTPException(status_code=400, detail='From address is required when email alerts are enabled.')
    if updated['from_address'] and '@' not in updated['from_address']:
        raise HTTPException(status_code=400, detail='From address must be a valid email address.')
    if updated['use_ssl']:
        updated['use_tls'] = False
    return updated

def validate_push_notification_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_push_notification_settings()
    allowed = {'enabled', 'server_url', 'topic', 'priority', 'username', 'password'}
    updated = {key: current.get(key) for key in allowed if key in current}
    for key, value in payload.items():
        if key in allowed:
            updated[key] = value
    updated['enabled'] = normalize_bool_setting(updated.get('enabled', False))
    for key in ('server_url', 'topic', 'priority', 'username', 'password'):
        updated[key] = str(updated.get(key) or '').strip()
    if not updated['server_url']:
        updated['server_url'] = 'https://ntfy.sh'
    if not updated['priority']:
        updated['priority'] = 'default'
    valid_priorities = {'min', 'low', 'default', 'high', 'urgent'}
    if updated['priority'] not in valid_priorities:
        raise HTTPException(status_code=400, detail=f"priority must be one of: {', '.join(sorted(valid_priorities))}.")
    if updated['enabled'] and (not updated['topic']):
        raise HTTPException(status_code=400, detail='Topic is required when push notifications are enabled.')
    return updated

def _int_field(payload: dict[str, Any], field: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(payload.get(field, default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f'{field} must be an integer.') from exc
    if value < minimum or value > maximum:
        raise HTTPException(status_code=400, detail=f'{field} must be between {minimum} and {maximum}.')
    return value

def validate_camera_settings(payload: dict[str, Any], current: dict[str, Any] | None=None, index: int=1) -> dict[str, Any]:
    current = current or {}
    updated = {key: current.get(key) for key in ('id', 'name', 'backend', 'device', 'width', 'height', 'fps', 'flip', 'stream_url', 'host', 'port', 'path', 'username', 'password') if key in current}
    updated.update({key: payload[key] for key in ('id', 'name', 'backend', 'device', 'flip', 'stream_url', 'host', 'port', 'path', 'username', 'password') if key in payload})
    backend = str(updated.get('backend', 'onvif')).lower()
    if backend not in {'onvif', 'rtsp'}:
        raise HTTPException(status_code=400, detail='Camera backend must be onvif or rtsp.')
    updated['backend'] = backend
    updated['id'] = normalize_camera_id(updated.get('id'), f'camera-{index}')
    updated['name'] = camera_default_name(updated, f'Camera {index}')
    updated['device'] = payload.get('device', current.get('device', 0))
    updated['width'] = _int_field({**current, **payload}, 'width', 1280, 160, 7680)
    updated['height'] = _int_field({**current, **payload}, 'height', 720, 120, 4320)
    updated['fps'] = _int_field({**current, **payload}, 'fps', 15, 1, 120)
    if 'port' in updated or 'port' in payload:
        updated['port'] = _int_field({**current, **payload}, 'port', 554, 1, 65535)
    for key in ('stream_url', 'host', 'path', 'username', 'password'):
        if key in updated:
            updated[key] = str(updated.get(key) or '').strip()
    if not updated.get('password') and current.get('password'):
        updated['password'] = current['password']
    if backend in {'onvif', 'rtsp'} and (not build_stream_url(updated)):
        raise HTTPException(status_code=400, detail='stream_url is required for ONVIF/RTSP cameras, or provide host plus optional username, password, port, and path.')
    flip = str(updated.get('flip', 'none')).lower()
    if flip not in {'none', 'horizontal', 'vertical', 'both'}:
        raise HTTPException(status_code=400, detail='flip must be none, horizontal, vertical, or both.')
    updated['flip'] = flip
    detection = default_camera_detection_settings()
    existing_detection = current.get('detection') if isinstance(current.get('detection'), dict) else {}
    payload_detection = payload.get('detection') if isinstance(payload.get('detection'), dict) else {}
    detection.update(existing_detection)
    detection.update(payload_detection)
    detection['object_detection_enabled'] = normalize_bool_setting(detection.get('object_detection_enabled', True), True)
    detection['object_labels'] = normalize_label_list(detection.get('object_labels', []))
    detection['zones'] = normalize_monitoring_zones(detection.get('zones', []))
    _migrate_legacy_camera_motion(detection)
    updated['detection'] = detection
    existing_recording = current.get('recording') if isinstance(current.get('recording'), dict) else {}
    payload_recording = payload.get('recording') if isinstance(payload.get('recording'), dict) else {}
    updated['recording'] = normalize_camera_recording_settings({**existing_recording, **payload_recording})
    existing_ptz = current.get('ptz') if isinstance(current.get('ptz'), dict) else {}
    payload_ptz = payload.get('ptz') if isinstance(payload.get('ptz'), dict) else {}
    updated['ptz'] = normalize_camera_ptz_settings({**existing_ptz, **payload_ptz})
    if 'motion' in payload:
        raw_motion = payload.get('motion') if isinstance(payload.get('motion'), dict) else {}
        cam_motion: dict[str, Any] = {}
        if raw_motion.get('pixel_threshold') is not None:
            cam_motion['pixel_threshold'] = _int_field({'pixel_threshold': raw_motion['pixel_threshold']}, 'pixel_threshold', 30, 1, 255)
        for _key in ('gate_fraction', 'scale_fraction', 'background_alpha'):
            if raw_motion.get(_key) is not None:
                try:
                    cam_motion[_key] = round(float(raw_motion[_key]), 6)
                except (TypeError, ValueError):
                    pass
        if cam_motion:
            updated['motion'] = cam_motion
    elif current.get('motion'):
        updated['motion'] = current['motion']
    return updated

def validate_cameras_settings(payload: Any) -> list[dict[str, Any]]:
    raw_cameras = payload.get('cameras') if isinstance(payload, dict) else payload
    if not isinstance(raw_cameras, list):
        raise HTTPException(status_code=400, detail='cameras must be a list.')
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    current_by_id = {str(camera_settings.get('id')): camera_settings for camera_settings in cameras_config}
    for index, raw_camera in enumerate(raw_cameras, start=1):
        if not isinstance(raw_camera, dict):
            raise HTTPException(status_code=400, detail='Each camera must be an object.')
        current = current_by_id.get(str(raw_camera.get('id'))) or (cameras_config[index - 1] if index <= len(cameras_config) else {})
        camera_settings = validate_camera_settings(raw_camera, current=current, index=index)
        if camera_settings['id'] in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate camera id: {camera_settings['id']}.")
        seen.add(camera_settings['id'])
        validated.append(camera_settings)
    return validated

def validate_recording_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_recording_config()
    merged = {**current, **payload}
    fmt = str(merged.get('format', 'mp4')).strip().lstrip('.').lower() or 'mp4'
    if fmt == 'avi':
        fmt = 'mp4'
    if fmt != 'mp4':
        raise HTTPException(status_code=400, detail='Recording format must be mp4 for browser playback.')
    return {'pre_event_seconds': _int_field(merged, 'pre_event_seconds', 10, 0, 300), 'post_event_seconds': _int_field(merged, 'post_event_seconds', 15, 0, 300), 'extension_step_seconds': _int_field(merged, 'extension_step_seconds', 45, 0, 300), 'max_clip_seconds': _int_field(merged, 'max_clip_seconds', 300, 1, 3600), 'format': fmt, 'chunk_duration_seconds': _int_field(merged, 'chunk_duration_seconds', 3600, 60, 86400), 'retention_days': _int_field(merged, 'retention_days', 14, 1, 3650), 'max_storage_gb': _int_field(merged, 'max_storage_gb', 20, 1, 100000), 'auto_purge_enabled': normalize_bool_setting(merged.get('auto_purge_enabled', True), True)}

def validate_storage_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_storage_config()
    updated = {key: str(current.get(key) or '') for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir', 'database')}
    for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir'):
        if key in payload:
            value = str(payload.get(key) or '').strip()
            if not value:
                raise HTTPException(status_code=400, detail=f'{key} cannot be blank.')
            updated[key] = value
    updated['database'] = str(config.get('storage', {}).get('database') or updated.get('database') or 'data/daygle_ai_camera.sqlite3')
    return updated

def validate_auth_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_auth_config()
    merged = {**current, **payload}
    try:
        session_timeout_hours = float(merged.get('session_timeout_hours', 12))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='session_timeout_hours must be a number.') from exc
    if session_timeout_hours < 0.25 or session_timeout_hours > 720:
        raise HTTPException(status_code=400, detail='session_timeout_hours must be between 0.25 and 720.')
    return {'session_timeout_hours': session_timeout_hours, 'max_login_attempts': _int_field(merged, 'max_login_attempts', 5, 1, 100), 'lockout_minutes': _int_field(merged, 'lockout_minutes', 15, 1, 1440)}

def validate_live_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_live_config()
    merged = {**current, **payload}
    snapshot_refresh_ms = _int_field(merged, 'snapshot_refresh_ms', 500, 150, 5000)
    detection_status_refresh_ms = _int_field(merged, 'detection_status_refresh_ms', 2000, 100, 15000)
    background_detection_enabled = normalize_bool_setting(merged.get('background_detection_enabled'), True)
    try:
        detection_interval_seconds = float(merged.get('detection_interval_seconds', 0.25))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='detection_interval_seconds must be a number.') from exc
    if detection_interval_seconds < 0.1 or detection_interval_seconds > 10:
        raise HTTPException(status_code=400, detail='detection_interval_seconds must be between 0.1 and 10.')
    try:
        event_debounce_seconds = float(merged.get('event_debounce_seconds', 10.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='event_debounce_seconds must be a number.') from exc
    if event_debounce_seconds < 0 or event_debounce_seconds > 300:
        raise HTTPException(status_code=400, detail='event_debounce_seconds must be between 0 and 300.')
    try:
        detection_history_minutes = int(float(merged.get('detection_history_minutes', 10)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='detection_history_minutes must be a whole number.') from exc
    if detection_history_minutes < 1 or detection_history_minutes > 120:
        raise HTTPException(status_code=400, detail='detection_history_minutes must be between 1 and 120.')
    motion_pixel_threshold = _int_field(merged, 'motion_pixel_threshold', 30, 1, 255)
    try:
        motion_gate_fraction = float(merged.get('motion_gate_fraction', 0.003))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='motion_gate_fraction must be a number.') from exc
    if not 0.0001 <= motion_gate_fraction <= 0.5:
        raise HTTPException(status_code=400, detail='motion_gate_fraction must be between 0.0001 and 0.5.')
    try:
        motion_scale_fraction = float(merged.get('motion_scale_fraction', 0.1))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='motion_scale_fraction must be a number.') from exc
    if not 0.001 <= motion_scale_fraction <= 1.0:
        raise HTTPException(status_code=400, detail='motion_scale_fraction must be between 0.001 and 1.0.')
    try:
        motion_background_alpha = float(merged.get('motion_background_alpha', 0.05))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='motion_background_alpha must be a number.') from exc
    if not 0.001 <= motion_background_alpha <= 0.5:
        raise HTTPException(status_code=400, detail='motion_background_alpha must be between 0.001 and 0.5.')
    periodic_scan_interval_seconds = _int_field(merged, 'periodic_scan_interval_seconds', 0, 0, 3600)
    return {'snapshot_refresh_ms': snapshot_refresh_ms, 'detection_status_refresh_ms': detection_status_refresh_ms, 'detection_interval_seconds': detection_interval_seconds, 'event_debounce_seconds': event_debounce_seconds, 'background_detection_enabled': background_detection_enabled, 'detection_history_minutes': detection_history_minutes, 'motion_pixel_threshold': motion_pixel_threshold, 'motion_gate_fraction': round(motion_gate_fraction, 6), 'motion_scale_fraction': round(motion_scale_fraction, 4), 'motion_background_alpha': round(motion_background_alpha, 4), 'periodic_scan_interval_seconds': periodic_scan_interval_seconds}

def _migrate_camera_id(old_id: str, new_id: str) -> None:
    old_key = RecordingService._camera_key(old_id)
    new_key = RecordingService._camera_key(new_id)
    with live_detection_history_lock:
        if old_id in live_detection_history:
            live_detection_history[new_id] = live_detection_history.pop(old_id)
    with _frame_motion_lock:
        if old_id in _frame_motion_prev:
            _frame_motion_prev[new_id] = _frame_motion_prev.pop(old_id)
    if recording_service is not None:
        for base in (recording_service.prebuffer_dir, recording_service.frames_dir, recording_service.audio_dir):
            old_dir = base / old_key
            new_dir = base / new_key
            if old_dir.exists() and (not new_dir.exists()):
                try:
                    old_dir.rename(new_dir)
                except OSError as exc:
                    logger.warning('Could not rename ingest dir %s → %s: %s', old_dir, new_dir, exc)

def apply_cameras_settings(settings_list: list[dict[str, Any]]) -> None:
    global camera, camera_config, cameras_config, camera_instances
    old_instances = camera_instances
    cameras_config = settings_list
    camera_config = settings_list[0] if settings_list else {}
    camera_instances = create_camera_instances(settings_list)
    camera = camera_instances[camera_config['id']] if camera_config else None
    for old_cam in (old_instances or {}).values():
        try:
            old_cam.close()
        except Exception as unexpected_exc:
            logger.warning('Unexpected error updating camera: %s', unexpected_exc)
    apply_sound_settings()

def apply_storage_and_recording_settings() -> None:
    global storage, recording_service
    storage = Storage({**config, 'storage': effective_storage_config()})
    old_service = recording_service
    recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})
    if old_service is not None:
        try:
            old_service.stop_prebuffer_workers()
            old_service.stop_all_continuous_recordings()
        except Exception as unexpected_exc:
            logger.warning('Unexpected error deleting camera: %s', unexpected_exc)

def reload_detector(ai_settings: dict[str, Any]) -> tuple[bool, str | None]:
    global detector, last_detector_error, _min_rule_confidence_cache
    _min_rule_confidence_cache = None
    previous_detector = detector
    old_session = getattr(previous_detector, 'session', None)
    if old_session is not None:
        previous_detector.session = None
        del old_session
        gc.collect()
    candidate = create_detector(ai_settings)
    candidate_error = getattr(candidate, 'unavailable_reason', None)
    if ai_settings['backend'] == 'onnx' and (not getattr(candidate, 'available', False)):
        detector = previous_detector
        last_detector_error = candidate_error or 'Failed to load ONNX detector.'
        log_detector_initialization('reload_failed')
        return (False, last_detector_error)
    detector = candidate
    last_detector_error = candidate_error
    log_detector_initialization('reload')
    return (True, last_detector_error)

def export_yolo_onnx(model_name: str, destination: Path) -> int:
    if model_name not in YOLO_MODELS:
        raise ValueError(f"Unknown model '{model_name}'. Available: {', '.join(YOLO_MODELS)}")
    info = YOLO_MODELS[model_name]
    pt_name = info['pt']
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-c', f"from ultralytics import YOLO\nmodel = YOLO('{pt_name}')\nmodel.export(format='onnx')\n"]
    result = subprocess.run(command, cwd=destination.parent, capture_output=True, text=True, timeout=600, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(details or f'Ultralytics export exited with status {result.returncode}.')
    exported = destination.parent / info['onnx']
    if exported != destination and exported.exists():
        exported.replace(destination)
    if not destination.exists():
        details = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(details or f'Ultralytics export did not create {destination.name}.')
    if destination.stat().st_size <= 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError('Exported model file is empty.')
    return destination.stat().st_size

def _do_download_model(model_name: str, switch_active: bool=True) -> dict[str, Any]:
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'. Available: {', '.join(YOLO_MODELS)}")
    info = YOLO_MODELS[model_name]
    destination = BASE_DIR / 'models' / info['onnx']
    try:
        exported_bytes = export_yolo_onnx(model_name, destination)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=502, detail=f"Failed to export {info['label']} ONNX model. Install export dependencies with `pip install ultralytics onnx`, then retry. Details: {exc}") from exc
    installed_version = _installed_package_version('ultralytics')
    with _installed_models_lock:
        installed_meta = _read_installed_models()
        installed_meta[model_name] = {'version': installed_version, 'installed_at': utc_now(), 'sha256': _sha256_file(destination)}
        _write_installed_models(installed_meta)
    ai_settings = effective_ai_config()
    rel_path = str(destination.relative_to(BASE_DIR))
    is_active = ai_settings.get('model_path') == rel_path
    if switch_active or is_active:
        updated = validate_ai_settings({**ai_settings, 'model_path': rel_path})
        database.set_setting('ai', updated, utc_now())
        reloaded, error = reload_detector(updated)
    else:
        updated = ai_settings
        reloaded = False
        error = None
    return {'ok': True, 'message': f"Exported {info['label']} ONNX to {destination.relative_to(BASE_DIR)}.", 'model_path': rel_path, 'bytes': exported_bytes, 'reload_succeeded': reloaded, 'reload_error': error, 'status': ai_status_payload(updated)}

@app.post('/api/settings/alert-email/test')
async def test_alert_email_settings(request: Request):
    payload = await request.json()
    settings = validate_alert_email_settings(payload.get('settings') if isinstance(payload.get('settings'), dict) else payload)
    recipient = str(payload.get('recipient') or settings.get('from_address') or '').strip()
    if '@' not in recipient:
        raise HTTPException(status_code=400, detail='Test recipient must be a valid email address.')
    try:
        EmailAlertService(settings).send_test(recipient)
    except EmailAlertError as exc:
        raise HTTPException(status_code=400, detail=f'Test email failed: {exc}') from exc
    return {'ok': True, 'recipient': recipient}

@app.post('/api/settings/alert-push/test')
async def test_push_notification_settings(request: Request):
    payload = await request.json()
    settings = validate_push_notification_settings(payload.get('settings') if isinstance(payload.get('settings'), dict) else payload)
    try:
        PushNotificationService(settings).send_test()
    except PushNotificationError as exc:
        raise HTTPException(status_code=400, detail=f'Test notification failed: {exc}') from exc
    return {'ok': True}

def _redact_camera(cam: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in cam.items() if k != 'password'}
    out['has_password'] = bool(cam.get('password'))
    return out

def _current_version() -> str:
    version_file = BASE_DIR / 'VERSION'
    return version_file.read_text(encoding='utf-8').strip() if version_file.exists() else 'unknown'

@app.get('/api/update/check')
def check_update(request: Request):
    require_admin(request)
    current_version = _current_version()
    try:
        req = urllib.request.Request(f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest', headers={'User-Agent': 'daygle-ai-camera-updater/1.0', 'Accept': 'application/vnd.github.v3+json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())
        tag_name = str(data.get('tag_name') or '')
        latest_version = tag_name.lstrip('v')
        update_available = bool(latest_version and current_version != 'unknown' and (_parse_semver(latest_version) > _parse_semver(current_version)))
        return {'current_version': current_version, 'latest_version': latest_version, 'tag_name': tag_name, 'html_url': str(data.get('html_url') or ''), 'release_notes': str(data.get('body') or ''), 'published_at': str(data.get('published_at') or ''), 'update_available': update_available}
    except urllib.error.HTTPError as exc:
        return {'current_version': current_version, 'latest_version': None, 'update_available': False, 'error': f'GitHub API error {exc.code}: {exc.reason}'}
    except Exception as exc:
        return {'current_version': current_version, 'latest_version': None, 'update_available': False, 'error': str(exc)}

@app.post('/api/update/apply')
def apply_update(request: Request):
    global _update_in_progress
    require_admin(request)
    with _update_lock:
        if _update_in_progress:
            raise HTTPException(status_code=409, detail='An update is already in progress.')
        _update_in_progress = True
    update_script = BASE_DIR / 'scripts' / 'update.sh'
    if not update_script.exists():
        with _update_lock:
            _update_in_progress = False
        raise HTTPException(status_code=503, detail='Update script not found.')
    try:
        result = subprocess.run(['bash', str(update_script)], capture_output=True, text=True, timeout=300, cwd=str(BASE_DIR))
    except subprocess.TimeoutExpired:
        with _update_lock:
            _update_in_progress = False
        raise HTTPException(status_code=504, detail='Update timed out after 5 minutes.')
    except Exception as exc:
        with _update_lock:
            _update_in_progress = False
        raise HTTPException(status_code=500, detail=f'Update failed: {exc}') from exc
    output = ((result.stdout or '') + ('\n' + result.stderr if result.stderr else '')).strip()
    service_restart_scheduled = False
    if result.returncode == 0:
        check = subprocess.run(['systemctl', 'is-active', 'daygle-ai-camera'], capture_output=True, text=True, timeout=5, check=False)
        if check.returncode == 0:

            def _delayed_restart() -> None:
                global _update_in_progress
                time.sleep(3)
                try:
                    subprocess.run(['systemctl', 'restart', 'daygle-ai-camera'], timeout=30, check=False)
                except Exception as exc:
                    logger.warning('Service restart after update failed: %s', exc)
                finally:
                    with _update_lock:
                        _update_in_progress = False
            threading.Thread(target=_delayed_restart, daemon=True, name='update-restart').start()
            service_restart_scheduled = True
        else:
            with _update_lock:
                _update_in_progress = False
    else:
        with _update_lock:
            _update_in_progress = False
    return {'ok': result.returncode == 0, 'output': output[-4000:], 'returncode': result.returncode, 'new_version': _current_version(), 'service_restart_scheduled': service_restart_scheduled}

@app.get('/api/audit')
def list_audit_log(request: Request, limit: int=Query(50, ge=1, le=200), offset: int=Query(0, ge=0), action: str | None=None, username: str | None=None, resource: str | None=None):
    require_admin(request)
    entries = database.list_audit_logs(limit=limit, offset=offset, action=action or None, username=username or None, resource=resource or None)
    total = database.count_audit_logs(action=action or None, username=username or None, resource=resource or None)
    return {'entries': entries, 'total': total, 'limit': limit, 'offset': offset}

@app.get('/audit')
def audit_page():
    audit_path = web_dir / 'audit.html'
    if audit_path.exists():
        return FileResponse(audit_path)
    return root()

@app.get('/api/camera-log')
def list_camera_log(request: Request, limit: int=Query(50, ge=1, le=200), offset: int=Query(0, ge=0), camera_id: str | None=None, event_type: str | None=None, severity: str | None=None):
    require_admin(request)
    entries = database.list_camera_diagnostics(limit=limit, offset=offset, camera_id=camera_id or None, event_type=event_type or None, severity=severity or None)
    total = database.count_camera_diagnostics(camera_id=camera_id or None, event_type=event_type or None, severity=severity or None)
    return {'entries': entries, 'total': total, 'limit': limit, 'offset': offset}

@app.delete('/api/camera-log')
def clear_camera_log(request: Request):
    require_admin(request)
    deleted = database.delete_all_camera_diagnostics()
    write_audit_log(request, 'delete_all', 'camera_log', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}

@app.get('/camera-log')
def camera_log_page():
    page_path = web_dir / 'camera-log.html'
    if page_path.exists():
        return FileResponse(page_path)
    return root()
from app.api.sound_router import router as sound_router
app.include_router(sound_router)
from app.api.settings_ai_router import router as settings_ai_router
app.include_router(settings_ai_router)
from app.api.recordings_router import router as recordings_router
app.include_router(recordings_router)
from app.api.recordings_router import recording_detail
if __name__ == '__main__':
    import uvicorn
    server_config = config.get('server', {})
    uvicorn.run('app.main:app', host=server_config.get('host', '0.0.0.0'), port=int(server_config.get('port', 8080)), reload=False)
from app.api.cameras_router import router as cameras_router
app.include_router(cameras_router)
from app.api.events_router import router as events_router
app.include_router(events_router)
from app.api.alerts_router import router as alerts_router
app.include_router(alerts_router)
from app.api.users_router import router as users_router
app.include_router(users_router)
from app.api.alert_email_router import router as alert_email_router
app.include_router(alert_email_router)
from app.api.alert_push_router import router as alert_push_router
app.include_router(alert_push_router)
from app.api.camera_offline_router import router as camera_offline_router
app.include_router(camera_offline_router)
from app.api.status_router import router as status_router
app.include_router(status_router)
from app.api.settings_system_router import router as settings_system_router
app.include_router(settings_system_router)
from app.api.live_router import router as live_router
app.include_router(live_router)
