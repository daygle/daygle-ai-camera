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
import app.state as _state
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

# State-registry Pool A rebinds: background modules and tests reach these via
# ``main.<attr>``; they now live in app.state but are re-exported here so the
# contract is preserved.  Locks/dicts are mutable objects; the rebind shares
# the same object, so mutations are visible through both names.
from app.state import (
    _MOTION_FRAME_W as _MOTION_FRAME_W,
    _MOTION_FRAME_H as _MOTION_FRAME_H,
    _MOTION_PIXEL_THRESHOLD as _MOTION_PIXEL_THRESHOLD,
    _MOTION_GATE_FRACTION as _MOTION_GATE_FRACTION,
    _MOTION_SCALE_FRACTION as _MOTION_SCALE_FRACTION,
    _MOTION_BACKGROUND_ALPHA as _MOTION_BACKGROUND_ALPHA,
    PUBLIC_PREFIXES as PUBLIC_PREFIXES,
    PUBLIC_PATHS as PUBLIC_PATHS,
    ADMIN_PATHS as ADMIN_PATHS,
    MUTATING_METHODS as MUTATING_METHODS,
    _LOOPBACK as _LOOPBACK,
    live_detection_history_lock as live_detection_history_lock,
    live_detection_history as live_detection_history,
    live_detection_status_lock as live_detection_status_lock,
    live_detection_status as live_detection_status,
    live_event_last_emitted_lock as live_event_last_emitted_lock,
    live_event_last_emitted as live_event_last_emitted,
    live_detection_retry_after as live_detection_retry_after,
    live_detection_failure_count as live_detection_failure_count,
    _live_backoff_lock as _live_backoff_lock,
    live_detection_worker_lock as live_detection_worker_lock,
    active_live_detection_cameras as active_live_detection_cameras,
    live_detection_last_checked as live_detection_last_checked,
    live_alert_monitor_stop as live_alert_monitor_stop,
    live_alert_monitor_thread as live_alert_monitor_thread,
    _periodic_scan_last_ts as _periodic_scan_last_ts,
    _frame_motion_lock as _frame_motion_lock,
    _frame_motion_prev as _frame_motion_prev,
    _frame_motion_error_cameras as _frame_motion_error_cameras,
    active_rtsp_recordings_lock as active_rtsp_recordings_lock,
    active_rtsp_recordings as active_rtsp_recordings,
    _camera_health_lock as _camera_health_lock,
    _camera_health_state as _camera_health_state,
    _notification_threads_lock as _notification_threads_lock,
    _notification_threads as _notification_threads,
)
# Phase-18: top-of-file Pool A from-import rebinds for the
# camera-config cluster extracted into app/camera_config.py. The
# rebind lives in the regular app.X import section (NOT at the very
# bottom like Phase-15/16) because module-load code in main.py does
# NOT call these helpers eagerly, but ``normalize_camera_settings`` /
# ``normalize_camera_id`` are referenced as bare names from sibling
# helpers still on main.py (e.g. ``normalize_monitoring_zones``,
# ``update_cameras`` handlers) and from the camera_router + recording
# _router at runtime. Top-of-file placement ensures the rebind wires
# ``main.<name>`` BEFORE any function body is evaluated.
from app.camera_config import (
    _migrate_camera_id as _migrate_camera_id,
    _redact_camera as _redact_camera,
    normalize_camera_id as normalize_camera_id,
    normalize_camera_settings as normalize_camera_settings,
)
from app.config_facades import (
    effective_ai_config as effective_ai_config,
    effective_auth_config as effective_auth_config,
    effective_cameras_config as effective_cameras_config,
    effective_live_config as effective_live_config,
    effective_recording_config as effective_recording_config,
    effective_storage_config as effective_storage_config,
    get_camera_config as get_camera_config,
)
# Phase-19: top-of-file Pool A from-import rebinds for the
# recording-settings cluster extracted into app/recording_settings.py.
# The rebind lives in the regular app.X import section (NOT at the very
# bottom like Phase-15/16) because sibling helpers still on main.py
# `camera_event_recording_config` (the sibling helper on main.py
# that reaches these as bare names inside its function bodies; historically
# `validate_camera_settings` did too but moved to `app/payload_validators.py`
# in Phase-22). Top-of-file placement
# ensures the rebind wires ``main.<name>`` BEFORE any sibling body
# evaluates. Pool C reach sites (``main.normalize_bool_setting``,
# ``main.normalize_email_recipients``, ``main.SOUND_CLASSES``,
# ``main.DEFAULT_RULES``) are also resolved inside the moved helpers.
from app.recording_settings import (
    _migrate_legacy_camera_motion as _migrate_legacy_camera_motion,
    _normalize_camera_sound_settings as _normalize_camera_sound_settings,
    normalize_camera_ptz_settings as normalize_camera_ptz_settings,
    normalize_camera_recording_settings as normalize_camera_recording_settings,
)
# Phase-20: top-of-file Pool A from-import rebinds for the AI
# subsystem cluster extracted into app/ai_settings.py. The rebind lives
# in the regular app.X import section (NOT at the very bottom like
# Phase-15/16) because internal main.py callers (`live_detection_status_payload`,
# `process_live_stream_alerts`, `log_detector_initialization`, `detector_status`
# itself, `_do_download_model`) reference these as bare names inside function
# bodies. Top-of-file placement ensures the rebind wires ``main.<name>``
# BEFORE any sibling body evaluates. The 11 Pool C reach sites
# (main.effective_ai_config, main.detector, main.detector_loaded_for,
# main.model_exists, main.onnx_runtime_installed, main.last_detector_error,
# main.YOLO_MODELS, main.active_ai_config_source, main.config,
# main.load_labels, main.HTTPException via fastapi) are also resolved
# inside the moved helpers.
from app.ai_settings import (
    ai_status_payload as ai_status_payload,
    detector_status as detector_status,
    validate_ai_settings as validate_ai_settings,
)

# Phase-22: top-of-file Pool A from-import rebinds for the
# settings-payload-validators cluster extracted into
# app/payload_validators.py. Top-of-file placement ensures the rebind
# wires ``main.<name>`` BEFORE any sibling body evaluates. Every
# validators router (admin/settings/live/email/push) reaches these as
# bare names inside function bodies. The 16 Pool C reach sites
# (``main.effective_email_alert_settings``,
# ``main.effective_push_notification_settings``,
# ``main.normalize_bool_setting``, ``main.normalize_camera_id``,
# ``main.camera_default_name``, ``main.default_camera_detection_settings``,
# ``main.build_stream_url``, ``main.normalize_label_list``,
# ``main.normalize_monitoring_zones``, ``main._migrate_legacy_camera_motion``,
# ``main.normalize_camera_recording_settings``,
# ``main.normalize_camera_ptz_settings``, ``main.effective_recording_config``,
# ``main.effective_storage_config``, ``main.config``,
# ``main.effective_auth_config``, ``main.effective_live_config``,
# ``main.cameras_config``) are also resolved inside the moved helpers.
from app.payload_validators import (
    _int_field as _int_field,
    validate_alert_email_settings as validate_alert_email_settings,
    validate_auth_settings as validate_auth_settings,
    validate_camera_settings as validate_camera_settings,
    validate_cameras_settings as validate_cameras_settings,
    validate_live_settings as validate_live_settings,
    validate_push_notification_settings as validate_push_notification_settings,
    validate_recording_settings as validate_recording_settings,
    validate_storage_settings as validate_storage_settings,
)
# Phase-21: top-of-file Pool A from-import rebinds for the zone /
# schema normalization cluster extracted into app/zone_schema.py. The
# rebind lives in the regular app.X import section (NOT at the very
# bottom like Phase-15/16) because internal main.py callers
# reference earlier zone-schema helpers as bare names inside their function
# bodies. Top-of-file placement ensures the rebind wires ``main.<name>``
# BEFORE any sibling body evaluates.
#
# Historical drift: when Phase-21 landed, the explicit caller list
# named: (validate_camera_settings, detection_label_allowed_for_zone,
# filter_detections_for_camera_zones). Phases 22 (payload_validators) and
# 23 (zone_detection) subsequently moved all three of those callers out of
# main.py, so the bare-name caller list for this rebind is empty today.
# The rebind remains because the Pool C reach sites below still
# flow through ``main.<attr>``. The 4 current sites are:
# (``main._LABEL_ALIASES``, ``main.normalize_bool_setting``,
# ``main.normalize_email_recipients``, ``main.normalize_camera_id``) -
# resolved inside the moved helpers (app/zone_schema.py and
# app/zone_detection.py both reach them at call time via
# ``import app.main as main``).
from app.zone_schema import (
    _LABEL_ALIASES as _LABEL_ALIASES,
    normalize_label_list as normalize_label_list,
    normalize_monitoring_zones as normalize_monitoring_zones,
    normalize_zone_object_rules as normalize_zone_object_rules,
    normalize_zone_point as normalize_zone_point,
    rectangle_zone_points as rectangle_zone_points,
    zone_bounds as zone_bounds,
    zone_motion_min_confidence as zone_motion_min_confidence,
)

# Phase-23: top-of-file Pool A from-import rebinds for the
# zone-detection orchestration cluster extracted into
# app/zone_detection.py. The rebind lives in the regular app.X import
# section (NOT at the very bottom like Phase-15/16) because internal
# main.py caller (``process_live_stream_alerts`` L1067) references
# these as bare names inside its function body, and Pool C reach sites
# (``main.get_camera_config``, ``main.camera_instances``, ``main.HTTPException``,
# ``main._MOTION_FRAME_W``, ``main._MOTION_FRAME_H``,
# ``main.zone_motion_min_confidence``, ``main.normalize_label_list``,
# ``main._LABEL_ALIASES``, ``main.normalize_email_recipients``,
# ``main.normalize_bool_setting``) are resolved inside the moved helpers.
# The default-arg bindings of ``zone_motion_detections`` (gate_fraction and
# scale_fraction) and ``_zone_pixel_motion_fraction``'s body references to
# main._MOTION_FRAME_W / main._MOTION_FRAME_H rely on these constants
# being defined on ``app.main`` BEFORE the rebind block fires, which is
# true because both module-level constants are populated before the
# phase 17 row of rebinds.
from app.zone_detection import (
    _zone_pixel_motion_fraction as _zone_pixel_motion_fraction,
    detection_center_in_zone as detection_center_in_zone,
    detection_has_matching_record_rule as detection_has_matching_record_rule,
    detection_label_allowed_for_zone as detection_label_allowed_for_zone,
    detection_matches_zone as detection_matches_zone,
    detection_overlap_ratio_with_zone_rect as detection_overlap_ratio_with_zone_rect,
    filter_detections_for_camera as filter_detections_for_camera,
    filter_detections_for_camera_zones as filter_detections_for_camera_zones,
    get_camera_instance as get_camera_instance,
    normalize_detection_boxes_for_frame as normalize_detection_boxes_for_frame,
    point_in_polygon as point_in_polygon,
    point_on_segment as point_on_segment,
    zone_alert_detections as zone_alert_detections,
    zone_detection_alert_rule_names as zone_detection_alert_rule_names,
    zone_motion_detections as zone_motion_detections,
    zone_motion_record_on_detect as zone_motion_record_on_detect,
    zone_name_for_detection as zone_name_for_detection,
    zone_object_alert_rules as zone_object_alert_rules,
    zone_object_rule_matches as zone_object_rule_matches,
    zone_record_on_detect as zone_record_on_detect,
    zone_rule_name as zone_rule_name,
)

# Phase-24: top-of-file Pool A from-import rebinds for the
# camera-offline/health cluster extracted into app/camera_health.py.
# The rebind lives in the regular app.X import section (NOT at the very
# bottom like Phase-15/16) because internal main.py callers
# (``live_alert_monitor_loop`` invokes ``_check_cameras_health`` every
# cycle) reference these as bare names inside function bodies, and Pool C
# reach sites (``main._camera_health_state``, ``main._camera_health_lock``,
# ``main.log_camera_diagnostic``, ``main.cameras_config``,
# ``main.live_detection_retry_after``, ``main.logger``, ``main.database``,
# ``main.effective_push_notification_settings``,
# ``main.effective_email_alert_settings``,
# ``main.PushNotificationService``, ``main.EmailAlertService``) are
# resolved inside the moved helpers. State primitives intentionally
# STAY on app.main (state-migration template, first hybrid-pattern with
# module-level mutable state involved).
from app.camera_health import (
    _camera_offline_notification_eligible as _camera_offline_notification_eligible,
    _camera_recovery_notification_eligible as _camera_recovery_notification_eligible,
    _check_cameras_health as _check_cameras_health,
    _deliver_camera_offline_notification as _deliver_camera_offline_notification,
    _mark_camera_offline_notified as _mark_camera_offline_notified,
    _mark_camera_recovery_notified as _mark_camera_recovery_notified,
    _update_camera_health as _update_camera_health,
    effective_camera_offline_alert_settings as effective_camera_offline_alert_settings,
)
# Phase-25: top-of-file Pool A from-import rebinds for the
# pure live-snapshot rendering cluster extracted into
# app/live_snapshot.py. The rebind wires ``main.<name>`` BEFORE any
# sibling body evaluates because the internal main.py caller
# (``deliver_email_alerts`` invokes ``render_live_snapshot_jpeg_overlay``
# as a bare name at L1522) references this as a bare name inside
# its function body, and ``tests/test_api.py`` reaches them as
# ``main.render_live_snapshot_*``. No Pool C reach sites - both helpers
# are pure, depending only on stdlib + ``rectangle_zone_points`` from
# app.zone_schema (which is now their sole importer).
from app.live_snapshot import (
    render_live_snapshot_svg as render_live_snapshot_svg,
    render_live_snapshot_jpeg_overlay as render_live_snapshot_jpeg_overlay,
)
# Phase-26: top-of-file Pool A from-import rebinds for the
# live-detection state cluster extracted into app/detection_state.py.
# The rebind wires ``main.<name>`` BEFORE any sibling body evaluates
# because the internal main.py caller (``process_live_stream_alerts``
# invokes ``detect_frame_motion`` as a bare name at
# L1084) refers to these as bare names inside function bodies, and
# ``tests/test_api.py`` reaches them as
# ``main.build_track_from_live_history`` / ``main.live_detection_history``.
# The four helpers are NOT pure: they reach ``main.live_detection_*``
# locks + dicts, ``main._frame_motion_*``, ``main._MOTION_FRAME_W/H``,
# the four ``_MOTION_*`` tuning constants, ``main.effective_live_config``,
# and ``main.logger`` via lazy ``import app.main as main`` access at
# call time - mirroring the Pool-C pattern in :mod:`app.zone_schema`.
# State primitives and the ``_MOTION_*`` constants STAY on ``app.main``
# so :mod:`app.zone_detection` keeps its ``main._MOTION_FRAME_*`` /
# ``main._MOTION_GATE_FRACTION`` / ``main._MOTION_SCALE_FRACTION``
# reach sites unchanged. The four ``detect_frame_motion`` tuning
# defaults were rewired to ``None`` + call-time resolution against the
# main.py constants for the same import-order reason (see
# app.detection_state's module docstring).
from app.detection_state import (
    build_track_from_live_history as build_track_from_live_history,
    detection_label_set as detection_label_set,
    detect_frame_motion as detect_frame_motion,
    record_live_detection_history as record_live_detection_history,
)
# Phase-27: top-of-file Pool A from-import rebinds for the
# live-event debounce cluster extracted into app/event_debounce.py.
# The rebind wires ``main.<name>`` BEFORE any sibling body evaluates
# because the internal main.py callers (``process_live_stream_alerts``
# invokes ``live_event_is_debounced`` and ``remember_live_event`` as
# bare names inside its function body, ``run_live_alert_monitor_once``
# invokes ``clear_live_camera_backoff`` as a bare name) reference
# these as bare names inside function bodies, and ``tests/test_api.py``
# reaches them as ``main.live_event_is_debounced`` /
# ``main.remember_live_event`` / ``main.clear_live_camera_backoff`` AND
# mutates ``main.live_event_last_emitted`` directly. The three helpers
# are NOT pure: they reach ``main.live_event_last_emitted_lock`` +
# ``main.live_event_last_emitted``, ``main._live_backoff_lock`` +
# ``main.live_detection_failure_count`` + ``main.live_detection_retry_after``,
# ``main._frame_motion_lock`` / ``main._frame_motion_prev`` /
# ``main._frame_motion_error_cameras`` / ``main._periodic_scan_last_ts``,
# and ``main.log_camera_diagnostic`` via lazy ``import app.main as main``
# access at call time - mirroring the Pool-C pattern in
# :mod:`app.zone_schema` and :mod:`app.detection_state`. State
# primitives STAY on ``app.main`` so :mod:`tests.test_api` continues
# to read/write ``main.live_event_last_emitted`` directly, and
# :mod:`app.detection_state` keeps its ``main._frame_motion_*`` +
# ``main._periodic_scan_last_ts`` write sites unchanged.
from app.event_debounce import (
    clear_live_camera_backoff as clear_live_camera_backoff,
    live_event_is_debounced as live_event_is_debounced,
    remember_live_event as remember_live_event,
    schedule_live_camera_backoff as schedule_live_camera_backoff,
)

# Phase 28: top-of-file Pool A from-import rebinds for the
# live-alert delivery cluster extracted into app/alert_dispatch.py.
# The rebind wires ``main.<name>`` BEFORE any sibling body evaluates
# because the internal main.py callers (``process_live_stream_alerts``
# invokes the dispatch family as bare names inside its function body
# and ``tests/test_api.py`` reaches them as
# ``main.deliver_push_notifications`` /
# ``main.wait_for_pending_alert_notifications``) reference these as
# bare names inside function bodies. The five helpers reach the
# following state + helpers on ``app.main`` at call time via lazy
# ``import app.main as main`` access:
#   - ``main._notification_threads_lock`` + ``main._notification_threads``
#     (state primitives owned on main.py)
#   - ``main.database`` (singleton DB handle; NOT the app/database.py module)
#   - ``main.effective_email_alert_settings()`` +
#     ``main.effective_push_notification_settings()`` (Phase-9/10 rebinds)
#   - ``main._format_alert_datetime(...)`` +
#     ``main._rule_notify_active_now(...)`` +
#     ``main.compute_minimum_rule_confidence()`` (top-level helpers)
#   - ``main.render_live_snapshot_jpeg_overlay(...)`` (Phase-25 rebind).
# This mirrors the Pool-C pattern in :mod:`app.zone_schema`,
# :mod:`app.detection_state`, and :mod:`app.event_debounce`. State
# primitives stay on main.py (mirrors the Phase-26 ``live_detection_history``
# precedent for the in-flight delivery threads).
from app.alert_dispatch import (
    deliver_sound_alert_notifications as _deliver_sound_alert_notifications,
    wait_for_pending_alert_notifications as wait_for_pending_alert_notifications,
    deliver_alert_notifications as _deliver_alert_notifications,
    deliver_email_alerts as deliver_email_alerts,
    deliver_push_notifications as deliver_push_notifications,
)

# Phase 29: top-of-file Pool A from-import rebinds for the
# live-detection status cluster extracted into app/detection_status.py.
# The rebind wires ``main.<name>`` BEFORE any sibling body evaluates
# because the internal main.py callers (``update_live_detection_status``
# is called from many sites including ``process_live_stream_alerts``
# and ``extend_active_rtsp_recording``; ``detection_label_strings`` +
# ``detection_label_confidences`` are invoked from
# ``process_live_stream_alerts``; ``live_detection_status_payload``
# is reached from ``app/api/live_router.py`` and
# ``app/api/status_router.py`` as ``main.live_detection_status_payload``
# AND from ``tests/test_api.py`` as ``main.live_detection_status_payload``)
# reference these as bare names inside function bodies, and tests reach
# them as ``main.<name>``. The five helpers reach the following on
# ``app.main`` at call time via lazy ``import app.main as main`` access:
#   - ``main.live_detection_status_lock`` + ``main.live_detection_status``
#     (state primitives owned on main.py, exclusive to this cluster)
#   - ``main.get_camera_config`` (Phase-18 rebind from app.camera_config)
#   - ``main.ai_status_payload`` (Phase-20 rebind from app.ai_settings)
#   - ``main.build_stream_url`` (top-level helper on main.py at L588).
# This mirrors the Pool-C pattern in :mod:`app.zone_schema`,
# :mod:`app.detection_state`, :mod:`app.event_debounce`, and
# :mod:`app.alert_dispatch`. State primitives stay on main.py (mirrors
# the Phase-26 ``live_detection_history_lock`` precedent).
#
# Skipped from this extraction (left in main.py): ``extend_active_rtsp_recording``
# (recording extension cluster, future Phase-30+) and
# ``schedule_live_camera_backoff`` (backoff scheduling, related to
# Phase-27 event_debounce but tightly coupled to recording expiry
# logic; defer).
from app.detection_status import (
    update_live_detection_status as update_live_detection_status,
    detection_label_strings as detection_label_strings,
    detection_label_confidences as detection_label_confidences,
    live_detection_status_payload as live_detection_status_payload,
    _camera_has_live_alert_stream as _camera_has_live_alert_stream,
)

# Phase 30: top-of-file Pool A from-import rebinds for the
# recording-extension cluster extracted into app/recording_extension.py.
# The rebind wires ``main.<name>`` BEFORE any sibling body evaluates
# because the internal main.py callers (``extend_active_rtsp_recording``
# is invoked from ``process_live_stream_alerts`` (around L1149) and
# another recording-orchestration helper (around L1331); the track trio
# is invoked from playback + main stream-loop sites) AND the external
# callers (``app/api/recordings_router.py`` reaches
# ``main.load_recording_detection_track`` + ``main.recording_track_sidecar_path``,
# ``tests/test_api.py`` extensively tests all 4 helpers via
# ``main.<name>`` reach) reference these as bare names inside function
# bodies. The four helpers reach the following on ``app.main`` at call
# time via lazy ``import app.main as main`` access:
#   - ``main.active_rtsp_recordings`` + ``main.active_rtsp_recordings_lock``
#     (state primitives owned on main.py, exclusive to this cluster)
#   - ``main.database`` (singleton DB handle on main.py)
#   - ``main.recording_service`` (RecordingService singleton on main.py)
#   - ``main.effective_recording_config`` (Phase-17 rebind from
#     app.config_facades)
#   - ``main.detection_label_strings`` + ``main.detection_label_confidences``
#     (Phase-29 rebinds from app.detection_status).
# The track trio has zero Pool-C reach - it resolves
# ``recording_track_sidecar_path`` as a bare local name inside the new
# module. This mirrors the Pool-C pattern in :mod:`app.zone_schema`,
# :mod:`app.detection_state`, :mod:`app.event_debounce`,
# :mod:`app.alert_dispatch`, and :mod:`app.detection_status`. State
# primitives stay on main.py (mirrors the Phase-26
# ``live_detection_history_lock`` + Phase-27
# ``_notification_threads_lock`` + Phase-29
# ``live_detection_status_lock`` precedent for in-flight
# recording + helper state).
#
# Skipped from this extraction (left in main.py): ``schedule_live_camera_backoff``
# (backoff scheduling + recording-extension overlap, future Phase-31+).
from app.recording_extension import (
    extend_active_rtsp_recording as extend_active_rtsp_recording,
    recording_track_sidecar_path as recording_track_sidecar_path,
    write_recording_detection_track as write_recording_detection_track,
    load_recording_detection_track as load_recording_detection_track,
)

# Phase 32: top-of-file Pool A from-import rebinds for the
# live-alert-monitor lifecycle cluster extracted into app/live_monitor.py.
from app.live_monitor import (
    run_live_alert_monitor_once as run_live_alert_monitor_once,
    _prune_frame_motion_state as _prune_frame_motion_state,
    live_alert_monitor_loop as live_alert_monitor_loop,
    start_live_alert_monitor as start_live_alert_monitor,
    stop_live_alert_monitor as stop_live_alert_monitor,
)
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
_state.config = config
auth_config = config.get('auth', {})
_state.auth_config = auth_config
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

def camera_event_recording_config(settings: dict[str, Any]) -> dict[str, Any]:
    base = effective_recording_config()
    camera_recording = normalize_camera_recording_settings(settings.get('recording'))
    base.update({'continuous': camera_recording['continuous']})
    return base

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
_state.database = database
camera_config: dict[str, Any] = {}
_state.camera_config = camera_config
cameras_config: list[dict[str, Any]] = []
_state.cameras_config = cameras_config
camera_instances: dict[str, Any] = {}
_state.camera_instances = camera_instances
camera = None
storage = Storage({**config, 'storage': effective_storage_config()})
recording_service = RecordingService({**config, 'storage': effective_storage_config(), 'recording': effective_recording_config()})
_state.recording_service = recording_service
auth = AuthService(config['storage']['database'], effective_auth_config())
_state.auth = auth
SESSION_COOKIE_NAME = str(effective_auth_config().get('cookie_name', SESSION_COOKIE))
detector = create_detector(effective_ai_config())
_state.detector = detector
last_detector_error: str | None = getattr(detector, 'unavailable_reason', None)
_state.last_detector_error = last_detector_error
alerts = AlertEngine([])
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


def default_camera_detection_settings() -> dict[str, Any]:
    return {'object_detection_enabled': True, 'zones': []}

def normalize_bool_setting(value: Any, default: bool=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


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
_state.cameras_config = cameras_config
camera_config = cameras_config[0] if cameras_config else {}
camera_instances = create_camera_instances(cameras_config)
_state.camera_instances = camera_instances
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


def log_detector_initialization(context: str='startup') -> None:
    ai_status = ai_status_payload()
    active_providers = getattr(detector, 'active_providers', None)
    providers_str = ','.join(active_providers) if active_providers else '<none>'
    logger.info('AI detector %s: active_backend=%s configured_backend=%s model_loaded=%s inference_available=%s providers=%s model_path=%s labels_path=%s error=%s', context, ai_status['active_backend'], ai_status['configured_backend'], ai_status['model_loaded'], ai_status['inference_available'], providers_str, ai_status['model_path'] or '<none>', ai_status['labels_path'] or '<none>', ai_status['error'] or '<none>')

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













def apply_cameras_settings(settings_list: list[dict[str, Any]]) -> None:
    global camera, camera_config, cameras_config, camera_instances
    old_instances = camera_instances
    cameras_config = settings_list
    _state.cameras_config = cameras_config
    camera_config = settings_list[0] if settings_list else {}
    _state.camera_config = camera_config
    camera_instances = create_camera_instances(settings_list)
    _state.camera_instances = camera_instances
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
    _state.recording_service = recording_service
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


def _current_version() -> str:
    version_file = BASE_DIR / 'VERSION'
    return version_file.read_text(encoding='utf-8').strip() if version_file.exists() else 'unknown'
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
from app.api.admin_router import router as admin_router
app.include_router(admin_router)
from app.api.camera_log_router import router as camera_log_router
app.include_router(camera_log_router)
from app.api.update_router import router as update_router
app.include_router(update_router)
from app.api.utility_router import router as utility_router
app.include_router(utility_router)
from app.api.web_router import router as web_router
app.include_router(web_router)
from app.api.auth_router import router as auth_router
app.include_router(auth_router)
from app.api.web_router import login_page as login_page
from app.api.web_router import setup_page as setup_page
_WEB_ROUTER_PAGE_ALIASES = (login_page, setup_page)
from app.middleware import authentication_middleware, app_navigation_middleware
app.middleware('http')(authentication_middleware)
app.middleware('http')(app_navigation_middleware)
from app.auth_gates import _request_ip as _request_ip, require_admin as require_admin, require_session as require_session, require_user as require_user
# Pool A back-compat rebinds for helpers moved to app/auth_helpers.py and
# app/request_helpers.py.  Sibling modules (middleware, tests) still reach
# them as ``main.<name>``; these rebinds keep that working without requiring
# every caller to be updated simultaneously.
from app.auth_helpers import auth_page as auth_page
from app.auth_helpers import csrf_token_response as csrf_token_response
from app.auth_helpers import set_csrf_cookie as set_csrf_cookie
from app.auth_helpers import set_session_cookie as set_session_cookie
from app.auth_helpers import clear_auth_cookies as clear_auth_cookies
from app.request_helpers import form_data as form_data
