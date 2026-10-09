"""PTZ auto-tracking: keep a chosen kind of object (a cat, a person) in view.

Once per detection cycle the live monitor hands this module the cycle's
detections. On a camera with auto-tracking on, it:

1. **Acquires** a target: the largest detection of a followed label (``cat``,
   or a group such as ``animal``) that has passed the camera's zone and
   confirmation gates and has been tracked for ``ACQUIRE_MIN_AGE`` cycles, so a
   one-frame false detection never swings the camera.
2. **Follows** it: the same track id, or failing that the same label nearest
   to where the target was last seen. Following deliberately ignores zones -
   the moment the camera pans, zone outlines no longer line up with the scene.
3. **Steers** with velocity pulses. When the target's centre is outside the
   dead zone (a box around the middle of the frame), the camera pans/tilts
   towards it at a speed proportional to how far off centre it is. The pulse
   is longer the further off centre the target is (``pulse_seconds``): short
   nudges for small corrections, up to ``MAX_PULSE_SECONDS`` for a target near
   the edge, so a person walking across the frame is not outrun. The next
   pulse waits ``SETTLE_SECONDS`` after the last one ends, because the video
   lags the motor: steering on frames that predate the last pulse is what
   makes trackers overshoot and hunt back and forth.
   A target that keeps walking away is chased harder: when a pan pulse did
   not gain on it (it is still as far off centre, on the same side), the next
   pulse is faster and longer (``BOOST_STEP`` up to
   ``MAX_BOOST``). The boost resets once the target is centred or the camera
   overshoots, so a person who stops is not swung past.
   A target that vanishes at the frame edge (half out of the picture, so the
   detector misses it) is followed off the edge with up to
   ``MAX_EDGE_PUSHES`` pulses in that direction instead of freezing.
4. **Lets go** when the target has not been seen for ``lost_seconds``, and
   after ``return_home_seconds`` with nothing to follow sends the camera back to
   its home position (or a chosen preset).

Optional zoom (``zoom``): while the target is centred, the camera zooms in
until the target's box is about ``target_size`` of the frame height, and
zooms out when it is much bigger. A target drifting towards the frame edge
triggers an immediate zoom out so it is not lost. When the target is lost the
tracker undoes its own zoom-in straight away (roughly - zoom pulses are timed,
not measured); return-home later restores the exact home zoom.

Manual PTZ use always wins: a command from the Live page pauses tracking for
``MANUAL_PAUSE_SECONDS``.

Camera commands run on a small worker pool, never on the detection thread, and
a camera with a command still in flight is skipped rather than queued, so a
slow or offline camera can never stall detection. Each pulse also marks the
camera as moving (``mark_camera_motion``) so the existing camera-motion guard
treats the pan as camera movement rather than objects moving.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.utils import normalize_bool_setting

logger = logging.getLogger('daygle.ai')

# Control loop timing (seconds). Pan/tilt pulses run from MIN_PULSE_SECONDS
# (just outside the dead zone) to MAX_PULSE_SECONDS (target at the frame
# edge); zoom-only pulses use PULSE_SECONDS. A test at 0.4 s fixed pulses
# could not keep a walking person in view; the camera stops after every pulse
# (app.ptz.ptz_move), so a longer pulse is bounded, not a runaway.
PULSE_SECONDS = 0.4
MIN_PULSE_SECONDS = 0.3
MAX_PULSE_SECONDS = 1.5
SETTLE_SECONDS = 0.5

# Tilt is scaled to the picture's shape: a 16:9 frame is far shorter than it
# is wide, so the same motor turn moves the picture about 16/9 times as far
# (as a share of the frame) vertically as horizontally. Steering both axes
# alike made tilt overshoot - a distant person at the top edge got the camera
# tilted past them into the sky. Uses the frame aspect the live pipeline
# stamps on each detection (``_frame_aspect``), 16:9 when it is missing.
DEFAULT_FRAME_ASPECT = 16 / 9


def tilt_scale(frame_aspect: Any) -> float:
    """Tilt velocity relative to pan for a frame of ``frame_aspect`` (w / h)."""
    try:
        aspect = float(frame_aspect or DEFAULT_FRAME_ASPECT)
    except (TypeError, ValueError):
        aspect = DEFAULT_FRAME_ASPECT
    if aspect <= 0:
        aspect = DEFAULT_FRAME_ASPECT
    return max(0.3, min(1.0, 1.0 / aspect))


def _scale_tilt(tilt: float, scale: float, speed: int) -> float:
    """Scale a tilt velocity, keeping it above the motor dead-band floor."""
    if not tilt:
        return 0.0
    magnitude = max(MIN_VELOCITY * max(1, min(8, speed)) / 8.0, abs(tilt) * scale)
    return magnitude if tilt > 0 else -magnitude


# Catch-up boost for a target that keeps moving away (see module docstring).
# Pan only: someone walking across the frame is the case it exists for, and a
# boosted tilt pulse was what carried the camera past a target into the sky.
# A pulse "gained" when the error shrank below BOOST_PROGRESS of what it was
# at the previous pulse; otherwise that axis's boost grows by BOOST_STEP.
BOOST_STEP = 1.5
MAX_BOOST = 2.5
BOOST_PROGRESS = 0.8
MAX_BOOSTED_PULSE_SECONDS = 2.5

# Follow a target off the frame edge: when it was last seen at least this far
# from centre and then disappeared, keep turning that way for a few pulses.
EDGE_ERROR = 0.35
MAX_EDGE_PUSHES = 2
MANUAL_PAUSE_SECONDS = 30.0
HOME_MOVE_SECONDS = 4.0

# A track must have been seen this many cycles before the camera moves for it.
ACQUIRE_MIN_AGE = 2
# Without a matching track id, the target is the same label nearest its last
# position, but only within this distance (fraction of the frame).
MAX_FOLLOW_JUMP = 0.35
# Zoom: zoom in below ZOOM_IN_RATIO * target_size, out above ZOOM_OUT_RATIO *
# target_size, and always out when the target is this far from centre.
ZOOM_IN_RATIO = 0.7
ZOOM_OUT_RATIO = 1.4
ZOOM_EDGE_ERROR = 0.35
ZOOM_VELOCITY = 0.5
# Zoom in only once the target has been centred, with no pan/tilt pulse, for
# this long - never on someone just passing through the middle mid-walk.
ZOOM_SETTLED_SECONDS = 2.0
# While zoomed in by the tracker, the same pan moves the picture further, so
# pan/tilt pulses are scaled down by this and the catch-up boost is off.
ZOOMED_PULSE_SCALE = 0.5
# Velocity = error * GAIN (clamped), scaled by the tracking speed; never below
# MIN_VELOCITY so small corrections still overcome motor dead-band.
GAIN = 2.0
MIN_VELOCITY = 0.15

DEFAULTS: dict[str, Any] = {
    'enabled': False,
    'labels': ['person'],
    'dead_zone': 0.15,
    'speed': 4,
    'lost_seconds': 3.0,
    'return_home_seconds': 30.0,
    'home_preset': '',
    'zoom': False,
    'target_size': 0.3,
}


def normalize_auto_track_settings(raw: Any) -> dict[str, Any]:
    """Canonical ``ptz.auto_track`` settings (see ``DEFAULTS``)."""
    from app.zone_schema import normalize_label_list

    raw = raw if isinstance(raw, dict) else {}

    def _float(key: str, low: float, high: float) -> float:
        try:
            value = float(raw.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            value = DEFAULTS[key]
        if value != value:  # NaN
            value = DEFAULTS[key]
        return round(max(low, min(high, value)), 3)

    try:
        speed = int(raw.get('speed', DEFAULTS['speed']))
    except (TypeError, ValueError):
        speed = DEFAULTS['speed']
    labels = normalize_label_list(raw['labels']) if 'labels' in raw else list(DEFAULTS['labels'])
    return {
        'enabled': normalize_bool_setting(raw.get('enabled'), False),
        'labels': labels,
        'dead_zone': _float('dead_zone', 0.05, 0.4),
        'speed': max(1, min(8, speed)),
        'lost_seconds': _float('lost_seconds', 1.0, 30.0),
        'return_home_seconds': _float('return_home_seconds', 0.0, 3600.0),
        'home_preset': str(raw.get('home_preset') or '').strip()[:64],
        'zoom': normalize_bool_setting(raw.get('zoom'), False),
        'target_size': _float('target_size', 0.05, 0.8),
    }


def axis_velocity(error: float, dead_zone: float, speed: int) -> float:
    """Velocity (-1..1) for one axis given the target's offset from centre."""
    if abs(error) <= dead_zone:
        return 0.0
    magnitude = max(MIN_VELOCITY, min(1.0, abs(error) * GAIN)) * (max(1, min(8, speed)) / 8.0)
    return magnitude if error > 0 else -magnitude


def pulse_seconds(error: float, dead_zone: float) -> float:
    """Pan/tilt pulse length for the target's largest offset from centre."""
    span = max(1e-6, 0.5 - dead_zone)
    fraction = max(0.0, min(1.0, (abs(error) - dead_zone) / span))
    return round(MIN_PULSE_SECONDS + fraction * (MAX_PULSE_SECONDS - MIN_PULSE_SECONDS), 3)


def zoom_velocity(error_x: float, error_y: float, box_height: float, target_size: float, centred: bool) -> float:
    """Zoom velocity (+in / -out) for the target's offset and size."""
    if max(abs(error_x), abs(error_y)) > ZOOM_EDGE_ERROR:
        return -ZOOM_VELOCITY
    if box_height > target_size * ZOOM_OUT_RATIO:
        return -ZOOM_VELOCITY
    if centred and box_height < target_size * ZOOM_IN_RATIO:
        return ZOOM_VELOCITY
    return 0.0


def scaled_zoom(zoom: float, duration: float) -> float:
    """Zoom velocity for a pulse of ``duration`` that changes the lens by one
    standard step (PULSE_SECONDS at ZOOM_VELOCITY), whatever the pulse length."""
    if not zoom or duration <= 0:
        return 0.0
    magnitude = min(abs(zoom), ZOOM_VELOCITY * PULSE_SECONDS / duration)
    return magnitude if zoom > 0 else -magnitude


def zoom_amount(zoom: float, duration: float) -> float:
    """Signed zoom applied by a pulse, in seconds at ZOOM_VELOCITY."""
    return zoom * duration / ZOOM_VELOCITY if zoom else 0.0


def _center(detection: dict[str, Any]) -> tuple[float, float] | None:
    box = detection.get('box')
    if not isinstance(box, dict):
        return None
    try:
        return (
            float(box.get('x') or 0) + float(box.get('width') or 0) / 2.0,
            float(box.get('y') or 0) + float(box.get('height') or 0) / 2.0,
        )
    except (TypeError, ValueError):
        return None


def _area(detection: dict[str, Any]) -> float:
    box = detection.get('box') or {}
    try:
        return float(box.get('width') or 0) * float(box.get('height') or 0)
    except (TypeError, ValueError):
        return 0.0


class _CameraState:
    __slots__ = (
        'target_label', 'target_track_id', 'last_center', 'last_seen',
        'next_command_at', 'paused_until', 'moved_since_home', 'busy', 'state',
        'zoom_in_seconds', 'boost', 'last_error', 'edge_pushes', 'last_steer_at',
        'frame_aspect',
    )

    def __init__(self) -> None:
        self.target_label: str | None = None
        self.target_track_id: Any = None
        self.last_center: tuple[float, float] | None = None
        self.last_seen = 0.0
        # Earliest time the next command may go out (pulse end + settle).
        self.next_command_at = 0.0
        self.paused_until = 0.0
        self.moved_since_home = False
        self.busy = False
        self.state = 'idle'
        # Net zoom this tracker has applied, in seconds at ZOOM_VELOCITY,
        # undone on loss.
        self.zoom_in_seconds = 0.0
        # When the last pan/tilt pulse went out (zoom-in waits for calm).
        self.last_steer_at = 0.0
        # Frame width / height from the last sighting (tilt scaling).
        self.frame_aspect: float | None = None
        self.reset_steering()

    def reset_steering(self) -> None:
        # Per axis (pan, tilt): the catch-up multiplier, and the signed error
        # at the last pulse on that axis (None = no pulse since centring).
        self.boost = [1.0, 1.0]
        self.last_error: list[float | None] = [None, None]
        self.edge_pushes = 0

    def drop_target(self) -> None:
        self.target_label = None
        self.target_track_id = None
        self.last_center = None
        self.reset_steering()


# Re-entrant: a dispatched job clears its busy flag under this lock, and a
# job may run inline on the calling thread (tests, a saturated pool).
_lock = threading.RLock()
_states: dict[str, _CameraState] = {}
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='ptz-track')
    return _executor


def _state_for(camera_id: str) -> _CameraState:
    state = _states.get(camera_id)
    if state is None:
        state = _states[camera_id] = _CameraState()
    return state


def reset_auto_tracking(camera_id: str | None = None) -> None:
    """Forget tracking state for one camera (or all of them)."""
    with _lock:
        if camera_id is None:
            _states.clear()
        else:
            _states.pop(str(camera_id), None)


def note_manual_ptz(camera_id: str, *, now: float | None = None) -> None:
    """A person used the PTZ controls: pause tracking and drop the target."""
    now = time.monotonic() if now is None else now
    with _lock:
        state = _state_for(str(camera_id))
        state.paused_until = now + MANUAL_PAUSE_SECONDS
        state.drop_target()
        state.moved_since_home = True
        state.last_seen = now
        # The operator may have zoomed: the tracker's own zoom accounting no
        # longer describes the lens, so it must not "undo" their zoom later.
        state.zoom_in_seconds = 0.0


def _dispatch(camera_id: str, state: _CameraState, job: Callable[[], None], what: str) -> bool:
    """Run ``job`` on the worker pool unless this camera is still busy."""
    if state.busy:
        return False
    state.busy = True

    def _run() -> None:
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - a camera error must not kill the worker
            logger.warning('PTZ auto-track %s failed on %s: %s', what, camera_id, exc)
        finally:
            with _lock:
                state.busy = False

    _get_executor().submit(_run)
    return True


def _pick_new_target(
    candidates: list[dict[str, Any]], labels: list[str],
) -> dict[str, Any] | None:
    from app.zone_schema import label_matches

    eligible = [
        det for det in candidates
        if isinstance(det, dict)
        and int(det.get('track_age') or 0) >= ACQUIRE_MIN_AGE
        and _center(det) is not None
        and any(label_matches(det.get('label'), label) for label in labels)
    ]
    # The largest box is usually the closest subject - the one worth following.
    return max(eligible, key=_area) if eligible else None


def _update_boost(state: _CameraState, axis: int, error: float, dead_zone: float) -> float:
    """Catch-up multiplier for one axis at a pulse (see BOOST_STEP)."""
    if abs(error) <= dead_zone:
        state.boost[axis] = 1.0
        state.last_error[axis] = None
        return 1.0
    previous = state.last_error[axis]
    if previous is not None:
        if (previous > 0) != (error > 0):
            # Overshot: the target is now on the other side. Start gentle.
            state.boost[axis] = 1.0
        elif abs(error) >= abs(previous) * BOOST_PROGRESS:
            # The last pulse did not gain on it: it is moving away.
            state.boost[axis] = min(MAX_BOOST, state.boost[axis] * BOOST_STEP)
    return state.boost[axis]


def _follow(state: _CameraState, visible: list[dict[str, Any]], *, max_jump: float = MAX_FOLLOW_JUMP) -> dict[str, Any] | None:
    from app.zone_schema import canonical_label

    same_label = [
        det for det in visible
        if isinstance(det, dict)
        and canonical_label(det.get('label')) == state.target_label
        and _center(det) is not None
    ]
    if state.target_track_id is not None:
        for det in same_label:
            if det.get('track_id') == state.target_track_id:
                return det
    if state.last_center is None or not same_label:
        return None
    lx, ly = state.last_center

    def _distance(det: dict[str, Any]) -> float:
        cx, cy = _center(det)  # type: ignore[misc]
        return ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5

    nearest = min(same_label, key=_distance)
    return nearest if _distance(nearest) <= max_jump else None


def update_auto_tracking(
    camera_id: str,
    camera_settings: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    visible: list[dict[str, Any]],
    now: float | None = None,
    mover: Callable[..., None] | None = None,
) -> dict[str, Any] | None:
    """Run one control step; return a status dict, or None when tracking is off.

    ``candidates`` are this cycle's confirmed, zone-scoped object detections
    (eligible to start a track); ``visible`` is every tracked detection this
    cycle (used to keep following once the camera has moved). ``mover`` is a
    test seam; by default commands go to the camera through ``app.ptz``.
    """
    from app.ptz import ptz_connection

    camera_id = str(camera_id)
    ptz_config = camera_settings.get('ptz') if isinstance(camera_settings.get('ptz'), dict) else {}
    settings = normalize_auto_track_settings(ptz_config.get('auto_track'))
    if not settings['enabled'] or not settings['labels']:
        with _lock:
            _states.pop(camera_id, None)
        return None
    conn = ptz_connection(camera_settings)
    if conn is None:
        return None
    now = time.monotonic() if now is None else now
    mover = mover or _default_mover

    with _lock:
        state = _state_for(camera_id)
        if now < state.paused_until:
            state.state = 'paused'
            return _status(state, now)

        # After pushing the camera off the edge after a lost target, the
        # target reappears somewhere else in the frame: accept the nearest
        # same-label box anywhere.
        max_jump = 1.5 if state.edge_pushes else MAX_FOLLOW_JUMP
        target = _follow(state, visible, max_jump=max_jump) if state.target_label else None
        if target is None and state.target_label and now - state.last_seen > settings['lost_seconds']:
            logger.info('PTZ auto-track on %s lost the %s', camera_id, state.target_label)
            state.drop_target()
        if target is None and state.target_label is None:
            target = _pick_new_target(candidates, settings['labels'])
            if target is not None:
                from app.zone_schema import canonical_label
                state.target_label = canonical_label(target.get('label'))
                logger.info('PTZ auto-track on %s following %s (track %s)', camera_id, state.target_label, target.get('track_id'))

        if target is not None:
            state.target_track_id = target.get('track_id')
            state.last_center = _center(target)
            state.last_seen = now
            state.state = 'tracking'
            state.edge_pushes = 0
            cx, cy = state.last_center  # type: ignore[misc]
            pan = axis_velocity(cx - 0.5, settings['dead_zone'], settings['speed'])
            # Image y grows downwards; ONVIF/Pelco-D tilt is positive upwards.
            tilt = -axis_velocity(cy - 0.5, settings['dead_zone'], settings['speed'])
            state.frame_aspect = target.get('_frame_aspect') or state.frame_aspect
            tilt = _scale_tilt(tilt, tilt_scale(state.frame_aspect), settings['speed'])
            boost = 1.0
            zoom = 0.0
            if settings['zoom']:
                try:
                    box_height = float((target.get('box') or {}).get('height') or 0)
                except (TypeError, ValueError):
                    box_height = 0.0
                zoom = zoom_velocity(
                    cx - 0.5, cy - 0.5, box_height, settings['target_size'],
                    centred=not (pan or tilt) and now - state.last_steer_at >= ZOOM_SETTLED_SECONDS,
                )
                if zoom < 0 and state.zoom_in_seconds <= 0 and box_height <= settings['target_size'] * ZOOM_OUT_RATIO:
                    # An edge zoom-out only undoes our own zoom-in; it never
                    # widens past where tracking started.
                    zoom = 0.0
            ready = now >= state.next_command_at and not state.busy
            zoomed_in = state.zoom_in_seconds > 0
            if ready and (pan or tilt) and zoomed_in:
                # Zoomed in, a pulse moves the picture further: no catch-up
                # boost (and shorter pulses below), or it overshoots.
                state.reset_steering()
            elif ready and (pan or tilt):
                # Boost is judged only when a pulse can actually go out, so a
                # cycle that is still settling does not count as "no gain".
                pan_boost = _update_boost(state, 0, cx - 0.5, settings['dead_zone'])
                pan = max(-1.0, min(1.0, pan * pan_boost))
                boost = pan_boost if pan else 1.0
            elif ready:
                state.reset_steering()
            if pan or tilt:
                duration = min(
                    MAX_BOOSTED_PULSE_SECONDS,
                    pulse_seconds(max(abs(cx - 0.5), abs(cy - 0.5)), settings['dead_zone']) * boost,
                )
                if zoomed_in:
                    duration = max(MIN_PULSE_SECONDS, duration * ZOOMED_PULSE_SCALE)
            else:
                duration = PULSE_SECONDS
            # One command carries pan, tilt and zoom for the same duration, so
            # a zoom riding on a long pan pulse is slowed to change the lens by
            # one standard zoom step (PULSE_SECONDS at ZOOM_VELOCITY).
            zoom = scaled_zoom(zoom, duration)
            if zoom and (pan or tilt) and conn.protocol == 'tcp_pelcod':
                # Pelco-D zoom has no speed byte, so it cannot be slowed to fit
                # a long pan pulse: zoom only in its own short pulses there.
                zoom = 0.0
            if (pan or tilt or zoom) and ready:
                if _dispatch(camera_id, state, lambda: mover('move', camera_id, conn, pan, tilt, zoom, duration), 'move'):
                    if pan:
                        state.last_error[0] = cx - 0.5
                    if tilt:
                        state.last_error[1] = cy - 0.5
                    if pan or tilt:
                        state.last_steer_at = now
                    state.next_command_at = now + duration + SETTLE_SECONDS
                    state.moved_since_home = True
                    state.zoom_in_seconds += zoom_amount(zoom, duration)
            return _status(state, now)

        if state.target_label is not None:
            state.state = 'tracking'
            lx, ly = state.last_center or (0.5, 0.5)
            if (
                max(abs(lx - 0.5), abs(ly - 0.5)) >= EDGE_ERROR
                and state.edge_pushes < MAX_EDGE_PUSHES
                and now >= state.next_command_at
            ):
                # Last seen at the edge: it has most likely walked out of the
                # picture (half out, so the detector misses it). Keep turning
                # after it instead of freezing until it is lost.
                pan = axis_velocity(lx - 0.5, EDGE_ERROR - 0.01, settings['speed'])
                tilt = -axis_velocity(ly - 0.5, EDGE_ERROR - 0.01, settings['speed'])
                tilt = _scale_tilt(tilt, tilt_scale(state.frame_aspect), settings['speed'])
                duration = MAX_PULSE_SECONDS
                zoom = 0.0
                if state.zoom_in_seconds > 0:
                    # Zoomed in: a shorter push, and widen out at the same time
                    # (not on Pelco-D, whose zoom cannot be slowed) so the
                    # target is easier to find again.
                    duration = MAX_PULSE_SECONDS * ZOOMED_PULSE_SCALE
                    if conn.protocol != 'tcp_pelcod':
                        zoom = scaled_zoom(-ZOOM_VELOCITY, duration)
                if _dispatch(camera_id, state, lambda: mover('move', camera_id, conn, pan, tilt, zoom, duration), 'edge follow'):
                    state.edge_pushes += 1
                    state.moved_since_home = True
                    state.last_steer_at = now
                    state.zoom_in_seconds = max(0.0, state.zoom_in_seconds + zoom_amount(zoom, duration))
                    state.next_command_at = now + duration + SETTLE_SECONDS
            # Otherwise briefly out of view (a missed detection, behind a
            # bush): hold still and keep the target until ``lost_seconds``.
            return _status(state, now)

        state.state = 'idle'
        if (
            state.zoom_in_seconds > 0
            and now >= state.next_command_at
        ):
            # Lost the target while zoomed in: widen back out, one pulse a
            # cycle, so the next subject can be found without waiting for the
            # return-home delay.
            if _dispatch(camera_id, state, lambda: mover('move', camera_id, conn, 0.0, 0.0, -ZOOM_VELOCITY, PULSE_SECONDS), 'zoom out'):
                state.zoom_in_seconds = max(0.0, state.zoom_in_seconds - PULSE_SECONDS)
                state.next_command_at = now + PULSE_SECONDS + SETTLE_SECONDS
            return _status(state, now)
        if (
            state.moved_since_home
            and settings['return_home_seconds'] > 0
            and now - state.last_seen >= settings['return_home_seconds']
            and now >= state.next_command_at
        ):
            preset = settings['home_preset']
            if _dispatch(camera_id, state, lambda: mover('home', camera_id, conn, preset), 'return home'):
                state.moved_since_home = False
                state.zoom_in_seconds = 0.0
                state.next_command_at = now + HOME_MOVE_SECONDS
                state.state = 'returning'
        return _status(state, now)


def _default_mover(action: str, camera_id: str, conn: Any, *args: Any) -> None:
    from app.detection_state import mark_camera_motion
    from app.ptz import ptz_goto_home, ptz_move

    if action == 'move':
        pan, tilt, zoom, duration = args
        mark_camera_motion(camera_id, duration, reason='auto_track')
        ptz_move(conn, pan, tilt, duration, zoom=zoom)
    elif action == 'home':
        (preset,) = args
        mark_camera_motion(camera_id, HOME_MOVE_SECONDS, reason='auto_track_home')
        if not ptz_goto_home(conn, preset):
            logger.info('PTZ auto-track on %s: no home preset set for this protocol; staying put', camera_id)


def _status(state: _CameraState, now: float) -> dict[str, Any]:
    status: dict[str, Any] = {'state': state.state}
    if state.state == 'tracking':
        status['label'] = state.target_label
        status['track_id'] = state.target_track_id
    elif state.state == 'paused':
        status['resumes_in'] = round(max(0.0, state.paused_until - now), 1)
    return status
