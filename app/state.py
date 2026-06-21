"""Application-scoped singleton and shared-state registry.

All modules that need access to runtime singletons, shared thread-safe state,
or compile-time constants without pulling in ``app.main`` import this module
directly.

Rules:
- No imports from other ``app.*`` modules (only stdlib + TYPE_CHECKING).
- Module-level declarations only; no functions.
- Singletons are set to ``None`` here and populated by ``app.main`` after
  each object is constructed.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.database import EventDatabase
    from app.auth import AuthService
    from app.recordings import RecordingService

# ---------------------------------------------------------------------------
# Compile-time constants (pure values; safe to import at module top-level)
# ---------------------------------------------------------------------------

# Motion-detection frame geometry and thresholds (used as function default-arg
# values in app.zone_detection and app.detection_state at import time).
_MOTION_FRAME_W: int = 160
_MOTION_FRAME_H: int = 120
_MOTION_PIXEL_THRESHOLD: int = 30
_MOTION_GATE_FRACTION: float = 0.003
_MOTION_SCALE_FRACTION: float = 0.1
_MOTION_BACKGROUND_ALPHA: float = 0.05

# Middleware / auth constants (moved from app.main so app.middleware can
# import them at module top-level without a circular import).
PUBLIC_PREFIXES: tuple[str, ...] = ('/static/',)
PUBLIC_PATHS: frozenset[str] = frozenset({'/favicon.ico', '/login', '/setup'})
ADMIN_PATHS: frozenset[str] = frozenset({
    '/onnx', '/yamnet-tflite', '/ai', '/cameras', '/settings',
    '/users', '/zones', '/sounds', '/audit', '/camera-log',
})
MUTATING_METHODS: frozenset[str] = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})

# Auth-gates loopback set (moved from app.main so app.auth_gates can import
# at module top-level without a circular import).
_LOOPBACK: frozenset[str] = frozenset({'127.0.0.1', '::1', 'localhost'})

# ---------------------------------------------------------------------------
# Lifespan-initialized singletons (None until app.main populates them)
# ---------------------------------------------------------------------------

database: Any = None        # EventDatabase instance
auth: Any = None            # AuthService instance
recording_service: Any = None  # RecordingService instance
detector: Any = None        # AI detector (OnnxDetector or stub)
last_detector_error: str | None = None

# ---------------------------------------------------------------------------
# Startup-initialized config snapshots (populated by app.main at module load)
# ---------------------------------------------------------------------------

config: dict = {}           # on-disk YAML config; static after startup
auth_config: dict = {}      # pre-stripped snapshot of config['auth']

# ---------------------------------------------------------------------------
# Camera runtime state (reassigned by app.main.apply_cameras_settings)
# ---------------------------------------------------------------------------

cameras_config: list = []
camera_instances: dict = {}
_camera_instances_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Live-detection shared state (locks + associated dicts)
# ---------------------------------------------------------------------------

live_detection_history_lock: threading.Lock = threading.Lock()
live_detection_history: dict = {}

live_detection_status_lock: threading.Lock = threading.Lock()
live_detection_status: dict = {}

live_event_last_emitted_lock: threading.Lock = threading.Lock()
live_event_last_emitted: dict = {}

live_detection_retry_after: dict = {}
live_detection_failure_count: dict = {}
_live_backoff_lock: threading.Lock = threading.Lock()

live_detection_worker_lock: threading.Lock = threading.Lock()
active_live_detection_cameras: set = set()
live_detection_last_checked: dict = {}

live_alert_monitor_stop: threading.Event = threading.Event()
live_alert_monitor_thread: threading.Thread | None = None

_periodic_scan_last_ts: dict = {}

# ---------------------------------------------------------------------------
# Frame-motion shared state
# ---------------------------------------------------------------------------

_frame_motion_lock: threading.Lock = threading.Lock()
_frame_motion_prev: dict = {}
_frame_motion_error_cameras: set = set()

# ---------------------------------------------------------------------------
# Active RTSP recordings
# ---------------------------------------------------------------------------

active_rtsp_recordings_lock: threading.Lock = threading.Lock()
active_rtsp_recordings: dict = {}

# ---------------------------------------------------------------------------
# Camera health shared state
# ---------------------------------------------------------------------------

_camera_health_lock: threading.Lock = threading.Lock()
_camera_health_state: dict = {}

# ---------------------------------------------------------------------------
# Notification thread pool
# ---------------------------------------------------------------------------

_notification_threads_lock: threading.Lock = threading.Lock()
_notification_threads: list = []

# ---------------------------------------------------------------------------
# Sound-monitor shared state
# ---------------------------------------------------------------------------

_sound_detectors: dict = {}
_sound_detectors_lock: threading.Lock = threading.Lock()
_sound_statuses: dict = {}
_sound_statuses_lock: threading.Lock = threading.Lock()
