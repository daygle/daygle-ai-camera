from __future__ import annotations

import asyncio
import copy
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
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.alerts import AlertEngine
from app.auth import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, AuthError, AuthService
from app.database import EventDatabase
from app.detector import DetectorUnavailableError, create_detector, load_labels
from app.email_alerts import EmailAlertError, EmailAlertService
from app.push_notifications import PushNotificationError, PushNotificationService
from app.camera_backend import OpenCvStreamCamera
from app.recordings import RecordingService
from app.settings import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, load_settings
from app.storage import Storage

logger = logging.getLogger('daygle.ai')

def _configure_file_logging() -> None:
    log_dir = Path(__file__).resolve().parent.parent / 'data' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / 'app.log'
    root = logging.getLogger()
    # Guard against duplicate handlers on re-import (tests, hot-reload)
    for existing in root.handlers:
        if isinstance(existing, logging.handlers.RotatingFileHandler) and existing.baseFilename == str(log_path):
            return
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    if not root.handlers:
        logging.basicConfig(level=logging.INFO)
    root.addHandler(handler)
    root.setLevel(logging.INFO)

_configure_file_logging()

YOLOV8N_MODEL = 'yolov8n.pt'
YOLOV8N_ONNX = 'yolov8n.onnx'

YOLO_MODELS: dict[str, dict[str, Any]] = {
    'yolov8n': {
        'pt': 'yolov8n.pt', 'onnx': 'yolov8n.onnx',
        'label': 'YOLOv8n · Nano', 'approx_mb': 6,
        'description': 'Fastest inference, lowest accuracy. Best for low-power or embedded hardware.',
    },
    'yolov8s': {
        'pt': 'yolov8s.pt', 'onnx': 'yolov8s.onnx',
        'label': 'YOLOv8s · Small', 'approx_mb': 22,
        'description': 'Good balance of speed and accuracy for most systems.',
    },
    'yolov8m': {
        'pt': 'yolov8m.pt', 'onnx': 'yolov8m.onnx',
        'label': 'YOLOv8m · Medium', 'approx_mb': 52,
        'description': 'Significantly better accuracy. Recommended for IR or night-vision cameras.',
    },
    'yolov8l': {
        'pt': 'yolov8l.pt', 'onnx': 'yolov8l.onnx',
        'label': 'YOLOv8l · Large', 'approx_mb': 87,
        'description': 'High accuracy. Requires a capable CPU or GPU.',
    },
    'yolov8x': {
        'pt': 'yolov8x.pt', 'onnx': 'yolov8x.onnx',
        'label': 'YOLOv8x · Extra Large', 'approx_mb': 131,
        'description': 'Best possible accuracy. GPU strongly recommended.',
    },
}
ONE_PIXEL_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04'
    b'\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'
)

config = load_settings()

auth_config = config.get('auth', {})
auth_enabled = bool(auth_config.get('enabled', True))

@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    log_detector_initialization()
    start_live_alert_monitor()
    try:
        yield
    finally:
        recording_service.stop_prebuffer_workers()
        recording_service.stop_all_continuous_recordings()
        stop_live_alert_monitor()


app = FastAPI(title='Daygle AI Camera', lifespan=app_lifespan)

BASE_DIR = Path(__file__).resolve().parent.parent
web_dir = BASE_DIR / 'web'
static_dir = web_dir
if static_dir.exists():
    app.mount('/static', StaticFiles(directory=static_dir), name='static')

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def effective_ai_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('ai', {}))
    override = database.get_setting('ai')
    if isinstance(override, dict):
        settings.update(override)
    return settings


_min_rule_confidence_cache: tuple[float, float] | None = None  # (value, timestamp)
_MIN_RULE_CONFIDENCE_TTL = 5.0  # seconds; short enough to pick up user edits promptly


def compute_minimum_rule_confidence(fallback: float | None = None) -> float:
    """Return the lowest min_confidence across all enabled object rules so YOLO's floor never silently suppresses per-rule thresholds.

    Falls back to the configured global AI confidence when no zone rules define a
    lower threshold, so the model detection threshold always matches user expectation.

    Result is cached for _MIN_RULE_CONFIDENCE_TTL seconds to avoid a database
    read on every detection frame (called at ~4 Hz per camera from the hot path).
    """
    global _min_rule_confidence_cache
    if _min_rule_confidence_cache is not None:
        cached_value, cached_at = _min_rule_confidence_cache
        if time.time() - cached_at < _MIN_RULE_CONFIDENCE_TTL:
            return cached_value

    if fallback is None:
        fallback = float(effective_ai_config().get('confidence') or 0.45)

    # Start at the global confidence floor so zone rules with higher thresholds
    # never silently raise YOLO's detection threshold above the global setting.
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
    settings = {
        'snapshot_refresh_ms': 500,
        'detection_status_refresh_ms': 2000,
        # 0.5s (2 Hz/camera) instead of 0.25s halves continuous YOLO load while
        # still catching walking subjects. Tunable in Settings (0.1-10s); raise
        # further to cut CPU more on low-power hardware.
        'detection_interval_seconds': 0.5,
        'event_debounce_seconds': 10.0,
        'background_detection_enabled': True,
        'detection_history_minutes': 10,
    }
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
    base.update({
        'enabled': camera_recording['enabled'],
        'continuous': camera_recording['continuous'],
        'record_on_alert': camera_recording['record_on_alert'],
        'mode': 'continuous' if camera_recording['continuous'] else 'motion',
        'record_on_motion': False,
        'record_on_human': False,
        'record_on_objects': [],
    })
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
# Rolling per-camera history of the background monitor's detections. Recording
# detection tracks are sliced out of this history when a clip finalizes, so
# playback overlays cost no extra decoding or inference: every box was already
# computed live for alerts. At one detection cycle per ~0.5s the cap covers
# roughly the last 40 minutes per camera.

live_detection_history: dict[str, deque] = {}
live_detection_history_lock = threading.Lock()
live_event_last_emitted: dict[str, dict[str, Any]] = {}
live_detection_retry_after: dict[str, float] = {}
live_detection_failure_count: dict[str, int] = {}
active_rtsp_recordings: dict[str, dict[str, Any]] = {}
active_rtsp_recordings_lock = threading.Lock()
live_detection_worker_lock = threading.Lock()
active_live_detection_cameras: set[str] = set()
_frame_motion_prev: dict[str, Any] = {}
_frame_motion_lock = threading.Lock()

_MOTION_FRAME_W = 160
_MOTION_FRAME_H = 120
_MOTION_PIXEL_THRESHOLD = 30    # intensity change per pixel (0–255) to count as changed; 30 filters IR/night-vision sensor noise
_MOTION_GATE_FRACTION = 0.003   # 0.3% of pixels must change before any motion is reported
_MOTION_SCALE_FRACTION = 0.10   # 10% of pixels changed maps to confidence 1.0 (less sensitive than 5% for IR cameras)
_MOTION_BACKGROUND_ALPHA = 0.05 # background learning rate; stationary objects absorbed in ~30s
live_alert_monitor_stop = threading.Event()
live_alert_monitor_thread: threading.Thread | None = None


def _non_empty_setting(settings: dict[str, Any], key: str) -> str:
    return str(settings.get(key) or '').strip()


def build_stream_url(settings: dict[str, Any]) -> str:
    stream_url = _non_empty_setting(settings, 'stream_url')
    if stream_url:
        username = _non_empty_setting(settings, 'username')
        password = _non_empty_setting(settings, 'password')
        parsed = urlsplit(stream_url)
        if username and parsed.scheme in {'rtsp', 'rtsps'} and parsed.netloc and '@' not in parsed.netloc:
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
    port = int(settings.get('port') or 554)
    path = _non_empty_setting(settings, 'path') or 'stream1'
    path = path.lstrip('/')
    credentials = ''
    if username:
        credentials = quote(username, safe='')
        if password:
            credentials += f":{quote(password, safe='')}"
        credentials += '@'
    return f'rtsp://{credentials}{host}:{port}/{path}'


def camera_default_name(settings: dict[str, Any], fallback: str = 'Primary Camera') -> str:
    return str(settings.get('name') or settings.get('device') or fallback).strip() or fallback


def normalize_camera_id(value: Any, fallback: str = 'camera-1') -> str:
    camera_id = re.sub(r'[^a-zA-Z0-9_-]+', '-', str(value or '').strip().lower()).strip('-')
    return camera_id or fallback


def default_camera_detection_settings() -> dict[str, Any]:
    return {
        'motion_enabled': True,
        'motion_email_enabled': True,
        'object_detection_enabled': True,
        'zones': [],
    }


def default_camera_recording_settings() -> dict[str, Any]:
    return {
        'enabled': True,
        'record_on_alert': True,
        'continuous': False,
    }


def normalize_bool_setting(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


_LABEL_ALIASES: dict[str, str] = {
    'human': 'person',
    'people': 'person',
    'pedestrian': 'person',
}


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
        if recipient and '@' in recipient and recipient.lower() not in seen:
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
        rules.append({
            'label': label,
            'enabled': normalize_bool_setting(rule.get('enabled'), True),
            'record_on_detect': normalize_bool_setting(rule.get('record_on_detect'), True),
            'alert_on_detect': normalize_bool_setting(rule.get('alert_on_detect'), True),
            'min_confidence': max(0.0, min(1.0, min_confidence)),
            'cooldown_seconds': max(0, cooldown_seconds),
            'email_enabled': normalize_bool_setting(rule.get('email_enabled'), False),
            'email_recipients': normalize_email_recipients(rule.get('email_recipients', [])),
            'active_start': str(rule.get('active_start') or '').strip() or None,
            'active_end': str(rule.get('active_end') or '').strip() or None,
            'push_enabled': normalize_bool_setting(rule.get('push_enabled'), False),
        })
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
    recording = default_camera_recording_settings()
    if isinstance(settings, dict):
        recording.update(settings)
    recording['enabled'] = normalize_bool_setting(recording.get('enabled'), True)
    recording['record_on_alert'] = normalize_bool_setting(recording.get('record_on_alert'), True)
    recording['continuous'] = normalize_bool_setting(recording.get('continuous'), False)
    return recording


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
    return [
        {'x': round(x, 4), 'y': round(y, 4)},
        {'x': round(x + width, 4), 'y': round(y, 4)},
        {'x': round(x + width, 4), 'y': round(y + height, 4)},
        {'x': round(x, 4), 'y': round(y + height, 4)},
    ]


def zone_bounds(points: list[dict[str, float]]) -> tuple[float, float, float, float]:
    xs = [point['x'] for point in points]
    ys = [point['y'] for point in points]
    left = min(xs)
    top = min(ys)
    right = max(xs)
    bottom = max(ys)
    return left, top, max(0.01, right - left), max(0.01, bottom - top)


def normalize_monitoring_zones(zones: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(zones, list):
        return normalized
    for index, zone in enumerate(zones, start=1):
        if not isinstance(zone, dict):
            continue
        x = max(0.0, min(1.0, float(zone.get('x') or 0)))
        y = max(0.0, min(1.0, float(zone.get('y') or 0)))
        width = max(0.01, min(1.0 - x, float(zone.get('width') or 0)))
        height = max(0.01, min(1.0 - y, float(zone.get('height') or 0)))
        points = [point for point in (normalize_zone_point(point) for point in zone.get('points') or []) if point is not None]
        if len(points) < 3:
            points = rectangle_zone_points(x, y, width, height)
        x, y, width, height = zone_bounds(points)
        object_rules = normalize_zone_object_rules(zone)
        had_monitor_motion = bool(zone.get('monitor_motion', True))
        has_motion_rule = any(str(r.get('label') or '').strip().lower() == 'motion' for r in object_rules)
        if had_monitor_motion and not has_motion_rule:
            object_rules.insert(0, {
                'label': 'motion',
                'enabled': True,
                'record_on_detect': True,
                'alert_on_detect': True,
                'min_confidence': 0.45,
                'cooldown_seconds': 60,
                'email_enabled': False,
                'email_recipients': [],
                'push_enabled': False,
                'active_start': None,
                'active_end': None,
            })
        monitor_motion = any(
            str(r.get('label') or '').strip().lower() == 'motion' and r.get('enabled', True)
            for r in object_rules
        )
        normalized.append({
            'id': normalize_camera_id(zone.get('id'), f'zone-{index}'),
            'name': str(zone.get('name') or f'Zone {index}').strip() or f'Zone {index}',
            'x': round(x, 4),
            'y': round(y, 4),
            'width': round(width, 4),
            'height': round(height, 4),
            'points': points,
            'enabled': bool(zone.get('enabled', True)),
            'monitor_motion': monitor_motion,
            'monitor_objects': bool(zone.get('monitor_objects', True)),
            'object_labels': [rule['label'] for rule in object_rules if str(rule.get('label') or '').strip().lower() != 'motion'],
            'object_rules': object_rules,
        })
    return normalized


def normalize_camera_settings(settings: dict[str, Any], index: int = 1) -> dict[str, Any]:
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
    for key in ('motion_enabled', 'motion_email_enabled', 'object_detection_enabled'):
        detection[key] = bool(detection.get(key, True))
    detection['object_labels'] = normalize_label_list(detection.get('object_labels', []))
    detection['zones'] = normalize_monitoring_zones(detection.get('zones', []))
    camera_settings['detection'] = detection
    camera_settings['recording'] = normalize_camera_recording_settings(camera_settings.get('recording'))
    return camera_settings


def effective_cameras_config() -> list[dict[str, Any]]:
    override = database.get_setting('cameras')
    if isinstance(override, list) and override:
        return [normalize_camera_settings(camera_settings, index) for index, camera_settings in enumerate(override, start=1)]
    return []


def get_camera_config(camera_id: str | None = None) -> dict[str, Any]:
    if not cameras_config:
        return camera_config
    if camera_id:
        normalized = normalize_camera_id(camera_id)
        for configured in cameras_config:
            if configured.get('id') == normalized:
                return configured
        raise HTTPException(status_code=404, detail='Camera not found')
    return cameras_config[0]


def get_camera_instance(camera_id: str | None = None):
    configured = get_camera_config(camera_id)
    instance = camera_instances.get(str(configured['id']))
    if instance is None:
        raise HTTPException(status_code=404, detail='Camera not found')
    return instance


def detection_center_in_zone(detection: dict[str, Any], zone: dict[str, Any]) -> bool:
    box = detection.get('box') or {}
    center_x = float(box.get('x') or 0) + (float(box.get('width') or 0) / 2)
    center_y = float(box.get('y') or 0) + (float(box.get('height') or 0) / 2)
    points = zone.get('points') or []
    if isinstance(points, list) and len(points) >= 3:
        return point_in_polygon(center_x, center_y, points)
    return (
        float(zone['x']) <= center_x <= float(zone['x']) + float(zone['width'])
        and float(zone['y']) <= center_y <= float(zone['y']) + float(zone['height'])
    )


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


def detection_matches_zone(detection: dict[str, Any], zone: dict[str, Any], *, min_overlap_ratio: float = 0.2) -> bool:
    if detection_center_in_zone(detection, zone):
        return True
    # For polygon zones the center-in-polygon test is authoritative; bounding-rect
    # overlap would match objects outside the polygon but inside its bounding box.
    points = zone.get('points') or []
    if isinstance(points, list) and len(points) >= 3:
        return False
    # For rectangular zones fall back to bounding-rect overlap so large objects
    # straddling the edge are not silently dropped.
    return detection_overlap_ratio_with_zone_rect(detection, zone) >= min_overlap_ratio


def point_in_polygon(x: float, y: float, points: list[dict[str, Any]]) -> bool:
    inside = False
    vertex_count = len(points)
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
            slope_x = (previous_x - current_x) * (y - current_y) / ((previous_y - current_y) or 1e-12) + current_x
            if x < slope_x:
                inside = not inside
        previous = current
    return inside if vertex_count >= 3 else False


def point_on_segment(x: float, y: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    cross = (y - y1) * (x2 - x1) - (x - x1) * (y2 - y1)
    if abs(cross) > 1e-9:
        return False
    return min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9 and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9


def filter_detections_for_camera(detections: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    if not detection_settings.get('object_detection_enabled', True):
        return []
    return filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_objects')


def zone_motion_detections(detections: list[dict[str, Any]], settings: dict[str, Any], frame_motion_confidence: float = 0.5) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    if not detection_settings.get('motion_enabled', True):
        return []
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get('monitor_motion', True)]
    if not zones:
        return []
    seen_zones: set[str] = set()
    result: list[dict[str, Any]] = []
    for zone in zones:
        zone_id = str(zone.get('id') or zone.get('name') or id(zone))
        if zone_id in seen_zones:
            continue
        conf_threshold = zone_motion_min_confidence(zone)
        if frame_motion_confidence < conf_threshold:
            continue
        seen_zones.add(zone_id)
        # Use the zone's own bounding box so the overlay shows where the motion zone is,
        # not which YOLO object happens to be parked inside it.
        result.append({
            'confidence': frame_motion_confidence,
            'box': {
                'x': float(zone.get('x', 0)),
                'y': float(zone.get('y', 0)),
                'width': float(zone.get('width', 1)),
                'height': float(zone.get('height', 1)),
            },
        })
    return result


def detection_label_allowed_for_zone(detection: dict[str, Any], zone: dict[str, Any], camera_labels: set[str]) -> bool:
    zone_labels = set(normalize_label_list(zone.get('object_labels', [])))
    allowed_labels = zone_labels or camera_labels
    if not allowed_labels:
        return True
    label = str(detection.get('label') or '').strip().lower()
    return _LABEL_ALIASES.get(label, label) in allowed_labels


def filter_detections_for_camera_zones(detections: list[dict[str, Any]], settings: dict[str, Any], *, zone_monitor_key: str, require_zones: bool = False) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get(zone_monitor_key, True)]
    camera_labels = set(normalize_label_list(detection_settings.get('object_labels', [])))
    if not zones:
        if zone_monitor_key == 'monitor_objects' and camera_labels and not require_zones:
            return [detection for detection in detections if str(detection.get('label') or '').strip().lower() in camera_labels]
        return [] if require_zones else detections
    return [
        detection
        for detection in detections
        if any(
            detection_matches_zone(detection, zone)
            and (zone_monitor_key != 'monitor_objects' or detection_label_allowed_for_zone(detection, zone, camera_labels))
            for zone in zones
        )
    ]


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
        for rule in (zone.get('object_rules') or []):
            if not rule.get('enabled', True):
                continue
            if action == 'alert' and not rule.get('alert_on_detect', True):
                continue
            if action == 'record' and not rule.get('record_on_detect', True):
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
    for zone in zones:
        zone_id = str(zone.get('id') or zone.get('name') or 'zone')
        for rule in (zone.get('object_rules') or []):
            if not rule.get('enabled', True) or not rule.get('alert_on_detect', True):
                continue
            label = str(rule.get('label') or '').strip().lower()
            if not label:
                continue
            rules.append({
                'name': zone_rule_name(settings, zone, rule),
                'object': label,
                'zone_id': zone_id,
                'min_confidence': rule.get('min_confidence', 0.5),
                'cooldown_seconds': rule.get('cooldown_seconds', 60),
                'enabled': True,
                'email_enabled': bool(rule.get('email_enabled', False)),
                'email_recipients': normalize_email_recipients(rule.get('email_recipients', [])),
                'push_enabled': bool(rule.get('push_enabled', False)),
                'active_start': rule.get('active_start'),
                'active_end': rule.get('active_end'),
            })
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
            matched.append({
                **detection,
                'zone_id': zone_id,
                'zone_name': zone.get('name') or zone_id,
            })
    return matched


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
        for rule in (zone.get('object_rules') or []):
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
        normalized.append({
            **detection,
            'box': {
                'x': round(box_x / width, 4),
                'y': round(box_y / height, 4),
                'width': round(box_width / width, 4),
                'height': round(box_height / height, 4),
            },
        })
    return normalized


def update_live_detection_status(camera_id: str, **updates: Any) -> None:
    live_detection_status[camera_id] = {
        **live_detection_status.get(camera_id, {}),
        **updates,
        'camera_id': camera_id,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }


def record_live_detection_history(camera_id: str, detections: list[dict[str, Any]], sample_ts: float | None = None) -> None:
    """Append one monitor cycle's detections to the camera's rolling history.

    ``sample_ts`` must be when the analyzed frame was CAPTURED, not when
    inference finished: tracks sliced from this history are replayed against
    the recorded video, and stamping at completion shifts every box late by
    the inference duration — the playback overlay then trails moving objects.

    Empty cycles are recorded too: a recording track sliced from the history
    needs "nothing in frame" samples so playback overlays clear when an object
    leaves instead of holding the last box."""
    sample = [
        {'label': detection.get('label'), 'confidence': detection.get('confidence'), 'box': detection.get('box')}
        for detection in detections
        if isinstance(detection.get('box'), dict)
    ]
    if sample_ts is None:
        sample_ts = time.time()
    with live_detection_history_lock:
        history = live_detection_history.get(camera_id)
        if history is None:
            history_minutes = max(1, int(effective_live_config().get("detection_history_minutes", 10)))
            history_maxlen = max(120, history_minutes * 120)
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
    track = [
        {'t': round(sample_ts - start_ts, 3), 'detections': sample_detections}
        for sample_ts, sample_detections in samples
        if start_ts <= sample_ts <= end_ts
    ]
    return track or None


def detection_label_set(detections: list[dict[str, Any]]) -> set[str]:
    return {
        str(detection.get('label') or '').strip().lower()
        for detection in detections
        if str(detection.get('label') or '').strip()
    }


def detect_frame_motion(camera_id: str, image: Any) -> tuple[bool, float]:
    """Adaptive-background motion gate. Returns (has_motion, confidence 0-1).

    ``image`` may be a BGR numpy array (from ``read_frame``) or JPEG bytes
    (legacy callers).  When a numpy array is provided the PIL decode is
    skipped, saving ~5-15 ms per cycle.
    """
    try:
        import numpy as _np
        if hasattr(image, 'shape') and hasattr(image, 'dtype'):
            # Fast path: already a numpy BGR array — convert to grayscale
            # using OpenCV (faster than PIL for large frames).
            import cv2
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            resized = cv2.resize(gray, (_MOTION_FRAME_W, _MOTION_FRAME_H),
                                 interpolation=cv2.INTER_NEAREST)
            current = _np.array(resized, dtype=_np.float32)
        else:
            from PIL import Image as _Image
            img = _Image.open(io.BytesIO(image)).convert('L').resize(
                (_MOTION_FRAME_W, _MOTION_FRAME_H), _Image.NEAREST
            )
            current = _np.array(img, dtype=_np.float32)
        with _frame_motion_lock:
            background = _frame_motion_prev.get(camera_id)
            if background is None:
                _frame_motion_prev[camera_id] = current
                return False, 0.0
            # Update background model before comparing so the diff reflects
            # deviation from the learned scene, not just the previous frame.
            updated_bg = (1.0 - _MOTION_BACKGROUND_ALPHA) * background + _MOTION_BACKGROUND_ALPHA * current
            _frame_motion_prev[camera_id] = updated_bg
        changed_fraction = float(_np.mean(_np.abs(current - background) > _MOTION_PIXEL_THRESHOLD))
        if changed_fraction < _MOTION_GATE_FRACTION:
            return False, 0.0
        return True, round(min(1.0, changed_fraction / _MOTION_SCALE_FRACTION), 3)
    except Exception:
        return True, 0.4  # fail open: allow motion if comparison unavailable


def live_event_is_debounced(camera_id: str, labels: set[str], debounce_seconds: float) -> bool:
    if debounce_seconds <= 0 or not labels:
        return False
    previous = live_event_last_emitted.get(camera_id)
    if not previous:
        return False
    elapsed = time.time() - float(previous.get('timestamp', 0))
    if elapsed > debounce_seconds:
        return False
    # Generic motion right after any event on this camera is the trailing edge of
    # the same physical activity (e.g. the background model re-settling after an
    # object left), not a new occurrence — suppress it regardless of label overlap.
    if labels <= {'motion'}:
        return True
    previous_labels = {str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()}
    return bool(previous_labels & labels)


def remember_live_event(camera_id: str, labels: set[str], *, merge: bool = False) -> None:
    if not labels:
        return
    if merge:
        previous = live_event_last_emitted.get(camera_id) or {}
        labels = labels | {
            str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()
        }
    live_event_last_emitted[camera_id] = {
        'timestamp': time.time(),
        'labels': sorted(labels),
    }


def clear_live_camera_backoff(camera_id: str) -> None:
    live_detection_retry_after.pop(camera_id, None)
    live_detection_failure_count.pop(camera_id, None)


def extend_active_rtsp_recording(
    *,
    camera_id: str,
    event_time: str,
    recording_config: dict[str, Any] | None,
    detections: list[dict[str, Any]] | None = None,
) -> int | None:
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
        if should_record and trigger_label:
            current_recording = database.get_recording(recording_id) or {}
            current_label = str(current_recording.get('trigger_label') or '').strip().lower()
            current_type = str(current_recording.get('trigger_type') or '').strip().lower()
            generic_labels = {'', 'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous'}
            candidate_label = str(trigger_label).strip().lower()
            if (
                candidate_label not in generic_labels
                and (current_label in generic_labels or current_type in {'motion', 'human'})
            ):
                database.update_recording_trigger(
                    recording_id,
                    trigger_type='alert' if trigger_type in {'motion', 'human'} else trigger_type,
                    trigger_label=candidate_label,
                )
    return recording_id


def schedule_live_camera_backoff(camera_id: str, message: str) -> float:
    failure_count = live_detection_failure_count.get(camera_id, 0) + 1
    live_detection_failure_count[camera_id] = failure_count
    backoff_seconds = min(300.0, max(10.0, 5.0 * (2 ** min(failure_count - 1, 5))))
    retry_after = time.time() + backoff_seconds
    live_detection_retry_after[camera_id] = retry_after
    update_live_detection_status(
        camera_id,
        state='error',
        reason=f'{message} Retrying in {int(backoff_seconds)}s.',
        detections=[],
    )
    return backoff_seconds


def live_detection_status_payload(camera_id: str | None = None) -> dict[str, Any]:
    selected_config = get_camera_config(camera_id)
    # Fall back to the requested id so status written under that key (e.g. by
    # process_live_stream_alerts before any camera is persisted) is still found.
    camera_key = str(selected_config.get('id') or camera_id or 'camera')
    ai_state = ai_status_payload()
    return {
        'camera_id': camera_key,
        'camera_name': selected_config.get('name'),
        'ai_backend': ai_state['active_backend'],
        'ai_configured_backend': ai_state['configured_backend'],
        'ai_available': ai_state['inference_available'],
        'ai_mode': ai_state['mode'],
        'ai_error': ai_state['error'],
        **live_detection_status.get(camera_key, {'state': 'waiting', 'reason': 'No live detection has run yet.'}),
    }


def _camera_has_live_alert_stream(settings: dict[str, Any]) -> bool:
    return bool(build_stream_url(settings))


def run_live_alert_monitor_once() -> int:
    live_settings = effective_live_config()
    if not normalize_bool_setting(live_settings.get('background_detection_enabled'), True):
        return 0

    processed = 0
    for selected_config in list(cameras_config):
        camera_id = str(selected_config.get('id') or 'camera')
        if not _camera_has_live_alert_stream(selected_config):
            continue
        retry_after = live_detection_retry_after.get(camera_id, 0)
        now = time.time()
        if retry_after and now < retry_after:
            continue
        stream_url = build_stream_url(selected_config)
        cam_rec_config = camera_event_recording_config(selected_config)
        if stream_url:
            recording_service.prime_rtsp_prebuffer(
                stream_url=stream_url,
                camera_id=camera_id,
                recording_config=cam_rec_config,
            )
            if recording_service.mode_for(cam_rec_config) == 'continuous':
                recording_service.start_continuous_chunk_recording(
                    stream_url=stream_url,
                    camera_id=camera_id,
                    recording_config=cam_rec_config,
                    on_chunk_complete=_make_continuous_chunk_callback(camera_id),
                )
        detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.25))
        with live_detection_worker_lock:
            if camera_id in active_live_detection_cameras:
                continue
            if now - live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
                continue
            live_detection_last_checked[camera_id] = now
            active_live_detection_cameras.add(camera_id)

        def _detect_bg(cid: str = camera_id, cfg: dict[str, Any] = copy.deepcopy(selected_config)) -> None:
            try:
                cam = get_camera_instance(cid)
                if not hasattr(cam, 'read_frame'):
                    update_live_detection_status(cid, state='skipped', reason='Background alerts require a camera that can read frames.', detections=[])
                    return
                image, frame = cam.read_frame()
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


def live_alert_monitor_loop() -> None:
    while not live_alert_monitor_stop.is_set():
        run_live_alert_monitor_once()
        interval = max(0.1, float(effective_live_config().get('detection_interval_seconds', 0.25)))
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


def queue_live_stream_alerts(image_bytes: bytes, frame: dict[str, Any], settings: dict[str, Any]) -> None:
    camera_id = str(settings.get('id') or 'camera')
    stream_url = build_stream_url(settings)
    if stream_url:
        recording_service.prime_rtsp_prebuffer(
            stream_url=stream_url,
            camera_id=camera_id,
            recording_config=camera_event_recording_config(settings),
        )
    # Background monitor already performs periodic detection and event creation.
    # Skip snapshot-triggered detection in that mode to avoid duplicate alerts/recordings.
    if normalize_bool_setting(effective_live_config().get('background_detection_enabled'), True):
        return
    detection_interval_seconds = float(effective_live_config().get('detection_interval_seconds', 0.25))
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


def process_live_stream_alerts(image: Any, frame: dict[str, Any], settings: dict[str, Any], *, enforce_interval: bool = True) -> int | None:
    camera_id = str(settings.get('id') or 'camera')
    live_settings = effective_live_config()
    detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.25))
    if not hasattr(detector, 'detect_image'):
        update_live_detection_status(camera_id, state='skipped', reason='Live stream alerts require ONNX AI mode.', detections=[])
        return None

    if enforce_interval:
        now = time.time()
        if now - live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
            return None
        live_detection_last_checked[camera_id] = now

    ai_state = ai_status_payload()
    if not ai_state['detector_loaded']:
        update_live_detection_status(camera_id, state='skipped', reason=ai_state['last_detector_error'] or 'ONNX detector is not loaded.', ai=ai_state, detections=[])
        return None

    # Determine whether we received a numpy frame or JPEG bytes.
    frame_is_numpy = hasattr(image, 'shape') and hasattr(image, 'dtype')

    # Resolve the frame's capture time BEFORE running inference. The history
    # sample must be stamped with when the frame was captured — detect_image
    # can take hundreds of ms (seconds on SBC CPUs), and a completion-time
    # stamp makes every baked playback overlay lag the video by that much.
    now = time.time()
    try:
        frame_capture_ts = float(frame.get('timestamp') or 0.0)
    except (TypeError, ValueError):
        frame_capture_ts = 0.0
    if not (now - 300 <= frame_capture_ts <= now + 1):
        frame_capture_ts = now

    try:
        if frame_is_numpy and hasattr(detector, 'detect_frame'):
            detections = detector.detect_frame(image, confidence=compute_minimum_rule_confidence())
        else:
            detections = detector.detect_image(image, confidence=compute_minimum_rule_confidence())
    except (DetectorUnavailableError, ValueError) as exc:
        logger.warning('Live detection skipped for camera %s: %s', camera_id, exc)
        update_live_detection_status(camera_id, state='error', reason=str(exc), ai=ai_state, detections=[])
        return None

    detections = normalize_detection_boxes_for_frame(detections, frame)
    record_live_detection_history(camera_id, detections, sample_ts=frame_capture_ts)
    raw_labels = [str(detection.get('label')) for detection in detections if detection.get('label')]
    frame_has_motion, frame_motion_confidence = detect_frame_motion(camera_id, image)
    motion_detections = zone_motion_detections(detections, settings, frame_motion_confidence) if frame_has_motion else []
    object_detections = filter_detections_for_camera(detections, settings)
    zone_rules = zone_object_alert_rules(settings)
    object_alert_detections = zone_alert_detections(settings, object_detections) if zone_rules else list(object_detections)

    # Detections that match a zone recording rule but NOT an alert rule (alert_on_detect=False).
    # They must still produce an event and recording even though no alert notification fires.
    # Without this they hit the early-return below and are silently dropped whenever
    # another label has an alert rule (making zone_rules non-empty).
    record_only_detections = (
        [d for d in object_detections if zone_record_on_detect(d, settings) and not zone_object_rule_matches(settings, d, action='alert')]
        if zone_rules else []
    )

    alert_detections = list(object_alert_detections) + record_only_detections
    if motion_detections:
        strongest_motion = max(motion_detections, key=lambda detection: float(detection.get('confidence', 0)))
        alert_detections.append({
            **strongest_motion,
            'label': 'motion',
            'motion_event': True,
        })
    if not alert_detections:
        update_live_detection_status(
            camera_id,
            state='checked',
            reason='No detections matched this camera and its monitoring areas.',
            detected_labels=raw_labels,
            matched_labels=[],
            detections=[{**d, 'alert_matched': False, 'alert_triggered': False} for d in object_detections],
        )
        return None

    triggered = alerts.process(alert_detections, rules=zone_rules)
    triggered_rule_names = {str(alert.get('rule_name') or '') for alert in triggered}
    triggered_labels = {str(alert.get('label') or '').lower() for alert in triggered}
    recording_detections = [
        {
            **detection,
            'alert_matched': bool(zone_detection_alert_rule_names(settings, detection) & triggered_rule_names)
            if zone_rules else str(detection.get('label') or '').lower() in triggered_labels,
            'alert_triggered': zone_record_on_detect(detection, settings),
        }
        for detection in object_detections
    ]
    if motion_detections:
        _motion_record = zone_motion_record_on_detect(settings)
        recording_detections.append({
            **strongest_motion,
            'label': 'motion',
            'motion_event': True,
            'alert_matched': 'motion' in triggered_labels,
            'alert_triggered': 'motion' in triggered_labels or _motion_record
            or detection_has_matching_record_rule({**strongest_motion, 'label': 'motion'}, zone_rules),
        })
    matched_labels = [str(detection.get('label')) for detection in alert_detections if detection.get('label')]
    camera_recording_config = camera_event_recording_config(settings)
    should_record_event, _trigger_type, _trigger_label = recording_service.should_record(recording_detections, camera_recording_config)
    debounced_labels = detection_label_set([detection for detection in recording_detections if detection.get('alert_triggered')])
    if not debounced_labels:
        debounced_labels = detection_label_set(recording_detections)
    global_debounce = max(0.0, float(live_settings.get('event_debounce_seconds', 10.0)))
    label_cooldowns: dict[str, float] = {}
    for _zone in (settings.get('detection') or {}).get('zones', []):
        for _rule in (_zone.get('object_rules') or []):
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
    # Anchor event timing to when the analyzed frame was captured, not when
    # inference finished: on slow CPUs inference adds hundreds of ms and the
    # recording window (pre/post roll) would start that much late.
    frame_capture_time = datetime.fromtimestamp(frame_capture_ts, tz=timezone.utc).isoformat()
    if should_record_event and live_event_is_debounced(camera_id, debounced_labels, debounce_seconds):
        extended_recording_id = extend_active_rtsp_recording(
            camera_id=camera_id,
            event_time=frame_capture_time,
            recording_config=camera_recording_config,
            detections=recording_detections,
        )
        # Refresh the debounce window while the same activity continues, so a new
        # event/recording requires a quiet gap of debounce_seconds. Without this
        # the window was anchored to the original event and continuing or trailing
        # activity spawned a fresh event+recording every debounce_seconds.
        remember_live_event(camera_id, debounced_labels, merge=True)
        update_live_detection_status(
            camera_id,
            state='checked',
            reason=(
                f'Ongoing detection extended active recording and suppressed duplicate event for {debounce_seconds:.1f}s debounce window.'
                if extended_recording_id is not None
                else f'Ongoing detection suppressed for {debounce_seconds:.1f}s debounce window.'
            ),
            detected_labels=raw_labels,
            matched_labels=matched_labels,
            detections=recording_detections,
            recording_id=extended_recording_id,
        )
        return None

    event_time = frame_capture_time
    # Lazily encode to JPEG only when we need to save a snapshot (~5% of cycles).
    if frame_is_numpy:
        image_bytes = _encode_frame_jpeg(image)
    else:
        image_bytes = image
    snapshot_path = storage.save_image_snapshot(image_bytes, f'{camera_id}.jpg')
    event_id = database.add_event(
        created_at=event_time,
        source='rtsp',
        snapshot_path=snapshot_path,
        detections=recording_detections,
        alert_triggered=bool(triggered),
        metadata={
            'camera_id': settings.get('id'),
            'camera_name': settings.get('name'),
            'ai_backend': ai_state['configured_backend'],
            'detector_backend': ai_state['active_backend'],
            'source': 'live-stream',
        },
    )
    recording_id = attach_event_recording(
        event_id,
        event_time,
        'rtsp',
        recording_detections,
        camera_id=camera_id,
        recording_config=camera_recording_config,
    )
    if recording_id is not None:
        remember_live_event(camera_id, debounced_labels)

    for alert in triggered:
        database.add_alert(
            created_at=datetime.now(timezone.utc).isoformat(),
            rule_name=alert['rule_name'],
            event_id=event_id,
            label=alert['label'],
            confidence=alert['confidence'],
            message=alert['message'],
        )
    motion_email_enabled = normalize_bool_setting((settings.get('detection') or {}).get('motion_email_enabled'), True)
    email_triggered = [
        alert for alert in triggered
        if motion_email_enabled or str(alert.get('label') or '').strip().lower() != 'motion'
    ]
    # Deliver notifications off the detection thread: SMTP/ntfy calls block for
    # up to their 10s timeouts, and this thread holds the camera's detection
    # slot — a slow mail server would stall monitoring (and history sampling,
    # which playback overlays are sliced from) for that whole time.
    if email_triggered or triggered:
        notify_thread = threading.Thread(
            target=_deliver_alert_notifications,
            args=(email_triggered, triggered, event_id, zone_rules),
            name=f'alert-notify-{event_id}',
            daemon=True,
        )
        with _notification_threads_lock:
            _notification_threads[:] = [thread for thread in _notification_threads if thread.is_alive()]
            _notification_threads.append(notify_thread)
        notify_thread.start()
    email_rules = [
        rule for rule in zone_rules
        if rule.get('enabled', True) and rule.get('email_enabled') and str(rule.get('name') or '') in {
            str(alert.get('rule_name') or '') for alert in email_triggered
        }
    ]
    email_recipients = sorted({recipient for rule in email_rules for recipient in rule.get('email_recipients', [])})
    update_live_detection_status(
        camera_id,
        state='alerted' if triggered else 'checked',
        reason=(
            'Alert matched.' if triggered
            else 'Detections found. No new alert event was created because no alert rule matched, or a matching rule is still in cooldown.'
        ),
        detected_labels=raw_labels,
        matched_labels=matched_labels,
        detections=recording_detections,
        triggered_alerts=triggered,
        event_id=event_id,
        recording_id=recording_id,
        recording_state='linked' if recording_id is not None else 'skipped',
        recording_reason='Recording linked.' if recording_id is not None else recording_skip_reason(recording_detections, camera_event_recording_config(settings)),
        email_enabled_rules=len(email_rules),
        email_recipients=email_recipients,
        email_attempted=bool(email_triggered and email_recipients and effective_email_alert_settings().get('enabled')),
    )
    return event_id


def create_camera(settings: dict[str, Any]):
    width = int(settings.get('width', 1280))
    height = int(settings.get('height', 720))
    fps = int(settings.get('fps', 15))
    stale = settings.get("stale_frame_grabs")
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


def ai_status_payload(ai_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = ai_settings or effective_ai_config()
    active_backend = getattr(detector, 'backend', 'unknown')
    configured_backend = str(settings.get('backend', 'onnx')).lower()
    detector_loaded = detector_loaded_for(settings)
    model_loaded = bool(configured_backend == 'onnx' and active_backend == 'onnx' and getattr(detector, 'available', False))
    runtime_installed = onnx_runtime_installed()
    exists = model_exists(settings)
    detector_reason = getattr(detector, 'unavailable_reason', None)
    error = last_detector_error or detector_reason
    if configured_backend == 'onnx' and not exists:
        mode = 'MODEL MISSING'
        error = error or f"ONNX model not found: {settings.get('model_path')}"
    elif configured_backend == 'onnx' and not model_loaded:
        mode = 'MODEL FAILED'
    elif configured_backend == 'onnx':
        mode = 'ONNX ACTIVE'
        error = detector_reason
    else:
        mode = 'MODEL FAILED'
    inference_available = detector_loaded
    model_path_str = str(settings.get('model_path') or '')
    model_filename = Path(model_path_str).name if model_path_str else ''
    model_label = next(
        (info['label'] for info in YOLO_MODELS.values() if info['onnx'] == model_filename),
        None,
    )
    return {
        'current_backend': configured_backend,
        'active_backend': active_backend,
        'configured_backend': configured_backend,
        'mode': mode,
        'model_loaded': model_loaded,
        'detector_loaded': detector_loaded,
        'model_path': model_path_str,
        'model_name': model_label,
        'labels_path': str(settings.get('labels_path') or ''),
        'model_exists': exists,
        'onnx_runtime_installed': runtime_installed,
        'inference_available': inference_available,
        'error': error,
        'last_detector_error': error,
        'active_config_source': active_ai_config_source(),
    }


def log_detector_initialization(context: str = 'startup') -> None:
    ai_status = ai_status_payload()
    logger.info(
        'AI detector %s: active_backend=%s configured_backend=%s model_loaded=%s '
        'inference_available=%s model_path=%s labels_path=%s error=%s',
        context,
        ai_status['active_backend'],
        ai_status['configured_backend'],
        ai_status['model_loaded'],
        ai_status['inference_available'],
        ai_status['model_path'] or '<none>',
        ai_status['labels_path'] or '<none>',
        ai_status['error'] or '<none>',
    )

PUBLIC_PREFIXES = ('/static/',)
PUBLIC_PATHS = {'/favicon.ico', '/login', '/setup'}
ADMIN_PATHS = {'/ai', '/cameras', '/settings', '/users', '/zones', '/audit'}
MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


@app.middleware('http')
async def authentication_middleware(request: Request, call_next):
    if not auth_enabled:
        return await call_next(request)

    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
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

    admin_required = path in ADMIN_PATHS or path.startswith('/api/users') or path.startswith('/api/settings/ai') or path.startswith('/api/settings/system') or path.startswith('/api/update/') or (path.startswith('/api/cameras') and request.method in MUTATING_METHODS) or (
        path.startswith('/api/settings/alert-email') and request.method in MUTATING_METHODS
    ) or (
        path.startswith('/api/settings/alert-push') and request.method in MUTATING_METHODS
    )
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
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
        expires=expires_at,
        max_age=int(session_hours * 3600),
    )


def set_csrf_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=request.url.scheme == 'https', samesite='lax', max_age=3600)


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE)


async def form_data(request: Request) -> dict[str, str]:
    body = (await request.body()).decode('utf-8')
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}


def auth_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{escape(title)} · Daygle AI Camera</title><link rel="stylesheet" href="/static/styles.css" /></head>
<body><main class="auth-shell"><section class="card auth-card"><p class="eyebrow">Daygle AI Camera</p>{body}</section></main></body></html>""")


def csrf_token_response(request: Request, title: str, body_template: str, *, status_code: int = 200) -> HTMLResponse:
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
    # Only trust X-Forwarded-For when the direct connection comes from a
    # loopback address (i.e. a local reverse proxy like nginx). Accepting it
    # unconditionally lets any client spoof their recorded IP.
    if direct in _LOOPBACK:
        forwarded = request.headers.get('x-forwarded-for')
        if forwarded:
            return forwarded.split(',')[0].strip()
    return direct or 'unknown'


def write_audit_log(
    request: Request,
    action: str,
    resource: str,
    resource_id: Any = None,
    details: dict[str, Any] | None = None,
    status: str = 'success',
) -> None:
    user: dict[str, Any] | None = getattr(request.state, 'user', None)
    user_id: int | None = int(user['id']) if user else None
    username: str = str(user['username']) if user else 'anonymous'
    try:
        database.add_audit_log(
            created_at=utc_now(),
            user_id=user_id,
            username=username,
            action=action,
            resource=resource,
            resource_id=str(resource_id) if resource_id is not None else None,
            details=details,
            ip_address=_request_ip(request),
            status=status,
        )
    except Exception as exc:
        logger.warning('Failed to write audit log: %s', exc)


def _parse_chunk_start_time(file_path: Path) -> datetime | None:
    stem = file_path.stem  # e.g. 'continuous_camera-1_20260609T140000'
    parts = stem.rsplit('_', 1)
    if len(parts) != 2:
        return None
    try:
        # ffmpeg's segment muxer expands -strftime patterns with localtime(),
        # so the filename timestamp is in the server's local timezone. Parsing
        # it as UTC shifted every continuous recording (and its overlay track
        # window) by the UTC offset on non-UTC servers.
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
            recording_id = database.add_recording(
                event_id=None,
                camera_id=camera_id,
                started_at=started_at_dt.isoformat(),
                ended_at=ended_at_dt.isoformat(),
                duration_seconds=duration_seconds,
                file_path=str(file_path),
                thumbnail_path=None,
                source='rtsp',
                created_at=utc_now(),
                trigger_type='continuous',
                trigger_label=None,
            )
            write_live_history_detection_track(
                recording_id, file_path, camera_id, started_at_dt.timestamp(), ended_at_dt.timestamp(),
            )
            purge_recordings_by_policy()
        except Exception as exc:
            logger.warning('Failed to register continuous chunk %s for camera %s: %s', file_path.name, camera_id, exc)
    return on_chunk_complete


def attach_event_recording(
    event_id: int,
    event_time: str,
    source: str,
    detections: list[dict[str, Any]],
    camera_id: str | None = None,
    recording_config: dict[str, Any] | None = None,
) -> int | None:
    stream_url = ''
    if source == 'rtsp' and camera_id:
        stream_url = build_stream_url(get_camera_config(camera_id))
        extended_recording_id = extend_active_rtsp_recording(
            camera_id=camera_id,
            event_time=event_time,
            recording_config=recording_config,
            detections=detections,
        )
        if extended_recording_id is not None:
            return extended_recording_id
    metadata = recording_service.event_recording_metadata(event_id, event_time, source, detections, write_clip=not stream_url, recording_config=recording_config)
    if metadata is None:
        return None
    if camera_id:
        metadata['camera_id'] = camera_id
    recording_id = database.add_recording(created_at=utc_now(), **metadata)
    if stream_url:
        start_rtsp_recording_capture(
            stream_url,
            metadata,
            event_id,
            detections,
            recording_id=recording_id,
            camera_id=camera_id,
            event_time=event_time,
            recording_config=recording_config,
        )
    else:
        # The clip was written synchronously above; save the monitor's detections
        # over its window so playback overlays can follow objects for free.
        window = _recording_capture_window(metadata)
        if window:
            write_live_history_detection_track(
                recording_id, Path(str(metadata.get('file_path') or '')), camera_id, window[0], window[1],
            )
    purge_recordings_by_policy()
    return recording_id


def start_rtsp_recording_capture(
    stream_url: str,
    metadata: dict[str, Any],
    event_id: int,
    detections: list[dict[str, Any]],
    *,
    recording_id: int,
    camera_id: str | None = None,
    event_time: str | None = None,
    recording_config: dict[str, Any] | None = None,
) -> None:
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
    # Ongoing detections may extend the capture deadline up to the configured
    # clip ceiling — not just pre+post, which made extensions a no-op and forced
    # continuing activity to spill into brand-new recordings.
    max_clip_seconds = max(1, int((recording_config or effective_recording_config()).get('max_clip_seconds', 60)))
    max_deadline_ts = start_capture_ts + max(duration_seconds, float(max_clip_seconds))

    if camera_id:
        with active_rtsp_recordings_lock:
            active_rtsp_recordings[camera_id] = {
                'recording_id': recording_id,
                'start_capture_ts': start_capture_ts,
                'capture_deadline_ts': min(max_deadline_ts, initial_deadline_ts),
                'max_capture_deadline_ts': max_deadline_ts,
            }

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
            final_duration_seconds = max(1.0, final_deadline_ts - start_capture_ts)
            dynamic_post_seconds = max(0, int(round(final_deadline_ts - triggered_at.timestamp())))

            if camera_id and pre_seconds > 0:
                content_start_ts, content_seconds = recording_service.write_rtsp_clip_with_prebuffer(
                    stream_url=stream_url,
                    camera_id=camera_id,
                    file_path=file_path,
                    triggered_at=triggered_at,
                    pre_seconds=pre_seconds,
                    post_seconds=dynamic_post_seconds,
                    max_duration_seconds=final_duration_seconds,
                    buffer_seconds=recording_service.prebuffer_window_seconds(recording_config),
                )
            else:
                # Without a prebuffer the capture records live from this moment,
                # not from start_capture_ts; the stored window must say so.
                content_start_ts = time.time()
                recording_service.write_rtsp_clip(stream_url, file_path, final_duration_seconds)
                content_seconds = final_duration_seconds

            # Anchor the stored timing and the detection track to the window the
            # written media actually covers — keyframe-aligned prebuffer segments
            # and fallback captures rarely start exactly at start_capture_ts, and
            # any mismatch here shows up as overlay boxes drifting against the
            # video during playback.
            database.update_recording_timing(
                recording_id,
                started_at=datetime.fromtimestamp(content_start_ts, tz=timezone.utc).isoformat(),
                ended_at=datetime.fromtimestamp(content_start_ts + content_seconds, tz=timezone.utc).isoformat(),
                duration_seconds=content_seconds,
            )
            write_live_history_detection_track(
                recording_id, file_path, camera_id, content_start_ts, content_start_ts + content_seconds,
            )
        except Exception as exc:
            logger.warning('RTSP recording capture failed for event %s, writing generated fallback: %s', event_id, exc)
            write_generated_fallback()
            write_live_history_detection_track(
                recording_id, file_path, camera_id, start_capture_ts, start_capture_ts + duration_seconds,
            )
        finally:
            if camera_id:
                with active_rtsp_recordings_lock:
                    session = active_rtsp_recordings.get(camera_id)
                    if session and int(session.get('recording_id', -1)) == int(recording_id):
                        active_rtsp_recordings.pop(camera_id, None)

    threading.Thread(target=capture, name=f'rtsp-recording-{event_id}', daemon=True).start()


def recording_skip_reason(detections: list[dict[str, Any]], recording_config: dict[str, Any] | None = None) -> str:
    should_record, trigger_type, trigger_label = recording_service.should_record(detections, recording_config)
    if should_record:
        return f'Recording policy matched {trigger_type}{f" {trigger_label}" if trigger_label else ""}, but no recording was linked.'
    if not recording_service.enabled_for(recording_config):
        return 'Recording is disabled or recording mode is off.'
    if recording_config and recording_config.get('record_on_alert'):
        return 'Recording is waiting for an enabled alert rule to trigger for this camera.'
    labels = ', '.join(str(detection.get('label')) for detection in detections if detection.get('label')) or 'none'
    mode = recording_service.mode_for(recording_config)
    return f'Recording policy skipped this event. Detected labels: {labels}. Mode: {mode}.'


_notification_threads_lock = threading.Lock()
_notification_threads: list[threading.Thread] = []


def wait_for_pending_alert_notifications(timeout: float = 10.0) -> None:
    """Block until in-flight alert email/push deliveries finish (used by tests)."""
    deadline = time.time() + max(0.0, timeout)
    with _notification_threads_lock:
        pending = [thread for thread in _notification_threads if thread.is_alive()]
    for thread in pending:
        thread.join(timeout=max(0.0, deadline - time.time()))


def _deliver_alert_notifications(
    email_triggered: list[dict[str, Any]],
    triggered: list[dict[str, Any]],
    event_id: int,
    rules: list[dict[str, Any]] | None,
) -> None:
    try:
        deliver_email_alerts(email_triggered, event_id, rules=rules)
    except Exception as exc:
        logger.warning('Email alert delivery failed for event %s: %s', event_id, exc)
    try:
        deliver_push_notifications(triggered, event_id, rules=rules)
    except Exception as exc:
        logger.warning('Push notification delivery failed for event %s: %s', event_id, exc)


def deliver_email_alerts(triggered: list[dict[str, Any]], event_id: int, rules: list[dict[str, Any]] | None = None) -> None:
    if not triggered:
        return
    event = database.get_event(event_id) or {}
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    camera_name = str(metadata.get('camera_name') or '').strip() or None
    camera_id = str(metadata.get('camera_id') or '').strip() or None
    rules_by_name = {str(rule.get('name')): rule for rule in (rules or [])}

    any_email_enabled = any(
        rules_by_name.get(str(alert.get('rule_name')), {}).get('email_enabled')
        for alert in triggered
    )
    snapshot_bytes: bytes | None = None
    snapshot_path = str(event.get('snapshot_path') or '')
    if any_email_enabled and snapshot_path:
        try:
            snap_path = Path(snapshot_path)
            if snap_path.exists():
                raw_bytes = snap_path.read_bytes()
                db_detections = event.get('detections') or []
                overlay_detections = [
                    {
                        'label': d.get('label'),
                        'confidence': d.get('confidence'),
                        'box': {'x': d.get('x', 0), 'y': d.get('y', 0), 'width': d.get('width', 0), 'height': d.get('height', 0)},
                    }
                    for d in db_detections
                ]
                snapshot_bytes = render_live_snapshot_jpeg_overlay(raw_bytes, overlay_detections)
        except Exception as exc:
            logger.debug('Failed to annotate snapshot for email alert event %s: %s', event_id, exc)

    mailer = EmailAlertService(effective_email_alert_settings())
    for alert in triggered:
        rule = rules_by_name.get(str(alert.get('rule_name')))
        if not rule or not rule.get('email_enabled'):
            continue
        try:
            mailer.send_alert(
                alert,
                event_id=event_id,
                recipients=rule.get('email_recipients', []),
                camera_name=camera_name,
                camera_id=camera_id,
                snapshot_bytes=snapshot_bytes,
            )
        except EmailAlertError as exc:
            logger.warning('Failed to send email alert for event %s rule %s: %s', event_id, alert.get('rule_name'), exc)


def deliver_push_notifications(triggered: list[dict[str, Any]], event_id: int, rules: list[dict[str, Any]] | None = None) -> None:
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
    rules_by_name = {str(rule.get('name')): rule for rule in (rules or [])}
    notifier = PushNotificationService(push_settings)
    for alert in triggered:
        rule_name = str(alert.get('rule_name') or '')
        rule = rules_by_name.get(rule_name)
        if not rule:
            logger.debug('Push skipped for event %s: no rule found for %r', event_id, rule_name)
            continue
        if not rule.get('push_enabled'):
            logger.debug('Push skipped for event %s rule %r: push_enabled is False', event_id, rule_name)
            continue
        try:
            notifier.send_alert(alert, event_id=event_id, camera_name=camera_name, camera_id=camera_id)
            logger.info('Push notification sent for event %s rule %r', event_id, rule_name)
        except PushNotificationError as exc:
            logger.error('Failed to send push notification for event %s rule %r: %s', event_id, rule_name, exc)


GITHUB_REPO = 'daygle/daygle-ai-camera'
MODELS_MANIFEST_URL = f'https://raw.githubusercontent.com/{GITHUB_REPO}/main/models-manifest.json'
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
        except Exception:
            return {}
    return {}


def _write_installed_models(data: dict[str, Any]) -> None:
    p = _installed_models_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding='utf-8')


def _sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _fetch_models_manifest() -> dict[str, Any]:
    req = urllib.request.Request(
        MODELS_MANIFEST_URL,
        headers={'User-Agent': 'daygle-ai-camera-updater/1.0'},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def render_live_snapshot_svg(
    frame: dict[str, Any],
    detections: list[dict[str, Any]],
    *,
    overlay: bool,
    camera_name: str = 'Camera',
    zones: list[dict[str, Any]] | None = None,
) -> str:
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
            points = zone.get('points') or rectangle_zone_points(
                max(0.0, min(1.0, float(zone.get('x') or 0))),
                max(0.0, min(1.0, float(zone.get('y') or 0))),
                max(0.01, min(1.0, float(zone.get('width') or 0))),
                max(0.01, min(1.0, float(zone.get('height') or 0))),
            )
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
            zone_markup.append(
                f'<g class="monitor-zone"><polygon points="{" ".join(svg_points)}" />'
                f'<text x="{label_x:.1f}" y="{label_y:.1f}">{zone_name}</text></g>'
            )

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
            detection_markup.append(
                f'<g class="detection-box"><rect x="{x:.1f}" y="{y:.1f}" width="{box_width:.1f}" height="{box_height:.1f}" />'
                f'<text x="{x:.1f}" y="{label_y:.1f}">{label} · {confidence}%</text></g>'
            )

    overlay_state = 'ON' if overlay else 'OFF'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <linearGradient id="camera-bg" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0" stop-color="#101827" />
      <stop offset="0.52" stop-color="#0b1220" />
      <stop offset="1" stop-color="#17223a" />
    </linearGradient>
    <radialGradient id="lens" cx="50%" cy="45%" r="68%">
      <stop offset="0" stop-color="#47d6ff" stop-opacity="0.22" />
      <stop offset="0.5" stop-color="#8b5cf6" stop-opacity="0.1" />
      <stop offset="1" stop-color="#070b13" stop-opacity="0" />
    </radialGradient>
    <style>
      .grid line {{ stroke: rgba(255,255,255,.08); stroke-width: 1; }}
      .hud {{ fill: #edf3ff; font: 700 26px Inter, Arial, sans-serif; letter-spacing: .04em; }}
      .muted {{ fill: #91a1ba; font: 700 20px Inter, Arial, sans-serif; }}
      .monitor-zone polygon {{ fill: rgba(71,214,255,.08); stroke: #47d6ff; stroke-width: 3; stroke-dasharray: 12 10; }}
      .monitor-zone text {{ fill: #47d6ff; font: 800 20px Inter, Arial, sans-serif; paint-order: stroke; stroke: rgba(7,11,19,.86); stroke-width: 4; stroke-linejoin: round; }}
      .detection-box rect {{ fill: rgba(73,230,163,.08); stroke: #49e6a3; stroke-width: 4; rx: 18; }}
      .detection-box text {{ fill: #49e6a3; font: 800 24px Inter, Arial, sans-serif; paint-order: stroke; stroke: rgba(7,11,19,.86); stroke-width: 5; stroke-linejoin: round; }}
    </style>
  </defs>
  <rect width="100%" height="100%" fill="url(#camera-bg)" />
  <rect width="100%" height="100%" fill="url(#lens)" />
  <g class="grid">{''.join(grid_lines)}</g>
  <circle cx="{width * .74:.1f}" cy="{height * .34:.1f}" r="{min(width, height) * .16:.1f}" fill="none" stroke="rgba(71,214,255,.16)" stroke-width="3" />
  <circle cx="{width * .28:.1f}" cy="{height * .62:.1f}" r="{min(width, height) * .12:.1f}" fill="none" stroke="rgba(139,92,246,.16)" stroke-width="3" />
  {''.join(zone_markup)}
  {''.join(detection_markup)}
  <rect x="24" y="24" width="520" height="116" rx="20" fill="rgba(7,11,19,.58)" stroke="rgba(255,255,255,.12)" />
  <text x="48" y="70" class="hud">{escape(camera_name).upper()}</text>
  <text x="48" y="112" class="muted">Frame #{frame_number} · {timestamp} · Overlay {overlay_state}</text>
</svg>'''


def render_live_snapshot_jpeg_overlay(image_bytes: bytes, detections: list[dict[str, Any]]) -> bytes:
    if not detections:
        return image_bytes
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
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
            playback_paths = [
                recording_playback_sidecar_path(file_path),
                recording_track_sidecar_path(file_path),
                file_path.with_name(f'{file_path.stem}.playback.failed'),
                file_path.with_name(f'{file_path.stem}.browser.mp4'),
                file_path.with_name(f'{file_path.stem}.playback.mp4'),
            ]
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
    return file_path.with_name(f'{file_path.stem}.h264.mp4')


def recording_stream_path(file_path: Path) -> Path:
    playback_path = recording_playback_sidecar_path(file_path)
    if playback_path.exists() and file_path.exists() and playback_path.stat().st_mtime >= file_path.stat().st_mtime:
        return playback_path
    # Event clips and prebuffer renders are already written as H.264/faststart
    # MP4 — serve them directly. Re-encoding those again doubled storage and
    # delayed first playback by the whole transcode.
    if file_path.suffix.lower() == '.mp4' and probe_video_codec(file_path) == 'h264':
        return file_path
    # If a previous transcode attempt failed and the source hasn't changed
    # since, skip retrying — every browser range request would otherwise
    # re-run a doomed ffmpeg process and flood the logs.
    failed_marker = file_path.with_name(f'{file_path.stem}.playback.failed')
    if (
        failed_marker.exists()
        and file_path.exists()
        and failed_marker.stat().st_mtime >= file_path.stat().st_mtime
    ):
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
    # An all-empty track is just a "nothing was localized" marker; report it as
    # missing so playback falls back to the static event box.
    if not any(isinstance(sample, dict) and sample.get('detections') for sample in data):
        return None
    return data


def write_live_history_detection_track(
    recording_id: int | None,
    file_path: Path,
    camera_id: str | None,
    start_ts: float,
    end_ts: float,
) -> bool:
    """Persist the monitor's detections over the capture window as the clip's track.

    This replaces the old post-recording "bake" that re-decoded the clip and ran
    detection on every sampled frame: the background monitor already analyzed
    these frames live, so slicing its history costs nothing. An all-empty slice
    is still written — it marks the clip as analyzed while the loader keeps
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
    localized = sum(1 for sample in track if sample.get('detections'))
    logger.info(
        'Saved detection track for recording %s from live history (%d samples, %d with detections).',
        recording_id, len(track), localized,
    )
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
    return start_ts, end_ts


def probe_video_codec(file_path: Path) -> str | None:
    """Return the first video stream's codec name (e.g. 'h264', 'hevc'), or None."""
    if not file_path.exists() or file_path.stat().st_size <= 0:
        return None
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return None
    command = [
        ffprobe,
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=codec_name',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(file_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    codec = (result.stdout or '').strip().lower()
    return codec or None if result.returncode == 0 else None


def probe_video_duration(file_path: Path) -> float | None:
    ffprobe = shutil.which('ffprobe')
    if not ffprobe or not file_path.exists():
        return None
    command = [
        ffprobe,
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(file_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        return float((result.stdout or '').strip()) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def transcode_recording_to_mp4(source_path: Path, output_path: Path) -> None:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required to convert recordings for browser playback.')
    tmp_path = output_path.with_name(f'{output_path.stem}.tmp{output_path.suffix}')
    if tmp_path.exists():
        tmp_path.unlink(missing_ok=True)
    command = [
        ffmpeg,
        '-y',
        '-fflags',
        '+discardcorrupt',
        '-err_detect',
        'ignore_err',
        '-i',
        str(source_path),
        '-map',
        '0:v:0',
        '-an',
        '-c:v',
        'libx264',
        '-preset',
        'veryfast',
        '-profile:v',
        'main',
        '-level',
        '4.0',
        '-pix_fmt',
        'yuv420p',
        '-movflags',
        '+faststart',
        str(tmp_path),
    ]
    # Long continuous chunks (up to an hour) can take far longer than the old
    # fixed 120s ceiling to re-encode on low-power hardware; scale the timeout
    # with the source duration so they convert instead of erroring out.
    duration = probe_video_duration(source_path) or 0.0
    timeout_seconds = max(120, int(duration * 3) + 60)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    if not tmp_path.exists():
        raise RuntimeError('MP4 conversion did not create an output file.')
    if result.returncode != 0 and not mp4_has_video_stream(tmp_path):
        tmp_path.unlink(missing_ok=True)
        error_detail = f'{result.stderr[:500]}\n...\n{result.stderr[-1000:]}'
        raise RuntimeError(f'ffmpeg failed to convert recording for browser playback: {error_detail}')
    if not mp4_has_video_stream(tmp_path):
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError('Converted MP4 does not contain a video stream.')
    tmp_path.replace(output_path)


def mp4_has_video_stream(file_path: Path) -> bool:
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return file_path.exists() and file_path.stat().st_size > 0
    command = [
        ffprobe,
        '-v',
        'error',
        '-select_streams',
        'v:0',
        '-show_entries',
        'stream=codec_name',
        '-of',
        'default=noprint_wrappers=1:nokey=1',
        str(file_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    return result.returncode == 0 and bool((result.stdout or '').strip())


DATABASE_RESTORE_REQUIRED_TABLES = {'events', 'detections', 'app_settings', 'users'}


def backup_directory() -> Path:
    backups_dir = Path(str(effective_storage_config().get('data_dir') or 'data')) / 'backups'
    backups_dir.mkdir(parents=True, exist_ok=True)
    return backups_dir


def safe_backup_timestamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def create_database_backup(prefix: str = 'daygle-database') -> Path:
    backup_path = backup_directory() / f'{prefix}-{safe_backup_timestamp()}-{secrets.token_hex(4)}.sqlite3'
    source = sqlite3.connect(database.database_path)
    try:
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return backup_path


def validate_restore_database(path: Path) -> None:
    try:
        with sqlite3.connect(path) as db:
            integrity = db.execute('PRAGMA integrity_check').fetchone()
            if not integrity or str(integrity[0]).lower() != 'ok':
                raise HTTPException(status_code=400, detail='Uploaded database failed SQLite integrity check.')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
            missing = sorted(DATABASE_RESTORE_REQUIRED_TABLES - tables)
            if missing:
                raise HTTPException(status_code=400, detail=f'Uploaded database is missing required table(s): {", ".join(missing)}.')
            admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()[0]
            if int(admin_count) < 1:
                raise HTTPException(status_code=400, detail='Uploaded database must include at least one active administrator account.')
    except sqlite3.DatabaseError as exc:
        raise HTTPException(status_code=400, detail='Uploaded file is not a valid SQLite database.') from exc


def refresh_runtime_after_database_restore() -> None:
    database.init()
    auth.init()
    apply_cameras_settings(effective_cameras_config())
    apply_storage_and_recording_settings()
    auth.apply_config(effective_auth_config())


def purge_recordings_by_policy(*, force: bool = False) -> dict[str, Any]:
    recording_settings = effective_recording_config()
    if not force and not _bool_value(recording_settings.get('auto_purge_enabled', True)):
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


@app.get('/login')
def login_page(request: Request, error: str | None = None):
    if auth_enabled and auth.users_exist() and auth.get_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return RedirectResponse('/', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return csrf_token_response(request, 'Login', f"""
<h1>Sign In</h1><p class="muted">Enter your Daygle AI Camera credentials.</p>{error_html}
<form class="form-stack" method="post" action="/login">
  <input type="hidden" name="csrf_token" value="{{csrf}}" />
  <label>Username<input name="username" autocomplete="username" required /></label>
  <label>Password<input name="password" type="password" autocomplete="current-password" required /></label>
  <button class="primary" type="submit">Sign In</button>
</form>""")


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
        except Exception:
            pass
        return login_page(request, str(exc))
    try:
        database.add_audit_log(created_at=utc_now(), user_id=int(_user['id']), username=str(_user['username']), action='login', resource='session', ip_address=ip, status='success')
    except Exception:
        pass
    response = RedirectResponse('/', status_code=303)
    set_session_cookie(response, request, token, expires_at)
    response.delete_cookie(CSRF_COOKIE)
    return response


@app.get('/setup')
def setup_page(request: Request, error: str | None = None):
    if auth_enabled and auth.users_exist():
        return RedirectResponse('/login', status_code=303)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ''
    return csrf_token_response(request, 'Initial setup', f"""
<h1>Create administrator</h1><p class="muted">This one-time setup is disabled after the first user is created.</p>{error_html}
<form class="form-stack" method="post" action="/setup">
  <input type="hidden" name="csrf_token" value="{{csrf}}" />
  <label>Username<input name="username" value="admin" autocomplete="username" required /></label>
  <label>Password<input name="password" type="password" autocomplete="new-password" required /></label>
  <label>Confirm password<input name="confirm_password" type="password" autocomplete="new-password" required /></label>
  <button class="primary" type="submit">Create Admin Account</button>
</form>""")


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
        auth.create_user(data.get('username', ''), data.get('password', ''), role='admin')
    except AuthError as exc:
        return setup_page(request, str(exc))
    return RedirectResponse('/login', status_code=303)


@app.get('/logout')
def logout_get(request: Request):
    # GET logout does not delete the session to avoid CSRF via link tricks.
    # The nav uses a JS-driven POST with a CSRF token instead.
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
        return body, None, content_type

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
        return payload.rstrip(b'\r\n-'), filename, uploaded_type

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


@app.get('/ai')
def ai_settings_page():
    ai_path = web_dir / 'ai.html'
    if ai_path.exists():
        return FileResponse(ai_path)
    return root()


@app.get('/profile')
def profile_page():
    profile_path = web_dir / 'profile.html'
    if profile_path.exists():
        return FileResponse(profile_path)
    return root()


def _settings_section_update() -> str:
    version_file = BASE_DIR / 'VERSION'
    current_version = version_file.read_text(encoding='utf-8').strip() if version_file.exists() else 'unknown'
    return (
        f'<section class="card" id="updateSection">'
        f'<div class="settings-section-header"><div class="settings-section-icon">🔄</div><div><h2>Software Updates</h2><p class="settings-section-subtitle">Current version: <strong id="currentVersion">{escape(current_version)}</strong>. Check for and apply updates from GitHub.</p></div></div>'
        f'<div id="updateStatus" class="status-panel" style="display:none"></div>'
        f'<div class="button-row">'
        f'<button id="checkUpdateBtn" type="button">Check for Updates</button>'
        f'<button id="applyUpdateBtn" class="secondary" type="button" style="display:none">Apply Update</button>'
        f'</div>'
        f'<pre id="updateOutput" class="update-output" style="display:none"></pre>'
        f'</section>'
    )


def _settings_section_recording() -> str:
    return (
        '<section class="card">'
        '<div class="settings-section-header"><div class="settings-section-icon">🎬</div><div><h2>Recording Clips</h2><p class="settings-section-subtitle">Control how event recordings are captured. Per-camera recording toggles are on the Live Cameras page.</p></div></div>'
        '<form id="recordingSettingsForm" class="form-grid">'
        '<label><span>Pre-Event Seconds</span><input name="pre_event_seconds" type="number" min="0" max="300" placeholder="10" /><span class="field-help">Seconds of footage to include before the trigger event. Default: 10s</span></label>'
        '<label><span>Post-Event Seconds</span><input name="post_event_seconds" type="number" min="0" max="300" placeholder="15" /><span class="field-help">Seconds to continue recording after the last detection. Default: 15s</span></label>'
        '<label><span>Extend On Motion (s)</span><input name="extension_step_seconds" type="number" min="0" max="300" placeholder="10" /><span class="field-help">Each time motion continues, the recording is extended by this many seconds. Default: 45s</span></label>'
        '<label><span>Max Clip Duration (s)</span><input name="max_clip_seconds" type="number" min="1" max="3600" placeholder="300" /><span class="field-help">Maximum total clip length. Prevents extremely long recordings. Default: 300s</span></label>'
        '<label><span>Format</span><input name="format" placeholder="mp4" /><span class="field-help">Video container format. mp4 is recommended for best compatibility. Default: mp4</span></label>'
        '</form>'
        '<div class="button-row"><button type="submit" form="recordingSettingsForm">Save Clip Settings</button></div>'
        '</section>'
    )


def _settings_section_retention() -> str:
    return (
        '<section class="card">'
        '<div class="settings-section-header"><div class="settings-section-icon">🧹</div><div><h2>Retention</h2><p class="settings-section-subtitle">Automatically clean up old recordings and events to manage disk usage.</p></div></div>'
        '<form id="retentionSettingsForm" class="form-grid">'
        '<label><span>Auto Purge</span><select name="auto_purge_enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select><span class="field-help">Automatically delete recordings and events that exceed retention limits. Default: Enabled</span></label>'
        '<label><span>Retention Days</span><input name="retention_days" type="number" min="1" max="3650" placeholder="30" /><span class="field-help">Delete recordings older than this many days. Default: 14 days</span></label>'
        '<label><span>Max Storage (GB)</span><input name="max_storage_gb" type="number" min="1" max="100000" placeholder="50" /><span class="field-help">Oldest recordings are deleted first when this limit is reached. Default: 20 GB</span></label>'
        '</form>'
        '<div class="button-row"><button type="submit" form="retentionSettingsForm">Save Retention</button><button id="purgeRecordingsBtn" class="secondary" type="button">Run Purge Now</button></div>'
        '</section>'
    )


def _settings_section_storage() -> str:
    return (
        '<section class="card">'
        '<div class="settings-section-header"><div class="settings-section-icon">📁</div><div><h2>Storage</h2><p class="settings-section-subtitle">Configure where Daygle stores data on disk. Changes take effect after saving.</p></div></div>'
        '<form id="storageSettingsForm" class="form-grid">'
        '<label><span>Data Directory</span><input name="data_dir" placeholder="/opt/daygle/data" /><span class="field-help">Root directory for all application data. Default: data</span></label>'
        '<label><span>Snapshots Directory</span><input name="snapshots_dir" placeholder="/opt/daygle/data/snapshots" /><span class="field-help">Where event snapshot images are saved. Default: data/snapshots</span></label>'
        '<label><span>Events Directory</span><input name="events_dir" placeholder="/opt/daygle/data/events" /><span class="field-help">Where event clip videos are saved. Default: data/events</span></label>'
        '<label><span>Recordings Directory</span><input name="recordings_dir" placeholder="/opt/daygle/data/recordings" /><span class="field-help">Where continuous recordings are saved. Default: data/recordings</span></label>'
        '</form>'
        '<div class="button-row"><button type="submit" form="storageSettingsForm">Save Storage</button></div>'
        '</section>'
    )


def _settings_section_auth() -> str:
    return (
        '<section class="card">'
        '<div class="settings-section-header"><div class="settings-section-icon">🔒</div><div><h2>Login Security</h2><p class="settings-section-subtitle">Protect against brute-force attacks and manage session duration.</p></div></div>'
        '<form id="authSettingsForm" class="form-grid">'
        '<label><span>Session Timeout (hours)</span><input name="session_timeout_hours" type="number" min="0.25" max="720" step="0.25" placeholder="12" /><span class="field-help">How long a user stays logged in before re-authentication is required. Default: 12 hours</span></label>'
        '<label><span>Max Login Attempts</span><input name="max_login_attempts" type="number" min="1" max="100" placeholder="5" /><span class="field-help">Number of failed attempts before the account is temporarily locked. Default: 5</span></label>'
        '<label><span>Lockout Minutes</span><input name="lockout_minutes" type="number" min="1" max="1440" placeholder="15" /><span class="field-help">How long the account is locked after exceeding max login attempts. Default: 15 minutes</span></label>'
        '</form>'
        '<div class="button-row"><button type="submit" form="authSettingsForm">Save Login Security</button></div>'
        '</section>'
    )


@app.get('/settings')
def system_settings_page():
    sections = ''.join([
        _settings_section_update(),
        _settings_section_recording(),
        _settings_section_retention(),
        _settings_section_storage(),
        _settings_section_auth(),
    ])
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Settings - Daygle AI Camera</title>'
        '<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell page-stack">'
        '<header class="hero"><div><p class="eyebrow">Administration</p><h1>Settings</h1>'
        '<p class="muted">Move day-to-day camera, recording, storage, and login settings out of YAML.</p></div></header>'
        '<div id="systemMessage" class="muted"></div>'
        f'{sections}'
        '</main><script src="/static/nav.js"></script><script src="/static/utils.js"></script><script src="/static/settings.js"></script></body></html>'
    )
    return HTMLResponse(html)

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


@app.put('/api/profile')
async def update_profile(request: Request):
    user = require_user(request)
    payload = await request.json()
    try:
        updated = auth.update_profile(
            int(user['id']),
            username=payload.get('username'),
            first_name=payload.get('first_name'),
            last_name=payload.get('last_name'),
            email=payload.get('email'),
            timezone_name=payload.get('timezone'),
            date_format=payload.get('date_format'),
            time_format=payload.get('time_format'),
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.state.user = updated
    return updated


@app.post('/api/profile/password')
async def change_profile_password(request: Request):
    user = require_user(request)
    payload = await request.json()
    try:
        auth.change_password(int(user['id']), str(payload.get('current_password') or ''), str(payload.get('new_password') or ''))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True}


@app.get('/api/status')
def status(camera_id: str | None = None):
    if not cameras_config:
        # Clean install: no cameras configured yet, but the app itself is up.
        ai_state = ai_status_payload()
        return {
            'status': 'online',
            'mode': None,
            'camera_id': None,
            'camera_name': None,
            'camera_detection': {},
            'ai_backend': ai_state['active_backend'],
            'ai_available': ai_state['inference_available'],
            'ai_error': ai_state['error'],
            'ai_mode': ai_state['mode'],
            'live_detection': live_detection_status_payload(camera_id),
            'frame_number': 0,
            'uptime_seconds': 0,
            'resolution': {'width': 0, 'height': 0},
        }
    selected_camera = get_camera_instance(camera_id)
    selected_config = get_camera_config(camera_id)
    frame = selected_camera.get_frame()
    ai_state = ai_status_payload()
    return {
        'status': 'online',
        'mode': selected_config.get('backend', 'onvif'),
        'camera_id': selected_config.get('id'),
        'camera_name': selected_config.get('name'),
        'camera_detection': selected_config.get('detection', {}),
        'ai_backend': ai_state['active_backend'],
        'ai_available': ai_state['inference_available'],
        'ai_error': ai_state['error'],
        'ai_mode': ai_state['mode'],
        'live_detection': live_detection_status_payload(camera_id),
        'frame_number': frame['frame_number'],
        'uptime_seconds': frame['uptime_seconds'],
        'resolution': {'width': frame['width'], 'height': frame['height']},
    }


@app.get('/api/status/ai')
def ai_status():
    return ai_status_payload()


@app.get('/api/live/detection-status')
def live_detection_status_api(camera_id: str | None = None):
    return live_detection_status_payload(camera_id)


@app.get('/api/live/snapshot')
def live_snapshot(camera_id: str | None = None):
    # Snapshots are served exactly as read from the camera. Detection boxes are
    # drawn client-side on a canvas from /api/live/detection-status data, so no
    # per-request JPEG decode/draw/re-encode happens on the server.
    selected_camera = get_camera_instance(camera_id)
    selected_config = get_camera_config(camera_id)
    if hasattr(selected_camera, 'read_jpeg'):
        try:
            image_bytes, frame = selected_camera.read_jpeg()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        queue_live_stream_alerts(image_bytes, frame, copy.deepcopy(selected_config))
        return Response(content=image_bytes, media_type='image/jpeg')
    raise HTTPException(status_code=503, detail='Live snapshots require an ONVIF/RTSP camera backend.')


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

    return {
        'detections': detections,
        'count': len(detections),
        'ai_backend': ai_state['active_backend'],
        'ai_error': ai_error,
    }


@app.get('/api/events')
def events(label: str | None = None, limit: int = Query(50, ge=1, le=200), alerted_only: bool = False, with_recording: bool = False):
    return database.search_events(label=label, limit=limit, alerted_only=alerted_only, with_recording=with_recording)


@app.get('/api/events/{event_id}')
def event_detail(event_id: int):
    event = database.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    return event


@app.delete('/api/events/{event_id}')
def delete_event(event_id: int, request: Request):
    require_admin(request)
    event = database.delete_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    snapshot_path = event.get('snapshot_path')
    if snapshot_path:
        snapshot = Path(snapshot_path)
        if snapshot.exists() and snapshot.is_file():
            snapshot.unlink(missing_ok=True)
    write_audit_log(request, 'delete', 'event', event_id)
    return {'ok': True}


@app.delete('/api/events')
def delete_all_events(request: Request):
    require_admin(request)
    deleted = database.delete_all_events()
    write_audit_log(request, 'delete_all', 'events', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}


@app.get('/api/alerts')
def alert_history(limit: int = Query(25, ge=1, le=200)):
    return database.alerts(limit=limit)


@app.delete('/api/alerts')
def delete_all_alert_history(request: Request):
    require_admin(request)
    deleted = database.delete_all_alerts()
    write_audit_log(request, 'delete_all', 'alert_history', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}


@app.get('/api/stats')
def stats():
    return database.stats()


@app.delete('/api/objects')
def delete_all_objects(request: Request):
    require_admin(request)
    deleted = database.delete_all_objects()
    return {'ok': True, 'deleted': deleted}


@app.get('/api/config')
def runtime_config():
    ai_state = ai_status_payload()
    return {
        'server': {'host': config.get('server', {}).get('host'), 'port': config.get('server', {}).get('port')},
        'camera': get_camera_config(None),
        'cameras': effective_cameras_config(),
        'ai': {
            'enabled': effective_ai_config().get('enabled'),
            'backend': effective_ai_config().get('backend'),
            'confidence': effective_ai_config().get('confidence'),
            'iou_threshold': effective_ai_config().get('iou_threshold'),
            'input_size': effective_ai_config().get('input_size'),
            'model_path': effective_ai_config().get('model_path'),
            'labels_path': effective_ai_config().get('labels_path'),
            'active_backend': ai_state['active_backend'],
            'mode': ai_state['mode'],
            'available': ai_state['inference_available'],
            'model_loaded': ai_state['model_loaded'],
            'error': ai_state['error'],
            'categories': effective_ai_config().get('categories', []),
        },
        'alerts': config.get('alerts', {}),
        'auth': {
            'enabled': auth_enabled,
            'session_timeout_hours': effective_auth_config().get('session_timeout_hours'),
            'max_login_attempts': effective_auth_config().get('max_login_attempts'),
            'lockout_minutes': effective_auth_config().get('lockout_minutes'),
        },
        'storage': {
            'database': effective_storage_config().get('database'),
            'snapshots_dir': effective_storage_config().get('snapshots_dir'),
            'recordings_dir': effective_storage_config().get('recordings_dir'),
        },
        'live': effective_live_config(),
        'recording': effective_recording_config(),
    }


@app.get('/api/recordings')
def recordings(label: str | None = None, camera_id: str | None = None, limit: int = Query(50, ge=1, le=200), alerted_only: bool = False):
    return database.list_recordings(label=label, camera_id=camera_id, limit=limit, alerted_only=alerted_only)


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
    # Use the specific label for human/object/alert triggers; fall back to trigger_type for generic triggers.
    color_key = trigger_label if trigger_type in {'human', 'object', 'alert'} and trigger_label else trigger_type

    return {
        **recording,
        'timeline_start_seconds': max(0.0, (visible_start - day_start).total_seconds()),
        'timeline_end_seconds': min(86400.0, (visible_end - day_start).total_seconds()),
        'timeline_duration_seconds': max(1.0, (visible_end - visible_start).total_seconds()),
        'color_key': color_key,
        'color_label': color_key,
    }


@app.get('/api/recordings/timeline')
def recordings_timeline(
    camera_id: str | None = None,
    day: str | None = None,
    tz_offset_minutes: int | None = Query(None, ge=-840, le=840),
):
    cameras = [
        {
            'id': str(camera_settings.get('id') or ''),
            'name': camera_default_name(camera_settings, f'Camera {index}'),
        }
        for index, camera_settings in enumerate(effective_cameras_config(), start=1)
    ]
    if not cameras and not camera_id:
        raise HTTPException(status_code=404, detail='No cameras configured')

    selected_camera_id = normalize_camera_id(camera_id or cameras[0]['id'])
    selected_camera = next((camera for camera in cameras if camera['id'] == selected_camera_id), None)
    if selected_camera is None:
        # Recordings outlive camera configuration: keep an explicitly requested
        # camera's timeline viewable even after the camera entry is removed.
        selected_camera = {'id': selected_camera_id, 'name': selected_camera_id}
        cameras = [*cameras, selected_camera]

    if day:
        try:
            target_day = datetime.strptime(day, '%Y-%m-%d').date()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='Invalid day. Use YYYY-MM-DD.') from exc
    else:
        target_day = datetime.now(timezone.utc).date()

    if tz_offset_minutes is None:
        timeline_timezone = timezone.utc
    else:
        # Browser getTimezoneOffset() is UTC-local minutes, so invert to get local UTC offset.
        timeline_timezone = timezone(timedelta(minutes=-tz_offset_minutes))

    day_start_local = datetime.combine(target_day, datetime.min.time(), tzinfo=timeline_timezone)
    day_end_local = day_start_local + timedelta(days=1)
    day_start = day_start_local.astimezone(timezone.utc)
    day_end = day_end_local.astimezone(timezone.utc)

    recordings = database.list_recordings_for_camera_day(selected_camera_id, day_start.isoformat(), day_end.isoformat())
    segments = [
        segment
        for segment in (
            _recording_timeline_segment(recording, day_start, day_end)
            for recording in recordings
        )
        if segment is not None
    ]
    rec_config = effective_recording_config()
    return {
        'camera': selected_camera,
        'cameras': cameras,
        'day': target_day.isoformat(),
        'day_start': day_start.isoformat(),
        'day_end': day_end.isoformat(),
        'timeline_timezone_offset_minutes': tz_offset_minutes if tz_offset_minutes is not None else 0,
        'pre_event_seconds': max(0, int(rec_config.get('pre_event_seconds', 5))),
        'recordings': segments,
    }


@app.post('/api/recordings/purge')
def purge_recordings(request: Request):
    require_admin(request)
    return purge_recordings_by_policy(force=True)


@app.get('/api/recordings/{recording_id}')
def recording_detail(recording_id: int):
    recording = database.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    file_path = Path(str(recording.get('file_path') or ''))
    recording['track'] = load_recording_detection_track(file_path)
    # Backfill from the live monitor's in-memory history while it still covers
    # the clip's window (e.g. a recording finalized before this feature, viewed
    # shortly after capture). Older clips simply have no track and playback
    # falls back to the static event boxes — clips are never decoded or
    # re-analyzed for overlays.
    if (
        recording['track'] is None
        and str(file_path)
        and file_path.exists()
        and not recording_track_sidecar_path(file_path).exists()
    ):
        window = _recording_capture_window(recording)
        if window and write_live_history_detection_track(
            recording_id, file_path, str(recording.get('camera_id') or '') or None, window[0], window[1],
        ):
            recording['track'] = load_recording_detection_track(file_path)
    return recording


@app.get('/api/recordings/{recording_id}/stream')
def stream_recording(recording_id: int, request: Request):
    recording = database.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    file_path = Path(recording['file_path'])
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail='Recording media file not found')

    stream_path = recording_stream_path(file_path)
    if not stream_path.exists() or not mp4_has_video_stream(stream_path):
        raise HTTPException(
            status_code=415,
            detail='Recording file is not a playable video stream. Generate a new recording to rebuild media.',
        )
    file_size = stream_path.stat().st_size
    media_type = mimetypes.guess_type(stream_path.name)[0] or 'video/mp4'
    range_header = request.headers.get('range')
    if not range_header:
        return FileResponse(stream_path, media_type=media_type)

    match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip())
    if not match:
        return Response(status_code=416, headers={'Content-Range': f'bytes */{file_size}'})
    start_text, end_text = match.groups()
    start = int(start_text) if start_text else 0
    end = int(end_text) if end_text else file_size - 1
    if start >= file_size or end < start:
        return Response(status_code=416, headers={'Content-Range': f'bytes */{file_size}'})
    end = min(end, file_size - 1)
    chunk_size = end - start + 1

    def iter_file():
        with stream_path.open('rb') as handle:
            handle.seek(start)
            remaining = chunk_size
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iter_file(),
        status_code=206,
        media_type=media_type,
        headers={
            'Content-Range': f'bytes {start}-{end}/{file_size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(chunk_size),
        },
    )


@app.get('/api/recordings/{recording_id}/download')
def download_recording(recording_id: int):
    recording = database.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    file_path = Path(recording['file_path'])
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail='Recording media file not found')
    stream_path = recording_stream_path(file_path)
    if not stream_path.exists() or not mp4_has_video_stream(stream_path):
        raise HTTPException(status_code=415, detail='Recording file is not a playable video stream.')
    started_at = str(recording.get('started_at') or '')
    safe_ts = re.sub(r'[^\w\-]', '_', started_at)[:19]
    filename = f'recording_{recording_id}_{safe_ts}.mp4'
    return FileResponse(
        stream_path,
        media_type='video/mp4',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.delete('/api/recordings/{recording_id}')
def delete_recording(recording_id: int, request: Request):
    require_admin(request)
    recording = database.delete_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    delete_recording_files([recording])
    write_audit_log(request, 'delete', 'recording', recording_id)
    return {'ok': True}


@app.delete('/api/recordings')
def delete_all_recordings(request: Request):
    require_admin(request)
    recordings = database.delete_all_recordings()
    delete_recording_files(recordings)
    write_audit_log(request, 'delete_all', 'recordings', details={'count': len(recordings)})
    return {'ok': True, 'deleted': len(recordings)}


@app.delete('/api/system/runtime-data')
def delete_runtime_data(request: Request):
    require_admin(request)
    recordings = database.delete_all_recordings()
    delete_recording_files(recordings)
    deleted_events = database.delete_all_events()
    deleted_alerts = database.delete_all_alerts()
    deleted_objects = database.delete_all_objects()
    storage_config = effective_storage_config()
    deleted_snapshots = clear_runtime_media_directory(storage_config.get('snapshots_dir'))
    deleted_event_artifacts = clear_runtime_media_directory(storage_config.get('events_dir'))
    with active_rtsp_recordings_lock:
        active_rtsp_recordings.clear()
    result = {
        'ok': True,
        'deleted': {
            'recordings': len(recordings),
            'events': deleted_events,
            'alerts': deleted_alerts,
            'objects': deleted_objects,
            'snapshot_files': deleted_snapshots,
            'event_artifacts': deleted_event_artifacts,
        },
        'preserved': ['settings', 'users', 'sessions', 'rules'],
    }
    write_audit_log(request, 'delete_all', 'runtime_data', details=result['deleted'])
    return result


@app.get('/api/users')
def list_users(request: Request):
    require_user(request)
    return auth.list_users()


@app.post('/api/users')
async def create_user(request: Request):
    require_admin(request)
    payload = await request.json()
    try:
        user = auth.create_user(
            payload.get('username', ''),
            payload.get('password', ''),
            payload.get('role', 'viewer'),
            first_name=payload.get('first_name', ''),
            last_name=payload.get('last_name', ''),
            email=payload.get('email', ''),
        )
        write_audit_log(request, 'create', 'user', user['id'], {'username': user['username'], 'role': user['role']})
        return user
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch('/api/users/{user_id}')
async def update_user(user_id: int, request: Request):
    require_admin(request)
    payload = await request.json()
    changes: dict[str, Any] = {}
    if 'role' in payload:
        changes['role'] = payload['role']
    if 'is_active' in payload:
        changes['is_active'] = payload['is_active']
    if 'password' in payload:
        changes['password_changed'] = True
    try:
        user = auth.update_user(user_id, role=payload.get('role'), is_active=payload.get('is_active'), password=payload.get('password'))
        write_audit_log(request, 'update', 'user', user_id, {'target_username': user.get('username'), **changes})
        return user
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def validate_ai_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_ai_config()
    allowed = {'enabled', 'backend', 'confidence', 'iou_threshold', 'input_size', 'model_path', 'labels_path'}
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
    updated['model_path'] = str(updated.get('model_path') or current.get('model_path') or 'models/yolov8n.onnx')
    updated['labels_path'] = str(updated.get('labels_path') or current.get('labels_path') or 'models/coco.names')
    return updated


def detector_status(ai_settings: dict[str, Any]) -> dict[str, Any]:
    ai_status = ai_status_payload(ai_settings)
    categories = ai_settings.get('categories', config.get('ai', {}).get('categories', []))
    labels = load_labels(ai_settings.get('labels_path'), categories) or list(categories)
    return {
        **ai_settings,
        'active_backend': ai_status['active_backend'],
        'configured_backend': ai_status['configured_backend'],
        'current_backend': ai_status['current_backend'],
        'mode': ai_status['mode'],
        'available': ai_status['inference_available'],
        'model_loaded': ai_status['model_loaded'],
        'detector_loaded': ai_status['detector_loaded'],
        'model_exists': ai_status['model_exists'],
        'onnx_runtime_installed': ai_status['onnx_runtime_installed'],
        'active_config_source': ai_status['active_config_source'],
        'error': ai_status['error'],
        'last_detector_error': ai_status['last_detector_error'],
        'categories': categories,
        'available_labels': labels,
    }


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
    if updated['enabled'] and not updated['host']:
        raise HTTPException(status_code=400, detail='SMTP host is required when email alerts are enabled.')
    if updated['enabled'] and not updated['from_address']:
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
    updated['enabled'] = _bool_value(updated.get('enabled', False))
    for key in ('server_url', 'topic', 'priority', 'username', 'password'):
        updated[key] = str(updated.get(key) or '').strip()
    if not updated['server_url']:
        updated['server_url'] = 'https://ntfy.sh'
    if not updated['priority']:
        updated['priority'] = 'default'
    valid_priorities = {'min', 'low', 'default', 'high', 'urgent'}
    if updated['priority'] not in valid_priorities:
        raise HTTPException(status_code=400, detail=f"priority must be one of: {', '.join(sorted(valid_priorities))}.")
    if updated['enabled'] and not updated['topic']:
        raise HTTPException(status_code=400, detail='Topic is required when push notifications are enabled.')
    return updated


def _bool_value(value: Any) -> bool:
    return value.lower() in {'1', 'true', 'yes', 'on'} if isinstance(value, str) else bool(value)


def _int_field(payload: dict[str, Any], field: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(payload.get(field, default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f'{field} must be an integer.') from exc
    if value < minimum or value > maximum:
        raise HTTPException(status_code=400, detail=f'{field} must be between {minimum} and {maximum}.')
    return value


def validate_camera_settings(payload: dict[str, Any], current: dict[str, Any] | None = None, index: int = 1) -> dict[str, Any]:
    current = current or {}
    updated = {
        key: current.get(key)
        for key in ('id', 'name', 'backend', 'device', 'width', 'height', 'fps', 'flip', 'stream_url', 'host', 'port', 'path', 'username', 'password')
        if key in current
    }
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
    if backend in {'onvif', 'rtsp'} and not build_stream_url(updated):
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
    detection['motion_enabled'] = _bool_value(detection.get('motion_enabled', True))
    detection['object_detection_enabled'] = _bool_value(detection.get('object_detection_enabled', True))
    detection['object_labels'] = normalize_label_list(detection.get('object_labels', []))
    detection['zones'] = normalize_monitoring_zones(detection.get('zones', []))
    updated['detection'] = detection

    existing_recording = current.get('recording') if isinstance(current.get('recording'), dict) else {}
    payload_recording = payload.get('recording') if isinstance(payload.get('recording'), dict) else {}
    updated['recording'] = normalize_camera_recording_settings({**existing_recording, **payload_recording})
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
    mode = str(merged.get('mode', 'motion')).lower()
    if mode not in {'off', 'continuous', 'motion', 'human', 'objects'}:
        raise HTTPException(status_code=400, detail='Recording mode must be off, continuous, motion, human, or objects.')
    fmt = str(merged.get('format', 'mp4')).strip().lstrip('.').lower() or 'mp4'
    if fmt == 'avi':
        fmt = 'mp4'
    if fmt != 'mp4':
        raise HTTPException(status_code=400, detail='Recording format must be mp4 for browser playback.')
    raw_objects = merged.get('record_on_objects', [])
    if isinstance(raw_objects, str):
        object_labels = [label.strip().lower() for label in raw_objects.split(',') if label.strip()]
    elif isinstance(raw_objects, list):
        object_labels = [str(label).strip().lower() for label in raw_objects if str(label).strip()]
    else:
        raise HTTPException(status_code=400, detail='record_on_objects must be a list or comma-separated string.')
    return {
        'enabled': _bool_value(merged.get('enabled', True)),
        'mode': mode,
        'continuous': _bool_value(merged.get('continuous', mode == 'continuous')),
        'record_on_motion': _bool_value(merged.get('record_on_motion', True)),
        'record_on_human': _bool_value(merged.get('record_on_human', True)),
        'record_on_objects': object_labels,
        'pre_event_seconds': _int_field(merged, 'pre_event_seconds', 10, 0, 300),
        'post_event_seconds': _int_field(merged, 'post_event_seconds', 15, 0, 300),
        'extension_step_seconds': _int_field(merged, 'extension_step_seconds', 45, 0, 300),
        'max_clip_seconds': _int_field(merged, 'max_clip_seconds', 300, 1, 3600),
        'format': fmt,
        'chunk_duration_seconds': _int_field(merged, 'chunk_duration_seconds', 3600, 60, 86400),
        'retention_days': _int_field(merged, 'retention_days', 14, 1, 3650),
        'max_storage_gb': _int_field(merged, 'max_storage_gb', 20, 1, 100000),
        'auto_purge_enabled': _bool_value(merged.get('auto_purge_enabled', True)),
    }


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
    return {
        'session_timeout_hours': session_timeout_hours,
        'max_login_attempts': _int_field(merged, 'max_login_attempts', 5, 1, 100),
        'lockout_minutes': _int_field(merged, 'lockout_minutes', 15, 1, 1440),
    }


def validate_live_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_live_config()
    merged = {**current, **payload}
    snapshot_refresh_ms = _int_field(merged, 'snapshot_refresh_ms', 500, 150, 5000)
    detection_status_refresh_ms = _int_field(merged, 'detection_status_refresh_ms', 2000, 500, 15000)
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
    return {
        'snapshot_refresh_ms': snapshot_refresh_ms,
        'detection_status_refresh_ms': detection_status_refresh_ms,
        'detection_interval_seconds': detection_interval_seconds,
        'event_debounce_seconds': event_debounce_seconds,
        'background_detection_enabled': background_detection_enabled,
        'detection_history_minutes': detection_history_minutes,
    }


def apply_cameras_settings(settings_list: list[dict[str, Any]]) -> None:
    global camera, camera_config, cameras_config, camera_instances
    cameras_config = settings_list
    camera_config = settings_list[0] if settings_list else {}
    camera_instances = create_camera_instances(settings_list)
    camera = camera_instances[camera_config['id']] if camera_config else None


def apply_storage_and_recording_settings() -> None:
    global storage, recording_service
    storage = Storage({**config, 'storage': effective_storage_config()})
    old_service = recording_service
    recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})
    if old_service is not None:
        try:
            old_service.stop_prebuffer_workers()
            old_service.stop_all_continuous_recordings()
        except Exception:
            pass


@app.get('/api/settings/ai')
def get_ai_settings():
    return detector_status(effective_ai_config())


def reload_detector(ai_settings: dict[str, Any]) -> tuple[bool, str | None]:
    global detector, last_detector_error, _min_rule_confidence_cache
    _min_rule_confidence_cache = None
    previous_detector = detector
    candidate = create_detector(ai_settings)
    candidate_error = getattr(candidate, 'unavailable_reason', None)
    if ai_settings['backend'] == 'onnx' and not getattr(candidate, 'available', False):
        detector = previous_detector
        last_detector_error = candidate_error or 'Failed to load ONNX detector.'
        log_detector_initialization('reload_failed')
        return False, last_detector_error
    detector = candidate
    last_detector_error = candidate_error
    log_detector_initialization('reload')
    return True, last_detector_error


@app.put('/api/settings/ai')
async def update_ai_settings(request: Request):
    require_admin(request)
    payload = await request.json()
    new_settings = validate_ai_settings(payload)
    database.set_setting('ai', new_settings, utc_now())
    reloaded, error = reload_detector(new_settings)
    response = detector_status(new_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
    write_audit_log(request, 'update', 'settings.ai', details={'model_path': new_settings.get('model_path'), 'backend': new_settings.get('backend')})
    return response


@app.post('/api/settings/ai/reload')
def reload_ai_detector():
    ai_settings = effective_ai_config()
    reloaded, error = reload_detector(ai_settings)
    response = detector_status(ai_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
    if not reloaded:
        return JSONResponse(response, status_code=400)
    return response


@app.post('/api/settings/ai/check-model')
def check_ai_model():
    return ai_status_payload(effective_ai_config())


def export_yolo_onnx(model_name: str, destination: Path) -> int:
    if model_name not in YOLO_MODELS:
        raise ValueError(f"Unknown model '{model_name}'. Available: {', '.join(YOLO_MODELS)}")
    info = YOLO_MODELS[model_name]
    pt_name = info['pt']
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        '-c',
        (
            'from ultralytics import YOLO\n'
            f"model = YOLO('{pt_name}')\n"
            "model.export(format='onnx')\n"
        ),
    ]
    result = subprocess.run(
        command,
        cwd=destination.parent,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
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


def export_yolov8n_onnx(destination: Path) -> int:
    return export_yolo_onnx('yolov8n', destination)


def _do_download_model(model_name: str, switch_active: bool = True) -> dict[str, Any]:
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'. Available: {', '.join(YOLO_MODELS)}")
    info = YOLO_MODELS[model_name]
    destination = BASE_DIR / 'models' / info['onnx']
    try:
        exported_bytes = export_yolo_onnx(model_name, destination)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Failed to export {info['label']} ONNX model. "
                'Install export dependencies with `pip install ultralytics onnx`, then retry. '
                f'Details: {exc}'
            ),
        ) from exc
    try:
        manifest = _fetch_models_manifest()
        installed_version = manifest.get('models', {}).get(model_name, {}).get('version') or 'unknown'
    except Exception:
        installed_version = 'unknown'
    with _installed_models_lock:
        installed_meta = _read_installed_models()
        installed_meta[model_name] = {
            'version': installed_version,
            'installed_at': utc_now(),
            'sha256': _sha256_file(destination),
        }
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
    return {
        'ok': True,
        'message': f"Exported {info['label']} ONNX to {destination.relative_to(BASE_DIR)}.",
        'model_path': rel_path,
        'bytes': exported_bytes,
        'reload_succeeded': reloaded,
        'reload_error': error,
        'status': ai_status_payload(updated),
    }


@app.get('/api/settings/ai/models')
def list_ai_models():
    models_dir = BASE_DIR / 'models'
    active_path = str(effective_ai_config().get('model_path') or '')
    installed_meta = _read_installed_models()
    result = []
    for model_id, info in YOLO_MODELS.items():
        onnx_path = models_dir / info['onnx']
        rel_path = str((models_dir / info['onnx']).relative_to(BASE_DIR))
        installed = onnx_path.exists()
        meta = installed_meta.get(model_id, {})
        result.append({
            'id': model_id,
            'label': info['label'],
            'description': info['description'],
            'approx_mb': info['approx_mb'],
            'path': rel_path,
            'installed': installed,
            'active': active_path == rel_path,
            'size_bytes': onnx_path.stat().st_size if installed else None,
            'installed_version': meta.get('version') if installed else None,
        })
    return result


@app.post('/api/settings/ai/download-model')
async def download_ai_model(request: Request):
    body = await request.json()
    return _do_download_model(str(body.get('model') or '').strip().lower())


@app.post('/api/settings/ai/download-yolov8n')
def download_yolov8n_model():
    return _do_download_model('yolov8n')


@app.get('/api/settings/ai/check-model-updates')
def check_model_updates(request: Request):
    require_admin(request)
    installed_meta = _read_installed_models()
    models_dir = BASE_DIR / 'models'
    try:
        manifest = _fetch_models_manifest()
    except urllib.error.HTTPError as exc:
        return {'error': f'Manifest fetch error {exc.code}: {exc.reason}', 'models': [], 'any_updates': False}
    except Exception as exc:
        return {'error': str(exc), 'models': [], 'any_updates': False}
    manifest_models = manifest.get('models', {})
    result = []
    for model_id, info in YOLO_MODELS.items():
        onnx_path = models_dir / info['onnx']
        in_meta = model_id in installed_meta
        if not in_meta and not onnx_path.exists():
            continue
        meta = installed_meta.get(model_id, {})
        installed_version = meta.get('version') or 'unknown'
        remote_version = manifest_models.get(model_id, {}).get('version')
        update_available = bool(
            remote_version
            and (
                installed_version == 'unknown'
                or _parse_semver(remote_version) > _parse_semver(installed_version)
            )
        )
        result.append({
            'id': model_id,
            'installed_version': installed_version,
            'latest_version': remote_version,
            'update_available': update_available,
        })
    return {
        'manifest_updated_at': manifest.get('updated_at'),
        'models': result,
        'any_updates': any(m['update_available'] for m in result),
    }


@app.post('/api/settings/ai/update-model')
async def update_ai_model(request: Request):
    require_admin(request)
    body = await request.json()
    model_name = str(body.get('model') or '').strip().lower()
    if model_name not in YOLO_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_name}'.")
    return _do_download_model(model_name, switch_active=False)


@app.post('/api/settings/ai/test-detector')
def test_ai_detector():
    ai_settings = effective_ai_config()
    ai_state = ai_status_payload(ai_settings)
    ai_error: str | None = None
    detections: list = []
    if not hasattr(detector, 'detect_image'):
        ai_error = 'Configured detector cannot run image inference.'
    else:
        try:
            detections = detector.detect_image(ONE_PIXEL_PNG)
        except DetectorUnavailableError as exc:
            ai_error = str(exc) or ai_state.get('last_detector_error') or 'Detector unavailable.'
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': ai_error is None, 'backend_used': ai_state['configured_backend'], 'detections': detections, 'status': ai_state, 'ai_error': ai_error}


@app.get('/api/settings/alert-email')
def get_alert_email_settings():
    return effective_email_alert_settings()


@app.put('/api/settings/alert-email')
async def update_alert_email_settings(request: Request):
    require_admin(request)
    payload = await request.json()
    settings = validate_alert_email_settings(payload)
    result = database.set_setting('alert_email', settings, utc_now())
    write_audit_log(request, 'update', 'settings.alert_email')
    return result


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


@app.get('/api/settings/alert-push')
def get_push_notification_settings():
    return effective_push_notification_settings()


@app.put('/api/settings/alert-push')
async def update_push_notification_settings(request: Request):
    require_admin(request)
    payload = await request.json()
    settings = validate_push_notification_settings(payload)
    result = database.set_setting('alert_push', settings, utc_now())
    write_audit_log(request, 'update', 'settings.alert_push')
    return result


@app.post('/api/settings/alert-push/test')
async def test_push_notification_settings(request: Request):
    payload = await request.json()
    settings = validate_push_notification_settings(payload.get('settings') if isinstance(payload.get('settings'), dict) else payload)
    try:
        PushNotificationService(settings).send_test()
    except PushNotificationError as exc:
        raise HTTPException(status_code=400, detail=f'Test notification failed: {exc}') from exc
    return {'ok': True}


@app.get('/api/settings/system')
def get_system_settings():
    return {
        'camera': get_camera_config(None),
        'cameras': effective_cameras_config(),
        'live': effective_live_config(),
        'recording': effective_recording_config(),
        'storage': effective_storage_config(),
        'auth': {
            'session_timeout_hours': effective_auth_config().get('session_timeout_hours'),
            'max_login_attempts': effective_auth_config().get('max_login_attempts'),
            'lockout_minutes': effective_auth_config().get('lockout_minutes'),
        },
        'bootstrap': {
            'database': config.get('storage', {}).get('database'),
            'auth_enabled': auth_enabled,
            'cookie_name': SESSION_COOKIE_NAME,
            'server': config.get('server', {}),
        },
    }


@app.get('/api/settings/system/database/backup')
def backup_database(request: Request):
    require_admin(request)
    backup_path = create_database_backup()
    write_audit_log(request, 'backup', 'database', details={'filename': backup_path.name})
    return FileResponse(
        backup_path,
        media_type='application/vnd.sqlite3',
        filename=backup_path.name,
        headers={'Cache-Control': 'no-store'},
    )


@app.post('/api/settings/system/database/restore')
async def restore_database(request: Request, file: UploadFile = File(...)):
    require_admin(request)
    filename = Path(file.filename or '').name
    if not filename:
        raise HTTPException(status_code=400, detail='Choose a SQLite database backup file to restore.')
    restore_temp = database.database_path.parent / f'.restore-{secrets.token_hex(8)}.sqlite3'
    try:
        with restore_temp.open('wb') as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        if restore_temp.stat().st_size == 0:
            raise HTTPException(status_code=400, detail='Uploaded database backup is empty.')
        validate_restore_database(restore_temp)
        safety_backup = create_database_backup(prefix='pre-restore-daygle-database')
        shutil.move(str(restore_temp), database.database_path)
        refresh_runtime_after_database_restore()
        write_audit_log(request, 'restore', 'database', details={'source_filename': filename, 'safety_backup': str(safety_backup)})
        return {
            'ok': True,
            'message': 'Database restored successfully.',
            'source_filename': filename,
            'safety_backup': str(safety_backup),
        }
    finally:
        restore_temp.unlink(missing_ok=True)
        await file.close()


@app.get('/api/cameras')
def list_cameras():
    return {'cameras': effective_cameras_config()}


@app.put('/api/cameras')
async def update_cameras(request: Request):
    require_admin(request)
    settings = validate_cameras_settings(await request.json())
    database.set_setting('cameras', settings, utc_now())
    apply_cameras_settings(settings)
    write_audit_log(request, 'update', 'settings.cameras', details={'count': len(settings)})
    return {'cameras': settings}


@app.put('/api/cameras/{camera_id}')
async def update_camera(camera_id: str, request: Request):
    require_admin(request)
    normalized = normalize_camera_id(camera_id)
    payload = await request.json()
    settings_list = list(effective_cameras_config())
    for index, current in enumerate(settings_list):
        if current.get('id') == normalized:
            settings_list[index] = validate_camera_settings({**payload, 'id': normalized}, current=current, index=index + 1)
            database.set_setting('cameras', settings_list, utc_now())
            apply_cameras_settings(settings_list)
            write_audit_log(request, 'update', 'settings.camera', normalized, {'camera_name': settings_list[index].get('name')})
            return settings_list[index]
    # Upsert: a PUT to an unknown id creates the camera (there is no default
    # camera on a clean install anymore).
    created = validate_camera_settings({**payload, 'id': normalized}, index=len(settings_list) + 1)
    settings_list.append(created)
    database.set_setting('cameras', settings_list, utc_now())
    apply_cameras_settings(settings_list)
    write_audit_log(request, 'create', 'settings.camera', normalized, {'camera_name': created.get('name')})
    return created


@app.put('/api/settings/system/live')
async def update_live_settings(request: Request):
    require_admin(request)
    settings = validate_live_settings(await request.json())
    database.set_setting('live', settings, utc_now())
    write_audit_log(request, 'update', 'settings.live')
    return settings


@app.put('/api/settings/system/recording')
async def update_recording_settings(request: Request):
    require_admin(request)
    settings = validate_recording_settings(await request.json())
    database.set_setting('recording', settings, utc_now())
    apply_storage_and_recording_settings()
    write_audit_log(request, 'update', 'settings.recording')
    return settings


@app.put('/api/settings/system/storage')
async def update_storage_settings(request: Request):
    require_admin(request)
    settings = validate_storage_settings(await request.json())
    database.set_setting('storage', settings, utc_now())
    apply_storage_and_recording_settings()
    write_audit_log(request, 'update', 'settings.storage')
    return settings


@app.put('/api/settings/system/auth')
async def update_auth_settings(request: Request):
    require_admin(request)
    settings = validate_auth_settings(await request.json())
    database.set_setting('auth', settings, utc_now())
    auth.apply_config(settings)
    write_audit_log(request, 'update', 'settings.auth')
    return settings


def _current_version() -> str:
    version_file = BASE_DIR / 'VERSION'
    return version_file.read_text(encoding='utf-8').strip() if version_file.exists() else 'unknown'


def _parse_semver(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split('.'))
    except ValueError:
        return (0,)


@app.get('/api/update/check')
def check_update(request: Request):
    require_admin(request)
    current_version = _current_version()
    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest',
            headers={
                'User-Agent': 'daygle-ai-camera-updater/1.0',
                'Accept': 'application/vnd.github.v3+json',
            },
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())
        tag_name = str(data.get('tag_name') or '')
        latest_version = tag_name.lstrip('v')
        update_available = bool(
            latest_version
            and current_version != 'unknown'
            and _parse_semver(latest_version) > _parse_semver(current_version)
        )
        return {
            'current_version': current_version,
            'latest_version': latest_version,
            'tag_name': tag_name,
            'html_url': str(data.get('html_url') or ''),
            'release_notes': str(data.get('body') or ''),
            'published_at': str(data.get('published_at') or ''),
            'update_available': update_available,
        }
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
        result = subprocess.run(
            ['bash', str(update_script)],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(BASE_DIR),
        )
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
        check = subprocess.run(
            ['systemctl', 'is-active', 'daygle-ai-camera'],
            capture_output=True, text=True, timeout=5, check=False,
        )
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

    return {
        'ok': result.returncode == 0,
        'output': output[-4000:],
        'returncode': result.returncode,
        'new_version': _current_version(),
        'service_restart_scheduled': service_restart_scheduled,
    }


@app.get('/api/audit')
def list_audit_log(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: str | None = None,
    username: str | None = None,
    resource: str | None = None,
):
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


if __name__ == '__main__':
    import uvicorn

    server_config = config.get('server', {})
    uvicorn.run(
        'app.main:app',
        host=server_config.get('host', '0.0.0.0'),
        port=int(server_config.get('port', 8080)),
        reload=False,
    )



