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

import secrets
import threading
import time
from typing import Any

# ---------------------------------------------------------------------------
# Compile-time constants (pure values; safe to import at module top-level)
# ---------------------------------------------------------------------------

# Motion-detection frame geometry and thresholds (used as function default-arg
# values in app.zone_detection and app.detection_state at import time).
_MOTION_FRAME_W: int = 320
_MOTION_FRAME_H: int = 240
_MOTION_PIXEL_THRESHOLD: int = 30
_MOTION_GATE_FRACTION: float = 0.005
_MOTION_SCALE_FRACTION: float = 0.03
_MOTION_BACKGROUND_ALPHA: float = 0.05
# Motion background engine. ``mog2`` is the default OpenCV Gaussian-mixture
# background subtractor (handles gradual light changes, swaying foliage, and
# shadow rejection); ``diff`` selects the legacy single-frame adaptive-diff
# fallback (also used automatically when the OpenCV build lacks MOG2).
_MOTION_ALGORITHM: str = 'mog2'
# Morphological open+close on the foreground mask to erase isolated sensor
# noise (a major false-positive source on IR/night cameras) and consolidate
# motion into coherent blobs.
_MOTION_DENOISE: bool = True
# MOG2 shadow rejection: cast shadows are classified separately and dropped so
# a moving shadow does not register as motion. Tri-state: 'on' (default) always
# rejects shadows, 'off' never does, 'auto' rejects them only while the scene is
# bright (day) and disables rejection when the frame goes dark (night/IR), where
# a genuine subject is otherwise easily misread as a shadow and dropped. Legacy
# bool values (True/False) are still honoured as on/off.
_MOTION_SHADOW_SUPPRESSION: str = 'on'
# Mean-brightness threshold (0-255) below which 'auto' shadow suppression treats
# the scene as night and stops rejecting shadows.
_MOTION_NIGHT_BRIGHTNESS: float = 50.0
# MOG2 history length (number of recent frames the mixture model blends). Not
# operator-exposed; the per-frame learning rate is driven by background_alpha.
_MOTION_MOG2_HISTORY: int = 250

# Middleware / auth constants (moved from app.main so app.middleware can
# import them at module top-level without a circular import).
PUBLIC_PREFIXES: tuple[str, ...] = ('/static/',)
PUBLIC_PATHS: frozenset[str] = frozenset({'/favicon.ico', '/login', '/setup'})
ADMIN_PATHS: frozenset[str] = frozenset({
    '/onnx', '/yamnet-tflite', '/ai', '/cameras', '/settings',
    '/users', '/zones', '/sounds', '/audit', '/camera-log', '/application-log',
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
cloudflare_tunnel_manager: Any = None  # CloudflareTunnelManager, initialized at startup


# ---------------------------------------------------------------------------
# Startup-initialized config snapshots (populated by app.main at module load)
# ---------------------------------------------------------------------------

config: dict = {}           # on-disk YAML config; static after startup
auth_config: dict = {}      # pre-stripped snapshot of config['auth']

# ---------------------------------------------------------------------------
# Camera runtime state (reassigned by app.main.apply_cameras_settings)
# ---------------------------------------------------------------------------

camera: Any = None          # active camera instance (first camera, or None)
cameras_config: list = []
camera_config: dict = {}
camera_instances: dict = {}
_camera_instances_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Live-detection shared state (locks + associated dicts)
# ---------------------------------------------------------------------------

live_detection_history_lock: threading.Lock = threading.Lock()
live_detection_history: dict = {}

# Rolling per-camera window of the object labels seen in each recent detection
# cycle, used by the temporal N-of-M confirmation gate
# (``app.detection_state.confirm_object_detections``). Kept separate from
# ``live_detection_history`` (which feeds recording-playback overlays) so the
# confirmation window can size itself independently.
live_detection_confirm_lock: threading.Lock = threading.Lock()
live_detection_confirm_history: dict = {}

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
# Last analyzed thumbnail per camera. Comparing consecutive thumbnails catches
# moving subjects that occupy too few pixels to clear the adaptive-background
# gate on any single sample (especially when the ingest repeats a frame).
_frame_motion_last_frame: dict = {}
# Full-resolution grayscale predecessor used to recover thin/high-frequency
# movement (tree leaves, wires, distant limbs) lost by thumbnail resizing.
_frame_motion_last_gray: dict = {}
_frame_motion_error_cameras: set = set()
# Per-camera OpenCV MOG2 background subtractor + the geometry/params it was
# built with (so a live frame-size or threshold change rebuilds it). Populated
# and mutated only under ``_frame_motion_lock`` from app/detection_state.py.
# codeql[py/unused-global-variable]
_frame_motion_mog2: dict = {}
# codeql[py/unused-global-variable]
_frame_motion_mog2_meta: dict = {}
# Consecutive per-zone motion evidence used by the live alert monitor. A zone
# must be present in two analyzed frames before motion can create an event or
# recording, which filters one-frame stream/exposure artifacts.
_motion_confirm_lock: threading.Lock = threading.Lock()
# Read/written from app/detection_state.py and app/live_monitor.py via
# ``_state._motion_confirm_streaks``; CodeQL's unused-global-variable query
# does not track these cross-module attribute accesses.
# codeql[py/unused-global-variable]
_motion_confirm_streaks: dict = {}

# ---------------------------------------------------------------------------
# Active RTSP recordings
# ---------------------------------------------------------------------------

active_rtsp_recordings_lock: threading.Lock = threading.Lock()
active_rtsp_recordings: dict = {}
# Wall-clock end timestamp of the last completed RTSP event capture per camera,
# guarded by ``active_rtsp_recordings_lock``. Used to clamp a new clip's pre-roll
# so it does not re-capture footage the previous clip already holds (see
# ``recording_extension.start_rtsp_recording_capture``). In-memory only: after a
# restart the rolling prebuffer is empty too, so there is nothing to overlap.
last_rtsp_capture_end: dict = {}

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

# ---------------------------------------------------------------------------
# Alert / storage singletons (assigned at module load by app.main)
# ---------------------------------------------------------------------------

alerts: Any = None      # AlertEngine instance
storage: Any = None     # Storage instance

# ---------------------------------------------------------------------------
# Startup-registered callables (assigned by app.main after they are defined)
# ---------------------------------------------------------------------------

camera_event_recording_config: Any = None
apply_cameras_settings: Any = None
apply_storage_and_recording_settings: Any = None
reload_detector: Any = None

# ---------------------------------------------------------------------------
# Settings-replacement lock (Bug 6)
# ---------------------------------------------------------------------------

# Serializes ``apply_storage_and_recording_settings`` and
# ``apply_cameras_settings`` (and any future settings-replacement path)
# so a concurrent settings change cannot prime an outgoing
# RecordingService while the swap is still teardown-then-publishing the
# new one. Both functions acquire this lock for the ENTIRE duration of
# their swap; only one of them can be inside the critical section at a
# time, so ``apply_sound_settings()`` -> ``prime_rtsp_prebuffer`` cannot
# interleave with ``_state.recording_service = NEW`` and start a fresh
# ffmpeg while the OLD service's workers are still alive.
_apply_settings_lock: threading.RLock = threading.RLock()

# ---------------------------------------------------------------------------
# In-flight update guard (used by update_router)
# ---------------------------------------------------------------------------

_update_in_progress: bool = False
_update_lock: threading.Lock = threading.Lock()

# ── Runtime-data two-step delete (M2 fix) token store ──────────────────
# Each admin who requests a preview of the
# ``DELETE /api/system/runtime-data`` wipe gets a fresh 256-bit token.
# The wipe endpoint refuses to run without ``?confirm=true`` AND a header
# echoing a recent, unconsumed token for the SAME user. Tokens expire
# after ``_RUNTIME_DELETE_TOKEN_TTL_SECONDS`` (currently 30s) and are
# pruned lazily on every issue/consume call. The in-memory store is
# bound to the process lifetime, so a service restart invalidates all
# outstanding tokens (acceptable for a defensive belt-and-braces UI
# affordance on a self-hosted service).

_RUNTIME_DELETE_TOKEN_TTL_SECONDS: float = 30.0
_runtime_delete_tokens: dict = {}
_runtime_delete_lock: threading.Lock = threading.Lock()


def issue_runtime_delete_token(user_id: Any) -> str:
    """Mint a fresh delete-confirm token bound to ``user_id`` and stash it.

    Lazy-prunes any pre-existing tokens older than the TTL on the way
    in - defends against the dict leaking if a single admin never
    follows up with a confirm.
    """
    with _runtime_delete_lock:
        now = time.time()
        for existing_user, _entry in list(_runtime_delete_tokens.items()):
            issued_at = _runtime_delete_tokens[existing_user][1]
            if now - issued_at > _RUNTIME_DELETE_TOKEN_TTL_SECONDS:
                _runtime_delete_tokens.pop(existing_user, None)
        token = secrets.token_urlsafe(32)
        _runtime_delete_tokens[user_id] = (token, now)
        return token


def consume_runtime_delete_token(user_id: Any, presented_token: str) -> str | None:
    """Validate + consume a delete-confirm token.

    Returns ``None`` on success (token matched & removed). Returns a
    short error string on any kind of mismatch - the same generic
    message is used for missing / expired / wrong-owner to defeat
    timing-based / enumeration side channels. ``user_id`` is the
    resolved id from the *currently-authenticated* session, so a
    token issued to admin A cannot be redeemed by admin B even if
    the header leaks.
    """
    with _runtime_delete_lock:
        now = time.time()
        for existing_user, _entry in list(_runtime_delete_tokens.items()):
            issued_at = _runtime_delete_tokens[existing_user][1]
            if now - issued_at > _RUNTIME_DELETE_TOKEN_TTL_SECONDS:
                _runtime_delete_tokens.pop(existing_user, None)
        entry = _runtime_delete_tokens.pop(user_id, None)
        if entry is None:
            return 'Confirm token not recognised; please request a new preview first.'
        stored_token, _issued_at = entry
        if stored_token != presented_token:
            return 'Confirm token invalid or has already been used.'
        return None
