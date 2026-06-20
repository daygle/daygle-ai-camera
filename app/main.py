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
    effective_email_alert_settings as effective_email_alert_settings,
    effective_live_config as effective_live_config,
    effective_push_notification_settings as effective_push_notification_settings,
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
    YOLO_MODELS as YOLO_MODELS,
    active_ai_config_source as active_ai_config_source,
    ai_status_payload as ai_status_payload,
    detector_status as detector_status,
    detector_loaded_for as detector_loaded_for,
    log_detector_initialization as log_detector_initialization,
    model_exists as model_exists,
    onnx_runtime_installed as onnx_runtime_installed,
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
    _alert_datetime_prefs as _alert_datetime_prefs,
    _format_alert_datetime as _format_alert_datetime,
    _rule_notify_active_now as _rule_notify_active_now,
    compute_minimum_rule_confidence as compute_minimum_rule_confidence,
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
    _make_continuous_chunk_callback as _make_continuous_chunk_callback,
    _parse_chunk_start_time as _parse_chunk_start_time,
    _recording_capture_window as _recording_capture_window,
    attach_event_recording as attach_event_recording,
    clear_runtime_media_directory as clear_runtime_media_directory,
    delete_recording_files as delete_recording_files,
    extend_active_rtsp_recording as extend_active_rtsp_recording,
    load_recording_detection_track as load_recording_detection_track,
    recording_skip_reason as recording_skip_reason,
    recording_track_sidecar_path as recording_track_sidecar_path,
    start_rtsp_recording_capture as start_rtsp_recording_capture,
    write_live_history_detection_track as write_live_history_detection_track,
    write_recording_detection_track as write_recording_detection_track,
)

# Phase 32 + Phase-K: live-alert-monitor lifecycle + live-stream detection
# helpers extracted into app/live_monitor.py.
from app.live_monitor import (
    run_live_alert_monitor_once as run_live_alert_monitor_once,
    _prune_frame_motion_state as _prune_frame_motion_state,
    live_alert_monitor_loop as live_alert_monitor_loop,
    start_live_alert_monitor as start_live_alert_monitor,
    stop_live_alert_monitor as stop_live_alert_monitor,
    queue_live_stream_alerts as queue_live_stream_alerts,
    _encode_frame_jpeg as _encode_frame_jpeg,
    process_live_stream_alerts as process_live_stream_alerts,
)
# Phase A: pure stateless utility helpers → app/utils.py
from app.utils import (
    _non_empty_setting as _non_empty_setting,
    build_stream_url as build_stream_url,
    camera_default_name as camera_default_name,
    default_camera_detection_settings as default_camera_detection_settings,
    normalize_bool_setting as normalize_bool_setting,
    normalize_email_recipients as normalize_email_recipients,
    _parse_iso_datetime as _parse_iso_datetime,
)
# Phase D: camera diagnostics helper → app/diagnostics.py
from app.diagnostics import log_camera_diagnostic as log_camera_diagnostic
# Phase E: sound-monitor helpers → app/sound_monitor.py
from app.sound_monitor import (
    _sound_status_reason as _sound_status_reason,
    apply_sound_settings as apply_sound_settings,
    stop_sound_monitor as stop_sound_monitor,
)
# Phase F: camera-instance helpers → app/camera_instance.py
from app.camera_instance import (
    create_camera as create_camera,
    create_camera_instances as create_camera_instances,
    read_ingest_frame as read_ingest_frame,
)
# Phase H: media/ffmpeg-ffprobe helpers → app/media_utils.py
from app.media_utils import (
    mp4_has_video_stream as mp4_has_video_stream,
    mp4_is_browser_playable as mp4_is_browser_playable,
    probe_audio_codec as probe_audio_codec,
    probe_stream_codec as probe_stream_codec,
    probe_video_codec as probe_video_codec,
    probe_video_duration as probe_video_duration,
    recording_playback_sidecar_path as recording_playback_sidecar_path,
    recording_stream_path as recording_stream_path,
    transcode_recording_to_mp4 as transcode_recording_to_mp4,
)
# Phase I: YOLO model-management helpers → app/model_management.py
from app.model_management import (
    PYPI_ULTRALYTICS_URL as PYPI_ULTRALYTICS_URL,
    _installed_models_lock as _installed_models_lock,
    _installed_models_path as _installed_models_path,
    _read_installed_models as _read_installed_models,
    _write_installed_models as _write_installed_models,
    _sha256_file as _sha256_file,
    _installed_package_version as _installed_package_version,
    _fetch_ultralytics_version as _fetch_ultralytics_version,
    _parse_semver as _parse_semver,
    _fetch_models_manifest as _fetch_models_manifest,
    export_yolo_onnx as export_yolo_onnx,
    _do_download_model as _do_download_model,
)
# Phase J: database backup + recording-purge helpers → app/backup.py
from app.backup import (
    DATABASE_RESTORE_REQUIRED_TABLES as DATABASE_RESTORE_REQUIRED_TABLES,
    DATABASE_RESTORE_LOCK as DATABASE_RESTORE_LOCK,
    backup_directory as backup_directory,
    safe_backup_timestamp as safe_backup_timestamp,
    create_database_backup as create_database_backup,
    validate_restore_database as validate_restore_database,
    overwrite_database_from_file as overwrite_database_from_file,
    refresh_runtime_after_database_restore as refresh_runtime_after_database_restore,
    purge_recordings_by_policy as purge_recordings_by_policy,
    purge_camera_diagnostics_by_policy as purge_camera_diagnostics_by_policy,
)
# Sound-monitor state: Pool A rebind from app.state so that
# app.api.sound_router (and tests) can still reach these via main.<attr>.
from app.state import (
    _sound_detectors as _sound_detectors,
    _sound_detectors_lock as _sound_detectors_lock,
    _sound_statuses as _sound_statuses,
    _sound_statuses_lock as _sound_statuses_lock,
)
_logger = logging.getLogger('daygle.ai')

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
ONE_PIXEL_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'
config = load_settings()
_state.config = config
auth_config = config.get('auth', {})
_state.auth_config = auth_config
# NOTE: removed module-level auth_enabled. Routers reach it via Depends(get_auth_enabled)
# (declared by name as _state.auth_config['enabled']) or _state.auth_config direct read; see app/deps.py.

@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    removed = database.cleanup_incomplete_recordings()
    if removed:
        _logger.info(f'Cleaned up {len(removed)} incomplete recording(s) from previous session')
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
static_dir = BASE_DIR / 'web'
if static_dir.exists():
    app.mount('/static', StaticFiles(directory=static_dir), name='static')

def camera_event_recording_config(settings: dict[str, Any]) -> dict[str, Any]:
    base = effective_recording_config()
    camera_recording = normalize_camera_recording_settings(settings.get('recording'))
    base.update({'continuous': camera_recording['continuous']})
    return base

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
_state.detector = create_detector(effective_ai_config())
last_detector_error: str | None = getattr(_state.detector, 'unavailable_reason', None)
_state.last_detector_error = last_detector_error
alerts = AlertEngine([])




















































cameras_config = effective_cameras_config()
_state.cameras_config = cameras_config
camera_config = cameras_config[0] if cameras_config else {}
camera_instances = create_camera_instances(cameras_config)
_state.camera_instances = camera_instances
camera = camera_instances[camera_config['id']] if camera_config else None

def config_file_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)


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
        _logger.warning('Failed to write audit log: %s', exc)

recording_service.diagnostic_callback = log_camera_diagnostic

GITHUB_REPO = 'daygle/daygle-ai-camera'
_update_in_progress = False
_update_lock = threading.Lock()

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
            _logger.warning('Unexpected error updating camera: %s', unexpected_exc)
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
            _logger.warning('Unexpected error deleting camera: %s', unexpected_exc)

def reload_detector(ai_settings: dict[str, Any]) -> tuple[bool, str | None]:
    import app.alert_dispatch as _alert_dispatch
    global last_detector_error
    _alert_dispatch._min_rule_confidence_cache = None
    previous_detector = _state.detector
    old_session = getattr(previous_detector, 'session', None)
    if old_session is not None:
        previous_detector.session = None
        del old_session
        gc.collect()
    candidate = create_detector(ai_settings)
    candidate_error = getattr(candidate, 'unavailable_reason', None)
    if ai_settings['backend'] == 'onnx' and (not getattr(candidate, 'available', False)):
        _state.detector = previous_detector
        last_detector_error = candidate_error or 'Failed to load ONNX detector.'
        log_detector_initialization('reload_failed')
        return (False, last_detector_error)
    _state.detector = candidate
    last_detector_error = candidate_error
    log_detector_initialization('reload')
    return (True, last_detector_error)



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
