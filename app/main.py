from __future__ import annotations

import copy
import importlib.util
import logging
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
from app.anpr import AnprPipeline, normalize_plate, plate_matches
from app.auth import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, AuthError, AuthService
from app.database import EventDatabase
from app.detector import DetectorUnavailableError, create_detector, load_labels
from app.email_alerts import EmailAlertError, EmailAlertService
from app.camera_backend import OpenCvStreamCamera
from app.recordings import RecordingService
from app.settings import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, load_settings
from app.storage import Storage

logger = logging.getLogger('daygle.ai')

YOLOV8N_MODEL = 'yolov8n.pt'
YOLOV8N_ONNX = 'yolov8n.onnx'
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


def effective_camera_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('camera', {}))
    override = database.get_setting('camera')
    if isinstance(override, dict):
        settings.update(override)
    return settings


def effective_anpr_config() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('anpr', {}))
    override = database.get_setting('anpr')
    if isinstance(override, dict):
        settings.update(override)
    return settings


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
        'detection_interval_seconds': 0.25,
        'event_debounce_seconds': 10.0,
        'background_detection_enabled': True,
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


def effective_alert_rules() -> list[dict[str, Any]]:
    return database.list_alert_rules()


def effective_email_alert_settings() -> dict[str, Any]:
    settings = copy.deepcopy(config.get('alerts', {}).get('email', {}))
    override = database.get_setting('alert_email')
    if isinstance(override, dict):
        settings.update(override)
    return settings


database = EventDatabase(config['storage']['database'])
camera_config = effective_camera_config()
cameras_config: list[dict[str, Any]] = []
camera_instances: dict[str, Any] = {}
camera = None

storage = Storage({**config, 'storage': effective_storage_config()})
recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})
anpr_pipeline = AnprPipeline(effective_anpr_config())
auth = AuthService(config['storage']['database'], effective_auth_config())
SESSION_COOKIE_NAME = str(effective_auth_config().get('cookie_name', SESSION_COOKIE))


database.seed_alert_rules(config.get('alerts', {}).get('rules', []), utc_now())
detector = create_detector(effective_ai_config())
last_detector_error: str | None = getattr(detector, 'unavailable_reason', None)
alerts = AlertEngine(effective_alert_rules())
live_detection_last_checked: dict[str, float] = {}
live_detection_status: dict[str, dict[str, Any]] = {}
live_event_last_emitted: dict[str, dict[str, Any]] = {}
live_detection_retry_after: dict[str, float] = {}
live_detection_failure_count: dict[str, int] = {}
active_rtsp_recordings: dict[str, dict[str, Any]] = {}
active_rtsp_recordings_lock = threading.Lock()
live_detection_worker_lock = threading.Lock()
active_live_detection_cameras: set[str] = set()
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
        'anpr_enabled': True,
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
        label = str(raw_label).strip().lower()
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
        labels = normalize_label_list(rule.get('label') or rule.get('object') or '')
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
        })
    return rules


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
        normalized.append({
            'id': normalize_camera_id(zone.get('id'), f'zone-{index}'),
            'name': str(zone.get('name') or f'Zone {index}').strip() or f'Zone {index}',
            'x': round(x, 4),
            'y': round(y, 4),
            'width': round(width, 4),
            'height': round(height, 4),
            'points': points,
            'enabled': bool(zone.get('enabled', True)),
            'monitor_motion': bool(zone.get('monitor_motion', True)),
            'monitor_objects': bool(zone.get('monitor_objects', True)),
            'object_labels': [rule['label'] for rule in normalize_zone_object_rules(zone)],
            'object_rules': normalize_zone_object_rules(zone),
            'monitor_anpr': bool(zone.get('monitor_anpr', True)),
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
    detection = default_camera_detection_settings()
    if isinstance(camera_settings.get('detection'), dict):
        detection.update(camera_settings['detection'])
    for key in ('motion_enabled', 'motion_email_enabled', 'object_detection_enabled', 'anpr_enabled'):
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
    return [normalize_camera_settings({}, 1)]


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
    points = zone.get('points') or []
    if isinstance(points, list) and len(points) >= 3:
        return False
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
    return filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_objects')


def detection_label_allowed_for_zone(detection: dict[str, Any], zone: dict[str, Any], camera_labels: set[str]) -> bool:
    zone_labels = set(normalize_label_list(zone.get('object_labels', [])))
    allowed_labels = zone_labels or camera_labels
    if not allowed_labels:
        return True
    return str(detection.get('label') or '').strip().lower() in allowed_labels


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
    if not label:
        return []
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for zone in zones:
        if not detection_matches_zone(detection, zone):
            continue
        for rule in normalize_zone_object_rules(zone):
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
        for rule in normalize_zone_object_rules(zone):
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
                'email_recipients': rule.get('email_recipients', []),
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


def zone_detection_alert_rule_names(settings: dict[str, Any], detection: dict[str, Any]) -> set[str]:
    return {zone_rule_name(settings, zone, rule) for zone, rule in zone_object_rule_matches(settings, detection, action='alert')}


def filter_detections_for_camera_anpr(detections: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    detection_settings = settings.get('detection') or {}
    zones = [zone for zone in detection_settings.get('zones', []) if zone.get('enabled', True) and zone.get('monitor_anpr', True)]
    if not zones:
        return []
    return [detection for detection in detections if any(detection_matches_zone(detection, zone) for zone in zones)]


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


def detection_label_set(detections: list[dict[str, Any]]) -> set[str]:
    return {
        str(detection.get('label') or '').strip().lower()
        for detection in detections
        if str(detection.get('label') or '').strip()
    }


def live_event_is_debounced(camera_id: str, labels: set[str], debounce_seconds: float) -> bool:
    if debounce_seconds <= 0 or not labels:
        return False
    previous = live_event_last_emitted.get(camera_id)
    if not previous:
        return False
    elapsed = time.time() - float(previous.get('timestamp', 0))
    if elapsed > debounce_seconds:
        return False
    previous_labels = {str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()}
    return bool(previous_labels & labels)


def remember_live_event(camera_id: str, labels: set[str]) -> None:
    if not labels:
        return
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
    camera_key = str(selected_config.get('id') or 'camera')
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
    for selected_config in list(effective_cameras_config()):
        camera_id = str(selected_config.get('id') or 'camera')
        if not _camera_has_live_alert_stream(selected_config):
            continue
        retry_after = live_detection_retry_after.get(camera_id, 0)
        now = time.time()
        if retry_after and now < retry_after:
            continue
        stream_url = build_stream_url(selected_config)
        if stream_url:
            recording_service.prime_rtsp_prebuffer(
                stream_url=stream_url,
                camera_id=camera_id,
                recording_config=camera_event_recording_config(selected_config),
            )
        detection_interval_seconds = float(live_settings.get('detection_interval_seconds', 0.25))
        with live_detection_worker_lock:
            if camera_id in active_live_detection_cameras:
                continue
            if now - live_detection_last_checked.get(camera_id, 0) < detection_interval_seconds:
                continue
            live_detection_last_checked[camera_id] = now
            active_live_detection_cameras.add(camera_id)
        try:
            selected_camera = get_camera_instance(camera_id)
            if not hasattr(selected_camera, 'read_jpeg'):
                update_live_detection_status(camera_id, state='skipped', reason='Background alerts require a camera that can read JPEG frames.', detections=[])
                continue
            image_bytes, frame = selected_camera.read_jpeg()
            clear_live_camera_backoff(camera_id)
            process_live_stream_alerts(image_bytes, frame, copy.deepcopy(selected_config), enforce_interval=False)
            processed += 1
        except Exception as exc:
            logger.warning('Background live alert check failed for camera %s: %s', camera_id, exc)
            schedule_live_camera_backoff(camera_id, str(exc))
        finally:
            with live_detection_worker_lock:
                active_live_detection_cameras.discard(camera_id)
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


def process_live_stream_alerts(image_bytes: bytes, frame: dict[str, Any], settings: dict[str, Any], *, enforce_interval: bool = True) -> int | None:
    camera_id = str(settings.get('id') or 'camera')
    live_settings = effective_live_config()
    recording_settings = effective_recording_config()
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

    try:
        detections = detector.detect_image(image_bytes)
    except (DetectorUnavailableError, ValueError) as exc:
        logger.warning('Live detection skipped for camera %s: %s', camera_id, exc)
        update_live_detection_status(camera_id, state='error', reason=str(exc), ai=ai_state, detections=[])
        return None

    detections = normalize_detection_boxes_for_frame(detections, frame)
    raw_labels = [str(detection.get('label')) for detection in detections if detection.get('label')]
    motion_detections = filter_detections_for_camera_zones(detections, settings, zone_monitor_key='monitor_motion', require_zones=True)
    try:
        motion_min_confidence = float(recording_settings.get('motion_min_confidence', 0.45))
    except (TypeError, ValueError):
        motion_min_confidence = 0.45
    motion_min_confidence = max(0.0, min(1.0, motion_min_confidence))
    motion_detections = [detection for detection in motion_detections if float(detection.get('confidence', 0)) >= motion_min_confidence]
    object_detections = filter_detections_for_camera(detections, settings)
    anpr_detections = filter_detections_for_camera_anpr(detections, settings)
    zone_rules = zone_object_alert_rules(settings)
    object_alert_detections = zone_alert_detections(settings, object_detections) if zone_rules else list(object_detections)
    alert_detections = list(object_alert_detections)
    if motion_detections:
        strongest_motion = max(motion_detections, key=lambda detection: float(detection.get('confidence', 0)))
        alert_detections.append({
            **strongest_motion,
            'label': 'motion',
            'motion_event': True,
        })
    if not alert_detections and not anpr_detections:
        update_live_detection_status(
            camera_id,
            state='checked',
            reason='No detections matched this camera and its monitoring areas.',
            detected_labels=raw_labels,
            matched_labels=[],
            detections=object_detections,
            anpr_detections=[],
        )
        return None

    alerts.rules = zone_rules + effective_alert_rules()
    triggered = alerts.process(alert_detections)
    triggered_rule_names = {str(alert.get('rule_name') or '') for alert in triggered}
    triggered_labels = {str(alert.get('label') or '').lower() for alert in triggered}
    recording_detections = [
        {
            **detection,
            'alert_matched': bool(zone_detection_alert_rule_names(settings, detection) & triggered_rule_names)
            if zone_rules else str(detection.get('label') or '').lower() in triggered_labels,
            'alert_triggered': zone_record_on_detect(detection, settings)
            if zone_rules else str(detection.get('label') or '').lower() in triggered_labels,
        }
        for detection in object_detections
    ]
    if motion_detections:
        recording_detections.append({
            **strongest_motion,
            'label': 'motion',
            'motion_event': True,
            'alert_matched': 'motion' in triggered_labels,
            'alert_triggered': 'motion' in triggered_labels,
        })
    matched_labels = [str(detection.get('label')) for detection in alert_detections if detection.get('label')]
    camera_recording_config = camera_event_recording_config(settings)
    should_record_event, _trigger_type, _trigger_label = recording_service.should_record(recording_detections, camera_recording_config)
    debounce_seconds = max(0.0, float(live_settings.get('event_debounce_seconds', 10.0)))
    debounced_labels = detection_label_set([detection for detection in recording_detections if detection.get('alert_triggered')])
    if not debounced_labels:
        debounced_labels = detection_label_set(recording_detections)
    if should_record_event and live_event_is_debounced(camera_id, debounced_labels, debounce_seconds):
        extended_recording_id = extend_active_rtsp_recording(
            camera_id=camera_id,
            event_time=datetime.now(timezone.utc).isoformat(),
            recording_config=camera_recording_config,
        )
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
            anpr_detections=anpr_detections,
            recording_id=extended_recording_id,
        )
        return None

    event_time = datetime.now(timezone.utc).isoformat()
    snapshot_path = storage.save_image_snapshot(image_bytes, f'{camera_id}.jpg')
    event_id = database.add_event(
        created_at=event_time,
        source='rtsp',
        snapshot_path=snapshot_path,
        detections=alert_detections or anpr_detections,
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
    process_anpr_for_event(event_id, anpr_detections, snapshot_path, event_time)

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
    deliver_email_alerts(email_triggered, event_id, rules=zone_rules + effective_alert_rules())
    email_rules = [
        rule for rule in (zone_rules + effective_alert_rules())
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
        anpr_detections=anpr_detections,
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
    return OpenCvStreamCamera(build_stream_url(settings), width=width, height=height, fps=fps)


def create_camera_instances(settings_list: list[dict[str, Any]]) -> dict[str, Any]:
    return {str(settings['id']): create_camera(settings) for settings in settings_list}


cameras_config = effective_cameras_config()
camera_config = cameras_config[0]
camera_instances = create_camera_instances(cameras_config)
camera = camera_instances[camera_config['id']]

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
    return {
        'current_backend': configured_backend,
        'active_backend': active_backend,
        'configured_backend': configured_backend,
        'mode': mode,
        'model_loaded': model_loaded,
        'detector_loaded': detector_loaded,
        'model_path': str(settings.get('model_path') or ''),
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
ADMIN_PATHS = {'/ai', '/settings', '/users', '/zones'}
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

    admin_required = path in ADMIN_PATHS or path.startswith('/api/users') or path.startswith('/api/settings/ai') or path.startswith('/api/settings/anpr') or path.startswith('/api/settings/system') or (path.startswith('/api/cameras') and request.method in MUTATING_METHODS) or (
        path.startswith('/api/settings/alert-email') and request.method in MUTATING_METHODS
    ) or (
        path.startswith('/api/settings/alerts') and request.method in MUTATING_METHODS
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
<title>{escape(title)} Ã‚Â· Daygle AI Camera</title><link rel="stylesheet" href="/static/styles.css" /></head>
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
    max_deadline_ts = start_capture_ts + duration_seconds

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
                recording_service.write_rtsp_clip_with_prebuffer(
                    stream_url=stream_url,
                    camera_id=camera_id,
                    file_path=file_path,
                    triggered_at=triggered_at,
                    pre_seconds=pre_seconds,
                    post_seconds=dynamic_post_seconds,
                    max_duration_seconds=final_duration_seconds,
                )
            else:
                recording_service.write_rtsp_clip(stream_url, file_path, final_duration_seconds)

            ended_at = datetime.fromtimestamp(start_capture_ts + final_duration_seconds, tz=timezone.utc).isoformat()
            database.update_recording_timing(
                recording_id,
                ended_at=ended_at,
                duration_seconds=final_duration_seconds,
            )
        except Exception as exc:
            logger.warning('RTSP recording capture failed for event %s, writing generated fallback: %s', event_id, exc)
            write_generated_fallback()
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


def deliver_email_alerts(triggered: list[dict[str, Any]], event_id: int, rules: list[dict[str, Any]] | None = None) -> None:
    if not triggered:
        return
    event = database.get_event(event_id) or {}
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    camera_name = str(metadata.get('camera_name') or '').strip() or None
    camera_id = str(metadata.get('camera_id') or '').strip() or None
    rules_by_name = {str(rule.get('name')): rule for rule in (rules or effective_alert_rules())}
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
            )
        except EmailAlertError as exc:
            logger.warning('Failed to send email alert for event %s rule %s: %s', event_id, alert.get('rule_name'), exc)


plate_alert_last_triggered: dict[str, float] = {}


def process_anpr_for_event(event_id: int, detections: list[dict[str, Any]], image_path: str | None, created_at: str) -> list[dict[str, Any]]:
    plate_results = anpr_pipeline.process_event(event_id=event_id, detections=detections, image_path=image_path, storage=storage)
    stored: list[dict[str, Any]] = []
    for result in plate_results:
        plate = database.upsert_plate(result['plate_number'], created_at)
        plate_event = database.add_plate_event(
            event_id=event_id,
            plate_id=plate['id'],
            plate_number=result['plate_number'],
            confidence=float(result['confidence']),
            image_path=result.get('image_path'),
            created_at=created_at,
        )
        stored.append(plate_event)
    trigger_plate_alerts(stored)
    return stored


def trigger_plate_alerts(plate_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import time

    triggered: list[dict[str, Any]] = []
    rules = database.list_plate_alert_rules()
    for plate_event in plate_events:
        plate = plate_event.get('plate') or {}
        for rule in rules:
            if not rule.get('enabled', True):
                continue
            rule_key = f"{rule['id']}:{plate_event['plate_number']}"
            last = plate_alert_last_triggered.get(rule_key, 0)
            if time.time() - last < int(rule.get('cooldown_seconds', 60)):
                continue
            rule_type = str(rule.get('rule_type'))
            matched = False
            if rule_type == 'plate':
                matched = plate_matches(rule.get('plate_pattern'), plate_event['plate_number'])
            elif rule_type == 'unknown':
                matched = not plate.get('is_whitelisted') and not plate.get('is_blacklisted')
            elif rule_type == 'blacklisted':
                matched = bool(plate.get('is_blacklisted'))
            if matched:
                plate_alert_last_triggered[rule_key] = time.time()
                triggered.append({'rule_name': rule['rule_name'], 'plate_number': plate_event['plate_number'], 'plate_event_id': plate_event['id']})
    return triggered


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
                f'<text x="{x:.1f}" y="{label_y:.1f}">{label} Ã‚Â· {confidence}%</text></g>'
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
  <text x="48" y="112" class="muted">Frame #{frame_number} Ã‚Â· {timestamp} Ã‚Â· Overlay {overlay_state}</text>
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
        if detection.get('alert_matched', detection.get('alert_triggered')) is False:
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


def recording_playback_sidecar_path(file_path: Path) -> Path:
    return file_path.with_name(f'{file_path.stem}.h264.mp4')


def recording_stream_path(file_path: Path) -> Path:
    playback_path = recording_playback_sidecar_path(file_path)
    if playback_path.exists() and playback_path.stat().st_mtime >= file_path.stat().st_mtime:
        return playback_path
    try:
        transcode_recording_to_mp4(file_path, playback_path)
    except Exception as exc:
        logger.warning('Recording playback conversion failed for %s: %s', file_path, exc)
        return file_path
    return playback_path if playback_path.exists() else file_path


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
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
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
    database.seed_alert_rules(config.get('alerts', {}).get('rules', []), utc_now())
    apply_cameras_settings(effective_cameras_config())
    apply_storage_and_recording_settings()
    apply_anpr_settings()
    auth.apply_config(effective_auth_config())
    alerts.rules = effective_alert_rules()


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
    try:
        _user, token, _csrf_token, expires_at = auth.authenticate(
            data.get('username', ''), data.get('password', ''), request.client.host if request.client else 'unknown'
        )
    except AuthError as exc:
        return login_page(request, str(exc))
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
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>AI Settings - Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell page-stack"><header class="hero"><div><p class="eyebrow">Administration</p><h1>AI Settings</h1><p class="muted">Configure AI detection, install models, and reload the detector.</p></div></header><section class="card"><h2>AI Status</h2><div id="settingsMessage" class="muted"></div><div id="aiStatusPanel" class="status-panel"></div><div class="button-row"><button id="checkModelBtn" class="secondary" type="button">Check Model</button><button id="downloadModelBtn" type="button">Download YOLOv8n ONNX</button><button id="reloadDetectorBtn" class="secondary" type="button">Reload Detector</button><button id="testDetectorBtn" class="secondary" type="button">Test Detector</button></div></section><section class="card"><h2>AI Settings</h2><form id="aiSettingsForm" class="form-grid"><label><span>AI Enabled</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><label><span>Backend</span><select name="backend"><option value="onnx">ONNX</option></select></label><label><span>Confidence</span><input name="confidence" type="number" min="0" max="1" step="0.01" /></label><label><span>IoU Threshold</span><input name="iou_threshold" type="number" min="0" max="1" step="0.01" /></label><label><span>Input Size</span><input name="input_size" type="number" min="32" max="2048" step="32" /></label><label><span>Model Path</span><input name="model_path" /></label><label><span>Labels Path</span><input name="labels_path" /></label><button type="submit">Save AI Settings</button></form></section></main><script src="/static/ai.js"></script></body></html>""")


@app.get('/profile')
def profile_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Profile Ã‚Â· Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell page-stack"><header class="hero"><div><p class="eyebrow">Account</p><h1>Profile</h1><p class="muted">Manage your display preferences and password.</p></div></header><section class="card"><h2>Profile Details</h2><div id="profileMessage" class="muted"></div><div id="profileSummary" class="status-panel"></div><form id="profileForm" class="form-grid"><input name="timezone" placeholder="Timezone" required /><label><span>Date Format</span><select name="date_format"><option value="locale">Browser Locale</option><option value="iso">YYYY-MM-DD</option><option value="au">DD/MM/YYYY</option><option value="us">MM/DD/YYYY</option></select></label><label><span>Time Format</span><select name="time_format"><option value="24h">24 Hour</option><option value="12h">12 Hour</option></select></label><button type="submit">Save Profile</button></form></section><section class="card"><h2>Change Password</h2><form id="passwordForm" class="form-grid"><input name="current_password" type="password" placeholder="Current password" autocomplete="current-password" required /><input name="new_password" type="password" placeholder="New password" autocomplete="new-password" required /><input name="confirm_password" type="password" placeholder="Confirm password" autocomplete="new-password" required /><button type="submit">Change Password</button></form></section></main><script src="/static/profile.js"></script></body></html>""")


@app.get('/anpr')
def anpr_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>ANPR Ã‚Â· Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell page-stack"><header class="hero"><div><p class="eyebrow">Recognition</p><h1>ANPR</h1><p class="muted">Search plates, review sightings, and manage plate alerts.</p></div></header><section class="card anpr-search-card"><h2>Plate search</h2><div id="anprMessage" class="muted"></div><div class="search-row anpr-search-row"><select id="anprCameraFilter" aria-label="Filter by camera"><option value="">All cameras</option></select><input id="plateSearchInput" placeholder="ABC123, 1ABC2D, XYZ999..." /><button id="plateSearchBtn">Search</button><button id="plateClearBtn" class="secondary">Recent</button></div><div id="plateResults" class="list"></div></section><section class="grid main-grid"><article class="card"><div class="section-header"><h2>Recent plates</h2><button id="deleteAllPlatesBtn" class="secondary delete-btn" type="button" hidden>Delete All</button></div><div id="recentPlates" class="list"></div></article><article class="card"><div class="section-header"><h2>Plate details</h2></div><div id="plateDetails" class="list"></div></article></section><section class="card"><h2>Plate alert rules</h2><form id="plateAlertRuleForm" class="form-grid"><input type="hidden" name="id" /><input name="rule_name" placeholder="Rule name" required /><label><span>Type</span><select name="rule_type"><option value="plate">Specific Plate</option><option value="unknown">Unknown Plate</option><option value="blacklisted">Blacklisted Plate</option></select></label><input name="plate_pattern" placeholder="Plate pattern" /><input name="cooldown_seconds" type="number" min="0" placeholder="Cooldown seconds" value="60" /><label><span>Enabled</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label><button type="submit">Save Rule</button><button id="cancelPlateRuleEdit" class="secondary" type="button">Cancel Edit</button></form><div id="plateAlertRules" class="list"></div></section></main><script src="/static/anpr.js"></script></body></html>""")


def _settings_section_anpr() -> str:
    return (
        '<section class="card"><h2>ANPR</h2>'
        '<form id="anprSettingsForm" class="form-grid">'
        '<label><span>OCR Backend</span><select name="backend"><option value="paddleocr">PaddleOCR</option><option value="easyocr">EasyOCR</option></select></label>'
        '<input name="min_confidence" type="number" min="0" max="1" step="0.01" placeholder="Min Confidence" />'
        '<input name="vehicle_labels" placeholder="Vehicle labels: car, truck, bus, motorcycle" />'
        '<button type="submit">Save ANPR</button>'
        '</form><p class="muted">Enable ANPR per camera and per monitoring area from Live Cameras.</p></section>'
    )


def _settings_section_recording() -> str:
    return (
        '<section class="card"><h2>Recording Clips</h2>'
        '<form id="recordingSettingsForm" class="form-grid">'
        '<input name="motion_min_confidence" type="number" min="0" max="1" step="0.01" placeholder="Motion min confidence" />'
        '<input name="pre_event_seconds" type="number" min="0" max="300" placeholder="Pre-event seconds" />'
        '<input name="post_event_seconds" type="number" min="0" max="300" placeholder="Post-event seconds" />'
        '<input name="extension_step_seconds" type="number" min="0" max="300" placeholder="Extension step seconds" />'
        '<input name="max_clip_seconds" type="number" min="1" max="3600" placeholder="Max clip seconds" />'
        '<input name="format" placeholder="Format: mp4" />'
        '<button type="submit">Save Clip Settings</button>'
        '</form><p class="muted">Alert-triggered and continuous recording are configured per camera above or from Live Cameras.</p></section>'
    )


def _settings_section_retention() -> str:
    return (
        '<section class="card"><h2>Retention</h2>'
        '<form id="retentionSettingsForm" class="form-grid">'
        '<label><span>Auto Purge</span><select name="auto_purge_enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label>'
        '<input name="retention_days" type="number" min="1" max="3650" placeholder="Retention days" />'
        '<input name="max_storage_gb" type="number" min="1" max="100000" placeholder="Max storage GB" />'
        '<button type="submit">Save Retention</button>'
        '</form><button id="purgeRecordingsBtn" class="secondary" type="button">Run Purge Now</button></section>'
    )


def _settings_section_storage() -> str:
    return (
        '<section class="card"><h2>Storage</h2>'
        '<form id="storageSettingsForm" class="form-grid">'
        '<input name="data_dir" placeholder="Data directory" />'
        '<input name="snapshots_dir" placeholder="Snapshots directory" />'
        '<input name="events_dir" placeholder="Events directory" />'
        '<input name="recordings_dir" placeholder="Recordings directory" />'
        '<input name="plates_dir" placeholder="Plate images directory" />'
        '<button type="submit">Save Storage</button>'
        '</form></section>'
    )


def _settings_section_auth() -> str:
    return (
        '<section class="card"><h2>Login Security</h2>'
        '<form id="authSettingsForm" class="form-grid">'
        '<input name="session_timeout_hours" type="number" min="0.25" max="720" step="0.25" placeholder="Session timeout hours" />'
        '<input name="max_login_attempts" type="number" min="1" max="100" placeholder="Max login attempts" />'
        '<input name="lockout_minutes" type="number" min="1" max="1440" placeholder="Lockout minutes" />'
        '<button type="submit">Save Login Security</button>'
        '</form></section>'
    )


@app.get('/settings')
def system_settings_page():
    sections = ''.join([
        _settings_section_anpr(),
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
        '</main><script src="/static/nav.js"></script><script src="/static/settings.js"></script></body></html>'
    )
    return HTMLResponse(html)

@app.get('/users')
def users_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Users Ã‚Â· Daygle AI Camera</title>
<link rel="stylesheet" href="/static/styles.css" /></head><body><main class="shell"><header class="hero"><div><p class="eyebrow">Administration</p><h1>User Management</h1><p class="muted">Create users, change roles, disable accounts, and reset passwords.</p></div></header><section class="card"><div id="userMessage" class="muted"></div><div id="users" class="list"></div></section><section class="card"><h2>Create User</h2><form id="createUserForm" class="form-grid"><input name="username" placeholder="Username" required /><select name="role"><option value="viewer">Viewer</option><option value="admin">Admin</option></select><input name="password" type="password" placeholder="Temporary Password" required /><button>Create</button></form></section></main><script src="/static/users.js"></script></body></html>""")


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
def live_snapshot(overlay: bool = Query(True), camera_id: str | None = None):
    selected_camera = get_camera_instance(camera_id)
    selected_config = get_camera_config(camera_id)
    if hasattr(selected_camera, 'read_jpeg'):
        try:
            image_bytes, frame = selected_camera.read_jpeg()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        queue_live_stream_alerts(image_bytes, frame, copy.deepcopy(selected_config))
        if overlay:
            camera_key = str(selected_config.get('id') or 'camera')
            detections = live_detection_status.get(camera_key, {}).get('detections') or []
            image_bytes = render_live_snapshot_jpeg_overlay(image_bytes, detections)
        return Response(content=image_bytes, media_type='image/jpeg')
    raise HTTPException(status_code=503, detail='Live snapshots require an ONVIF/RTSP camera backend.')


@app.post('/api/detect/test-image')
async def detect_test_image(request: Request):
    image_bytes, filename, content_type = await _read_uploaded_image(request)
    if not image_bytes:
        raise HTTPException(status_code=400, detail='Uploaded image is empty')

    ai_settings = effective_ai_config()
    ai_state = ai_status_payload(ai_settings)
    if ai_settings.get('backend') == 'onnx' and not ai_state['detector_loaded']:
        raise HTTPException(status_code=400, detail=ai_state['last_detector_error'] or 'ONNX detector is not loaded.')
    try:
        detections = detector.detect_image(image_bytes)
    except DetectorUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    snapshot_path = storage.save_image_snapshot(image_bytes, filename)
    alerts.rules = effective_alert_rules()
    triggered = alerts.process(detections)
    event_time = datetime.now(timezone.utc).isoformat()
    event_id = database.add_event(
        created_at=event_time,
        source='test-image',
        snapshot_path=snapshot_path,
        detections=detections,
        alert_triggered=bool(triggered),
        metadata={
            'filename': filename,
            'content_type': content_type,
            'ai_backend': ai_state['configured_backend'],
            'detector_backend': ai_state['active_backend'],
        },
    )

    recording_id = attach_event_recording(event_id, event_time, 'test-image', detections)
    plate_events = process_anpr_for_event(event_id, detections, snapshot_path, event_time)

    for alert in triggered:
        database.add_alert(
            created_at=datetime.now(timezone.utc).isoformat(),
            rule_name=alert['rule_name'],
            event_id=event_id,
            label=alert['label'],
            confidence=alert['confidence'],
            message=alert['message'],
        )
    deliver_email_alerts(triggered, event_id)

    return {
        'created': True,
        'event_id': event_id,
        'recording_id': recording_id,
        'detections': detections,
        'alerts': triggered,
        'plate_events': plate_events,
        'snapshot_path': snapshot_path,
        'ai_backend': ai_state['configured_backend'],
        'backend_used': ai_state['configured_backend'],
        'detector_backend': ai_state['active_backend'],
    }


@app.get('/api/events')
def events(label: str | None = None, limit: int = Query(50, ge=1, le=200), alerted_only: bool = False):
    return database.search_events(label=label, limit=limit, alerted_only=alerted_only)


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
    return {'ok': True}


@app.delete('/api/events')
def delete_all_events(request: Request):
    require_admin(request)
    deleted = database.delete_all_events()
    return {'ok': True, 'deleted': deleted}


@app.get('/api/alerts')
def alert_history(limit: int = Query(25, ge=1, le=200)):
    return database.alerts(limit=limit)


@app.delete('/api/alerts')
def delete_all_alert_history(request: Request):
    require_admin(request)
    deleted = database.delete_all_alerts()
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
        'anpr': effective_anpr_config(),
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
            'plates_dir': effective_storage_config().get('plates_dir'),
        },
        'live': effective_live_config(),
        'recording': effective_recording_config(),
    }


@app.get('/api/recordings')
def recordings(label: str | None = None, limit: int = Query(50, ge=1, le=200), alerted_only: bool = False):
    return database.list_recordings(label=label, limit=limit, alerted_only=alerted_only)


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

    trigger_type = str(recording.get('trigger_type') or 'motion').strip().lower() or 'motion'
    trigger_label = str(recording.get('trigger_label') or '').strip().lower() or None
    if trigger_type in {'motion', 'continuous', 'none', 'off'}:
        display_label = trigger_type
    elif trigger_type == 'human':
        display_label = 'person'
    else:
        display_label = trigger_label or trigger_type

    return {
        **recording,
        'timeline_start_seconds': max(0.0, (visible_start - day_start).total_seconds()),
        'timeline_end_seconds': min(86400.0, (visible_end - day_start).total_seconds()),
        'timeline_duration_seconds': max(1.0, (visible_end - visible_start).total_seconds()),
        'color_key': display_label,
        'color_label': display_label,
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
    if not cameras:
        raise HTTPException(status_code=404, detail='No cameras configured')

    selected_camera_id = normalize_camera_id(camera_id or cameras[0]['id'])
    selected_camera = next((camera for camera in cameras if camera['id'] == selected_camera_id), None)
    if selected_camera is None:
        raise HTTPException(status_code=404, detail='Camera not found')

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
    return {
        'camera': selected_camera,
        'cameras': cameras,
        'day': target_day.isoformat(),
        'day_start': day_start.isoformat(),
        'day_end': day_end.isoformat(),
        'timeline_timezone_offset_minutes': tz_offset_minutes if tz_offset_minutes is not None else 0,
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


@app.delete('/api/recordings/{recording_id}')
def delete_recording(recording_id: int, request: Request):
    require_admin(request)
    recording = database.delete_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    delete_recording_files([recording])
    return {'ok': True}


@app.delete('/api/recordings')
def delete_all_recordings(request: Request):
    require_admin(request)
    recordings = database.delete_all_recordings()
    delete_recording_files(recordings)
    return {'ok': True, 'deleted': len(recordings)}


@app.get('/api/users')
def list_users(request: Request):
    require_user(request)
    return auth.list_users()


@app.post('/api/users')
async def create_user(request: Request):
    payload = await request.json()
    try:
        return auth.create_user(payload.get('username', ''), payload.get('password', ''), payload.get('role', 'viewer'))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch('/api/users/{user_id}')
async def update_user(user_id: int, request: Request):
    payload = await request.json()
    try:
        return auth.update_user(user_id, role=payload.get('role'), is_active=payload.get('is_active'), password=payload.get('password'))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _event_camera_id_from_plate_event(plate_event: dict[str, Any]) -> str | None:
    event = plate_event.get('event') if isinstance(plate_event, dict) else None
    if not isinstance(event, dict):
        return None
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    raw_camera_id = metadata.get('camera_id') or event.get('camera_id')
    return normalize_camera_id(raw_camera_id) if raw_camera_id else None


def _filter_plate_events_by_camera(events: list[dict[str, Any]], camera_id: str | None) -> list[dict[str, Any]]:
    if not camera_id:
        return events
    target = normalize_camera_id(camera_id)
    return [event for event in events if _event_camera_id_from_plate_event(event) == target]


def _apply_camera_filter_to_plate(plate: dict[str, Any], camera_id: str | None) -> dict[str, Any]:
    if not camera_id:
        return plate
    filtered_events = _filter_plate_events_by_camera(plate.get('events', []), camera_id)
    filtered = dict(plate)
    filtered['events'] = filtered_events
    filtered['sighting_count'] = len(filtered_events)
    timestamps = [str(event.get('created_at') or '') for event in filtered_events if event.get('created_at')]
    if timestamps:
        filtered['first_seen'] = min(timestamps)
        filtered['last_seen'] = max(timestamps)
    else:
        filtered['first_seen'] = None
        filtered['last_seen'] = None
    return filtered


@app.get('/api/plates')
def list_plates(limit: int = Query(50, ge=1, le=200), camera_id: str | None = None):
    if not camera_id:
        return database.list_plates(limit=limit)
    plates = database.list_plates(limit=200)
    filtered: list[dict[str, Any]] = []
    for plate in plates:
        detailed = database.get_plate(int(plate['id']))
        if detailed is None:
            continue
        camera_filtered = _apply_camera_filter_to_plate(detailed, camera_id)
        if camera_filtered.get('sighting_count', 0) <= 0:
            continue
        camera_filtered.pop('events', None)
        filtered.append(camera_filtered)
        if len(filtered) >= limit:
            break
    return filtered


@app.get('/api/plates/search')
def search_plates(q: str = '', limit: int = Query(50, ge=1, le=200), camera_id: str | None = None):
    events = database.search_plates(normalize_plate(q), limit=limit)
    return _filter_plate_events_by_camera(events, camera_id)


@app.post('/api/plates/whitelist')
async def whitelist_plate(request: Request):
    require_admin(request)
    payload = await request.json()
    plate_number = normalize_plate(payload.get('plate_number') or '')
    if not plate_number:
        raise HTTPException(status_code=400, detail='plate_number is required.')
    return database.update_plate_status(plate_number, notes=payload.get('notes'), is_whitelisted=True, is_blacklisted=False)


@app.post('/api/plates/blacklist')
async def blacklist_plate(request: Request):
    require_admin(request)
    payload = await request.json()
    plate_number = normalize_plate(payload.get('plate_number') or '')
    if not plate_number:
        raise HTTPException(status_code=400, detail='plate_number is required.')
    return database.update_plate_status(plate_number, notes=payload.get('notes'), is_whitelisted=False, is_blacklisted=True)


@app.get('/api/plates/{plate_id}')
def get_plate(plate_id: int, camera_id: str | None = None):
    plate = database.get_plate(plate_id)
    if plate is None:
        raise HTTPException(status_code=404, detail='Plate not found')
    return _apply_camera_filter_to_plate(plate, camera_id)


@app.delete('/api/plates/{plate_id}')
def delete_plate(plate_id: int, request: Request):
    require_admin(request)
    plate = database.delete_plate(plate_id)
    if plate is None:
        raise HTTPException(status_code=404, detail='Plate not found')
    return {'ok': True}


@app.delete('/api/plates')
def delete_all_plates(request: Request):
    require_admin(request)
    deleted = database.delete_all_plates()
    return {'ok': True, 'deleted': deleted}


@app.get('/api/plate-alerts')
def list_plate_alerts():
    return database.list_plate_alert_rules()


@app.post('/api/plate-alerts')
async def create_plate_alert(request: Request):
    require_admin(request)
    return database.create_plate_alert_rule(validate_plate_alert_rule(await request.json()), utc_now())


@app.put('/api/plate-alerts/{rule_id}')
async def update_plate_alert(rule_id: int, request: Request):
    require_admin(request)
    rule = database.update_plate_alert_rule(rule_id, validate_plate_alert_rule(await request.json(), partial=True), utc_now())
    if rule is None:
        raise HTTPException(status_code=404, detail='Plate alert rule not found')
    return rule


@app.delete('/api/plate-alerts/{rule_id}')
def delete_plate_alert(rule_id: int, request: Request):
    require_admin(request)
    if not database.delete_plate_alert_rule(rule_id):
        raise HTTPException(status_code=404, detail='Plate alert rule not found')
    return {'ok': True}


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
        'categories': ai_settings.get('categories', config.get('ai', {}).get('categories', [])),
    }


def available_labels() -> list[str]:
    ai_settings = effective_ai_config()
    labels = load_labels(ai_settings.get('labels_path'), ai_settings.get('categories', []))
    return labels or list(ai_settings.get('categories', []))


def validate_alert_rule(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    rule: dict[str, Any] = {}
    required = ('name', 'object')
    for field in required:
        if not partial and not str(payload.get(field, '')).strip():
            raise HTTPException(status_code=400, detail=f'{field} is required.')
    for field in ('name', 'object'):
        if field in payload:
            value = str(payload.get(field, '')).strip()
            if not value:
                raise HTTPException(status_code=400, detail=f'{field} cannot be blank.')
            rule[field] = value
    if 'min_confidence' in payload or not partial:
        try:
            value = float(payload.get('min_confidence', 0.5))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='min_confidence must be a number.') from exc
        if not 0 <= value <= 1:
            raise HTTPException(status_code=400, detail='min_confidence must be between 0 and 1.')
        rule['min_confidence'] = value
    if 'cooldown_seconds' in payload or not partial:
        try:
            value = int(payload.get('cooldown_seconds', 60))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='cooldown_seconds must be an integer.') from exc
        if value < 0:
            raise HTTPException(status_code=400, detail='cooldown_seconds cannot be negative.')
        rule['cooldown_seconds'] = value
    if 'enabled' in payload or not partial:
        rule['enabled'] = bool(payload.get('enabled', True))
    if 'email_enabled' in payload or not partial:
        rule['email_enabled'] = bool(payload.get('email_enabled', False))
    if 'email_recipients' in payload or not partial:
        raw_recipients = payload.get('email_recipients', [])
        if isinstance(raw_recipients, str):
            recipients = [value.strip() for value in raw_recipients.split(',') if value.strip()]
        elif isinstance(raw_recipients, list):
            recipients = [str(value).strip() for value in raw_recipients if str(value).strip()]
        else:
            raise HTTPException(status_code=400, detail='email_recipients must be a list or comma-separated string.')
        for recipient in recipients:
            if '@' not in recipient:
                raise HTTPException(status_code=400, detail='Email recipients must be valid email addresses.')
        rule['email_recipients'] = recipients
    for field in ('active_start', 'active_end'):
        if field in payload:
            value = payload.get(field) or None
            if value is not None and not re.fullmatch(r'\d{2}:\d{2}', str(value)):
                raise HTTPException(status_code=400, detail=f'{field} must use HH:MM format.')
            rule[field] = value
    return rule


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


def validate_plate_alert_rule(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    rule: dict[str, Any] = {}
    if not partial and not str(payload.get('rule_name', '')).strip():
        raise HTTPException(status_code=400, detail='rule_name is required.')
    if 'rule_name' in payload:
        value = str(payload.get('rule_name') or '').strip()
        if not value:
            raise HTTPException(status_code=400, detail='rule_name cannot be blank.')
        rule['rule_name'] = value
    if 'rule_type' in payload or not partial:
        rule_type = str(payload.get('rule_type', 'plate')).lower()
        if rule_type not in {'plate', 'unknown', 'blacklisted'}:
            raise HTTPException(status_code=400, detail='rule_type must be plate, unknown, or blacklisted.')
        rule['rule_type'] = rule_type
    if 'plate_pattern' in payload or not partial:
        pattern = normalize_plate(payload.get('plate_pattern') or '')
        if rule.get('rule_type', payload.get('rule_type')) == 'plate' and not pattern:
            raise HTTPException(status_code=400, detail='plate_pattern is required for specific plate rules.')
        rule['plate_pattern'] = pattern or None
    if 'enabled' in payload or not partial:
        rule['enabled'] = _bool_value(payload.get('enabled', True))
    if 'cooldown_seconds' in payload or not partial:
        rule['cooldown_seconds'] = _int_field(payload, 'cooldown_seconds', 60, 0, 86400)
    return rule


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


def _float_field(payload: dict[str, Any], field: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(payload.get(field, default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f'{field} must be a number.') from exc
    if value < minimum or value > maximum:
        raise HTTPException(status_code=400, detail=f'{field} must be between {minimum} and {maximum}.')
    return value


def validate_camera_settings(payload: dict[str, Any], current: dict[str, Any] | None = None, index: int = 1) -> dict[str, Any]:
    current = current or effective_camera_config()
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
    detection['anpr_enabled'] = _bool_value(detection.get('anpr_enabled', True))
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
    if not raw_cameras:
        raise HTTPException(status_code=400, detail='At least one camera is required.')
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

def validate_anpr_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_anpr_config()
    merged = {**current, **payload}
    backend = str(merged.get('backend', 'paddleocr')).lower()
    if backend not in {'paddleocr', 'easyocr'}:
        raise HTTPException(status_code=400, detail='ANPR backend must be paddleocr or easyocr.')
    try:
        min_confidence = float(merged.get('min_confidence', 0.75))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='ANPR min_confidence must be a number.') from exc
    if not 0 <= min_confidence <= 1:
        raise HTTPException(status_code=400, detail='ANPR min_confidence must be between 0 and 1.')
    raw_labels = merged.get('vehicle_labels', [])
    if isinstance(raw_labels, str):
        vehicle_labels = [label.strip().lower() for label in raw_labels.split(',') if label.strip()]
    elif isinstance(raw_labels, list):
        vehicle_labels = [str(label).strip().lower() for label in raw_labels if str(label).strip()]
    else:
        raise HTTPException(status_code=400, detail='vehicle_labels must be a list or comma-separated string.')
    return {
        'enabled': _bool_value(merged.get('enabled', True)),
        'backend': backend,
        'min_confidence': min_confidence,
        'vehicle_labels': vehicle_labels or ['car', 'truck', 'bus', 'motorcycle'],
    }


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
        'motion_min_confidence': _float_field(merged, 'motion_min_confidence', 0.45, 0.0, 1.0),
        'pre_event_seconds': _int_field(merged, 'pre_event_seconds', 5, 0, 300),
        'post_event_seconds': _int_field(merged, 'post_event_seconds', 10, 0, 300),
        'extension_step_seconds': _int_field(merged, 'extension_step_seconds', 10, 0, 300),
        'max_clip_seconds': _int_field(merged, 'max_clip_seconds', 60, 1, 3600),
        'format': fmt,
        'retention_days': _int_field(merged, 'retention_days', 14, 1, 3650),
        'max_storage_gb': _int_field(merged, 'max_storage_gb', 20, 1, 100000),
        'auto_purge_enabled': _bool_value(merged.get('auto_purge_enabled', True)),
    }


def validate_storage_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = effective_storage_config()
    updated = {key: str(current.get(key) or '') for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir', 'plates_dir', 'database')}
    for key in ('data_dir', 'snapshots_dir', 'events_dir', 'recordings_dir', 'plates_dir'):
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
    if event_debounce_seconds < 0 or event_debounce_seconds > 120:
        raise HTTPException(status_code=400, detail='event_debounce_seconds must be between 0 and 120.')
    return {
        'snapshot_refresh_ms': snapshot_refresh_ms,
        'detection_status_refresh_ms': detection_status_refresh_ms,
        'detection_interval_seconds': detection_interval_seconds,
        'event_debounce_seconds': event_debounce_seconds,
        'background_detection_enabled': background_detection_enabled,
    }


def apply_cameras_settings(settings_list: list[dict[str, Any]]) -> None:
    global camera, camera_config, cameras_config, camera_instances
    cameras_config = settings_list
    camera_config = settings_list[0]
    camera_instances = create_camera_instances(settings_list)
    camera = camera_instances[camera_config['id']]


def apply_camera_settings(settings: dict[str, Any]) -> None:
    apply_cameras_settings([settings])


def apply_storage_and_recording_settings() -> None:
    global storage, recording_service
    storage = Storage({**config, 'storage': effective_storage_config()})
    recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})


def apply_anpr_settings() -> None:
    global anpr_pipeline
    anpr_pipeline = AnprPipeline(effective_anpr_config())


@app.get('/api/settings/ai')
def get_ai_settings():
    return detector_status(effective_ai_config())


def reload_detector(ai_settings: dict[str, Any]) -> tuple[bool, str | None]:
    global detector, last_detector_error
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
    payload = await request.json()
    new_settings = validate_ai_settings(payload)
    database.set_setting('ai', new_settings, utc_now())
    reloaded, error = reload_detector(new_settings)
    response = detector_status(new_settings)
    response['reload_succeeded'] = reloaded
    response['reload_error'] = error
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


def export_yolov8n_onnx(destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        '-c',
        (
            'from ultralytics import YOLO\n'
            f"model = YOLO('{YOLOV8N_MODEL}')\n"
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

    exported = destination.parent / YOLOV8N_ONNX
    if exported != destination and exported.exists():
        exported.replace(destination)
    if not destination.exists():
        details = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(details or f'Ultralytics export did not create {destination.name}.')
    if destination.stat().st_size <= 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError('Exported model file is empty.')
    return destination.stat().st_size


@app.post('/api/settings/ai/download-yolov8n')
def download_yolov8n_model():
    ai_settings = effective_ai_config()
    destination = BASE_DIR / 'models' / YOLOV8N_ONNX
    try:
        exported_bytes = export_yolov8n_onnx(destination)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - environment dependent
        raise HTTPException(
            status_code=502,
            detail=(
                'Failed to export YOLOv8n ONNX model. Install the export dependencies with '
                '`pip install ultralytics onnx`, then retry. '
                f'Details: {exc}'
            ),
        ) from exc

    updated = validate_ai_settings({**ai_settings, 'model_path': str(destination.relative_to(BASE_DIR))})
    database.set_setting('ai', updated, utc_now())
    reloaded, error = reload_detector(updated)
    return {
        'ok': True,
        'message': f'Exported YOLOv8n ONNX to {destination.relative_to(BASE_DIR)}.',
        'model_path': str(destination.relative_to(BASE_DIR)),
        'bytes': exported_bytes,
        'reload_succeeded': reloaded,
        'reload_error': error,
        'status': ai_status_payload(updated),
    }


@app.post('/api/settings/ai/test-detector')
def test_ai_detector():
    ai_settings = effective_ai_config()
    ai_state = ai_status_payload(ai_settings)
    if ai_settings.get('backend') == 'onnx' and not ai_state['detector_loaded']:
        raise HTTPException(status_code=400, detail=ai_state['last_detector_error'] or 'ONNX detector is not loaded.')
    if not hasattr(detector, 'detect_image'):
        raise HTTPException(status_code=400, detail='Configured detector cannot run image inference.')
    try:
        detections = detector.detect_image(ONE_PIXEL_PNG)
    except DetectorUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'backend_used': ai_state['configured_backend'], 'detections': detections, 'status': ai_state}


@app.get('/api/settings/alerts')
def get_alert_rules():
    return {'rules': database.list_alert_rules(), 'available_labels': available_labels()}


@app.get('/api/settings/alert-email')
def get_alert_email_settings():
    return effective_email_alert_settings()


@app.put('/api/settings/alert-email')
async def update_alert_email_settings(request: Request):
    payload = await request.json()
    settings = validate_alert_email_settings(payload)
    return database.set_setting('alert_email', settings, utc_now())


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


@app.get('/api/settings/system')
def get_system_settings():
    return {
        'camera': get_camera_config(None),
        'cameras': effective_cameras_config(),
        'anpr': effective_anpr_config(),
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
def backup_database():
    backup_path = create_database_backup()
    return FileResponse(
        backup_path,
        media_type='application/vnd.sqlite3',
        filename=backup_path.name,
        headers={'Cache-Control': 'no-store'},
    )


@app.post('/api/settings/system/database/restore')
async def restore_database(file: UploadFile = File(...)):
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
        return {
            'ok': True,
            'message': 'Database restored successfully.',
            'source_filename': filename,
            'safety_backup': str(safety_backup),
        }
    finally:
        restore_temp.unlink(missing_ok=True)
        await file.close()


@app.put('/api/settings/system/camera')
async def update_camera_settings(request: Request):
    settings = validate_camera_settings(await request.json())
    database.set_setting('camera', settings, utc_now())
    database.set_setting('cameras', [settings], utc_now())
    apply_camera_settings(settings)
    return settings


@app.get('/api/cameras')
def list_cameras():
    return {'cameras': effective_cameras_config()}


@app.put('/api/cameras')
async def update_cameras(request: Request):
    settings = validate_cameras_settings(await request.json())
    database.set_setting('cameras', settings, utc_now())
    database.set_setting('camera', settings[0], utc_now())
    apply_cameras_settings(settings)
    return {'cameras': settings}


@app.put('/api/cameras/{camera_id}')
async def update_camera(camera_id: str, request: Request):
    normalized = normalize_camera_id(camera_id)
    payload = await request.json()
    settings_list = list(effective_cameras_config())
    for index, current in enumerate(settings_list):
        if current.get('id') == normalized:
            settings_list[index] = validate_camera_settings({**payload, 'id': normalized}, current=current, index=index + 1)
            database.set_setting('cameras', settings_list, utc_now())
            if index == 0:
                database.set_setting('camera', settings_list[0], utc_now())
            apply_cameras_settings(settings_list)
            return settings_list[index]
    raise HTTPException(status_code=404, detail='Camera not found')


@app.get('/api/settings/anpr')
def get_anpr_settings():
    return effective_anpr_config()


@app.put('/api/settings/anpr')
async def update_anpr_settings(request: Request):
    settings = validate_anpr_settings(await request.json())
    database.set_setting('anpr', settings, utc_now())
    apply_anpr_settings()
    return settings


@app.put('/api/settings/system/anpr')
async def update_system_anpr_settings(request: Request):
    return await update_anpr_settings(request)


@app.put('/api/settings/system/live')
async def update_live_settings(request: Request):
    settings = validate_live_settings(await request.json())
    database.set_setting('live', settings, utc_now())
    return settings


@app.put('/api/settings/system/recording')
async def update_recording_settings(request: Request):
    settings = validate_recording_settings(await request.json())
    database.set_setting('recording', settings, utc_now())
    apply_storage_and_recording_settings()
    return settings


@app.put('/api/settings/system/storage')
async def update_storage_settings(request: Request):
    settings = validate_storage_settings(await request.json())
    database.set_setting('storage', settings, utc_now())
    apply_storage_and_recording_settings()
    return settings


@app.put('/api/settings/system/auth')
async def update_auth_settings(request: Request):
    settings = validate_auth_settings(await request.json())
    database.set_setting('auth', settings, utc_now())
    auth.apply_config(settings)
    return settings


@app.post('/api/settings/alerts')
async def create_alert_rule(request: Request):
    payload = await request.json()
    rule = database.create_alert_rule(validate_alert_rule(payload), utc_now())
    alerts.rules = effective_alert_rules()
    return rule


@app.put('/api/settings/alerts/{rule_id}')
async def update_alert_rule(rule_id: int, request: Request):
    payload = await request.json()
    rule = database.update_alert_rule(rule_id, validate_alert_rule(payload, partial=True), utc_now())
    if rule is None:
        raise HTTPException(status_code=404, detail='Alert rule not found')
    alerts.rules = effective_alert_rules()
    return rule


@app.delete('/api/settings/alerts/{rule_id}')
def delete_alert_rule(rule_id: int):
    if not database.delete_alert_rule(rule_id):
        raise HTTPException(status_code=404, detail='Alert rule not found')
    alerts.rules = effective_alert_rules()
    return {'ok': True}


if __name__ == '__main__':
    import uvicorn

    server_config = config.get('server', {})
    uvicorn.run(
        'app.main:app',
        host=server_config.get('host', '0.0.0.0'),
        port=int(server_config.get('port', 8080)),
        reload=False,
    )



