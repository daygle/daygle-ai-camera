from __future__ import annotations

import ctypes
import ctypes.util
import logging
import math
import os
import shutil
import subprocess
import threading
import time
from fractions import Fraction
from typing import Any


logger = logging.getLogger('daygle.ai')


# A failed ``VideoCapture.read``/``retrieve`` is recoverable for RTSP streams:
# FFmpeg can briefly return an empty decoded picture while the network session
# is being torn down or re-established. Keep retries bounded so a dead camera
# does not block a request indefinitely or spin in a tight reconnect loop.
CAPTURE_READ_ATTEMPTS = 3
CAPTURE_RETRY_BASE_SECONDS = 0.25
CAPTURE_RETRY_MAX_SECONDS = 2.0
CAPTURE_FAILURE_LOG_INTERVAL_SECONDS = 15.0

# Cached library handles discovered on first VideoCapture open.
_avutil_libs: list[Any] = []
_avutil_libs_searched = False
_avutil_lock = threading.Lock()


def _configure_ffmpeg_log_level() -> None:
    """Suppress FFmpeg decode noise.

    Must be called *after* cv2.VideoCapture() has been constructed so that
    FFmpeg's own initialisation cannot reset the log level back to a noisy
    default.  Library discovery is deferred to the first call and cached;
    the level is re-applied on every call so that reconnects (which create a
    new VideoCapture and trigger a fresh FFmpeg init) stay quiet.
    """
    # Only ``_avutil_libs_searched`` is rebound here; ``_avutil_libs`` is
    # mutated in place (append) below, which needs no ``global``.
    global _avutil_libs_searched

    level_name = str(os.environ.get('DAYGLE_FFMPEG_LOGLEVEL', 'quiet')).strip().lower()
    level_map = {
        'quiet': -8,
        'panic': 0,
        'fatal': 8,
        'error': 16,
        'warning': 24,
        'info': 32,
        'verbose': 40,
        'debug': 48,
        'trace': 56,
    }
    level = level_map.get(level_name)
    if level is None:
        logger.warning('Unknown DAYGLE_FFMPEG_LOGLEVEL=%s; log level unchanged.', level_name)
        return

    # Discover libavutil handles once; after that just re-apply the level.
    # Double-checked locking keeps this safe when multiple camera threads
    # each open a VideoCapture concurrently.
    if not _avutil_libs_searched:
        with _avutil_lock:
            if not _avutil_libs_searched:
                import glob as _glob

                lib_paths: list[str] = []

                # opencv-python-headless bundles its own copy of FFmpeg. The system
                # libavutil is a separate shared-library instance, so calling
                # av_log_set_level on it has no effect on OpenCV's decoder output.
                # Search the cv2 package directory for the bundled libavutil first.
                try:
                    import importlib.util as _ilu
                    _spec = _ilu.find_spec('cv2')
                    if _spec and _spec.origin:
                        _pkg_dir = os.path.dirname(_spec.origin)
                        for _d in [_pkg_dir, os.path.join(_pkg_dir, '.libs')]:
                            lib_paths.extend(sorted(_glob.glob(os.path.join(_d, 'libavutil*.so*'))))
                except Exception as exc:
                    logger.debug("Could not search cv2 package for libavutil: %s", exc)

                # Scan /proc/self/maps for any libavutil already mapped into this
                # process (populated after cv2.VideoCapture() opens the stream).
                try:
                    with open('/proc/self/maps') as _maps:
                        for _line in _maps:
                            if 'libavutil' in _line and '.so' in _line:
                                _parts = _line.rstrip().split()
                                if _parts and _parts[-1].startswith('/') and _parts[-1] not in lib_paths:
                                    lib_paths.append(_parts[-1])
                except Exception as exc:
                    logger.debug("Could not scan /proc/self/maps for libavutil: %s", exc)

                # Fall back to the system-installed library.
                _system_lib = ctypes.util.find_library('avutil')
                if _system_lib and _system_lib not in lib_paths:
                    lib_paths.append(_system_lib)

                for lib_name in lib_paths:
                    try:
                        avutil = ctypes.CDLL(lib_name)
                        avutil.av_log_set_level.argtypes = [ctypes.c_int]
                        avutil.av_log_set_level.restype = None
                        _avutil_libs.append(avutil)
                    except Exception as exc:
                        logger.debug('Unable to load avutil %s: %s', lib_name, exc)

                # Set the flag last so other threads never see a partially
                # populated _avutil_libs list.
                _avutil_libs_searched = True

    for avutil in _avutil_libs:
        try:
            avutil.av_log_set_level(level)
        except Exception as exc:
            logger.debug('Unable to set FFmpeg log level: %s', exc)


class OpenCvStreamCamera:
    """Camera backend for RTSP/ONVIF-compatible streams read through OpenCV.

    Many ONVIF cameras, including P6S-style IP cameras, expose the actual video
    as an RTSP URL. This backend stores that stream URL and uses OpenCV/FFmpeg
    to pull snapshots for the live view.
    """

    def __init__(self, stream_url: str, width: int = 1280, height: int = 720, fps: int | None = None, stale_frame_grabs: int | None = None) -> None:
        self.stream_url = stream_url
        self.width = width
        self.height = height
        self.configured_fps = fps if fps and fps > 0 else None
        # Keep a conservative fallback for buffer-draining until the stream's
        # metadata is available. ``effective_fps`` exposes the distinction
        # between this fallback and the camera's declared source rate.
        self.fps = self.configured_fps or 15
        self.detected_fps: float | None = None
        self._stale_frame_grabs_configured = stale_frame_grabs
        self.frame_number = 0
        self.started_at = time.time()
        self.last_error: str | None = None
        self._capture: Any | None = None
        # Set by ``close()`` so an in-flight blocking ``cv2.VideoCapture()``
        # open can detect the close (after OpenCV eventually returns) and
        # release the capture instead of leaving it dangling. Instances are
        # never reopened after close.
        self._closed = False
        self._fps_probe_attempted_at = 0.0
        self._fps_probe_lock = threading.Lock()
        self._fps_probe_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._consecutive_read_failures = 0
        self._last_capture_failure_log_at = 0.0
        self._next_capture_retry_at = 0.0

    @property
    def backend(self) -> str:
        return "onvif"

    @property
    def effective_fps(self) -> float:
        """Return the best-known source FPS for display and stream handling.

        A configured value is an explicit operator override. Otherwise use
        OpenCV/FFmpeg's declared stream rate when it is sane. RTSP metadata is
        often absent, so retain the historical 15 FPS fallback without
        presenting it as a detected hardware value.
        """
        if self.configured_fps is not None:
            return float(self.configured_fps)
        if self.detected_fps is not None and math.isfinite(self.detected_fps) and 0 < self.detected_fps <= 120:
            return float(self.detected_fps)
        return 15.0

    def _probe_declared_fps(self) -> None:
        """Populate ``detected_fps`` for shared-ingest cameras.

        The shared FFmpeg ingest supplies frames to detection, so the OpenCV
        capture is normally never opened and ``CAP_PROP_FPS`` cannot populate
        the status payload. A short, cached ffprobe fills that metadata gap
        without changing the ingest's deliberately lower detection sampling
        rate. Failure is harmless and retried periodically.
        """
        if self.configured_fps is not None or self.detected_fps is not None:
            return
        ffprobe = shutil.which('ffprobe')
        if not ffprobe or not self.stream_url:
            return
        command = [
            ffprobe,
            '-v', 'error',
            '-rtsp_transport', 'tcp',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=avg_frame_rate,r_frame_rate',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            '-i', self.stream_url,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            return
        if result.returncode != 0:
            return
        for raw_rate in result.stdout.splitlines():
            try:
                rate = float(Fraction(raw_rate.strip()))
            except (ValueError, ZeroDivisionError):
                continue
            if math.isfinite(rate) and 0 < rate <= 120:
                with self._fps_probe_lock:
                    if self.configured_fps is None and self.detected_fps is None:
                        self.detected_fps = rate
                        self.fps = rate
                return

    def _schedule_fps_probe(self) -> None:
        """Start the cached source-rate probe without blocking status calls."""
        if self.configured_fps is not None or self.detected_fps is not None:
            return
        now = time.monotonic()
        with self._fps_probe_lock:
            if self.configured_fps is not None or self.detected_fps is not None:
                return
            if now - self._fps_probe_attempted_at < 60.0:
                return
            self._fps_probe_attempted_at = now
            thread = self._fps_probe_thread
            if isinstance(thread, threading.Thread) and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._probe_declared_fps,
                name='camera-fps-probe',
                daemon=True,
            )
            self._fps_probe_thread = thread
            thread.start()

    @property
    def fps_source(self) -> str:
        if self.configured_fps is not None:
            return 'configured'
        if self.detected_fps is not None and math.isfinite(self.detected_fps) and 0 < self.detected_fps <= 120:
            return 'detected'
        return 'fallback'

    def get_frame(self, timestamp: float | None = None) -> dict[str, Any]:
        # Shared-ingest cameras normally do not open the OpenCV capture, so
        # request a background metadata probe instead of blocking /api/status.
        self._schedule_fps_probe()
        return {
            "frame_number": self.frame_number,
            "timestamp": timestamp if timestamp is not None else time.time(),
            "width": self.width,
            "height": self.height,
            "fps": self.effective_fps,
            "configured_fps": self.configured_fps,
            "detected_fps": self.detected_fps,
            "effective_fps": self.effective_fps,
            "fps_source": self.fps_source,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "stream_url": self.stream_url,
            "last_error": self.last_error,
        }

    def _open_capture(self):
        if not self.stream_url:
            raise RuntimeError("ONVIF/RTSP stream URL is not configured.")
        if self._closed:
            raise RuntimeError('Camera closed.')

        # Prefer TCP for RTSP cameras. UDP packet loss and frequent reconnects
        # can make inexpensive ONVIF cameras fail during session setup.
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|max_delay;500000|stimeout;5000000|fflags;discardcorrupt")

        import cv2

        if self._capture is None:
            # ``cv2.VideoCapture()`` against a dead RTSP URL blocks inside
            # OpenCV for ~30s and neither stimeout nor
            # CAP_PROP_OPEN_TIMEOUT_MSEC bounds it in current builds. We hold
            # the instance lock across this call (see ``_acquire_raw_frame``),
            # so ``close()`` only waits a short grace window and this in-flight
            # open must detect the close once OpenCV returns.
            self._capture = cv2.VideoCapture(self.stream_url)
            if self._closed:
                # close() won the race while the blocking open was in flight;
                # discard the late capture and bail so the caller cleans up.
                self._capture.release()
                self._capture = None
                raise RuntimeError('Camera closed.')
            if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
                self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # Apply AFTER VideoCapture opens: FFmpeg's own init runs during
            # VideoCapture construction and resets the global log level, so
            # we must set quiet *after* that init completes.  Re-applying on
            # every new capture (including reconnects) keeps it quiet.
            _configure_ffmpeg_log_level()
        if not self._capture.isOpened():
            self._release_capture()
            self.last_error = "Unable to open ONVIF/RTSP stream."
            raise RuntimeError(self.last_error)
        # Try to read the stream's declared FPS from the container. Some
        # RTSP sources report 0 or a nonsensical value, in which case we
        # keep the configured/default FPS for buffer calculations. Only do
        # this after confirming the capture opened, otherwise we may read a
        # bogus value from an uninitialised capture.
        try:
            cap_fps = float(self._capture.get(cv2.CAP_PROP_FPS) or 0)
            if math.isfinite(cap_fps) and 0 < cap_fps <= 120:
                self.detected_fps = cap_fps
                if self.configured_fps is None:
                    self.fps = cap_fps
        except Exception:
            pass
        return self._capture

    def _release_capture(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    @staticmethod
    def _frame_contains_data(image: Any) -> bool:
        """Return whether a decoded OpenCV image is usable.

        FFmpeg/OpenCV may report ``ok=True`` while returning ``None`` or an
        empty array when the decoder has no picture after an RTSP interruption.
        Never let that value reach detection or JPEG encoding.
        """
        if image is None:
            return False
        try:
            return int(getattr(image, 'size', 1)) > 0
        except (TypeError, ValueError):
            return True

    def _record_capture_failure(self, reason: str) -> None:
        self._consecutive_read_failures += 1
        now = time.monotonic()
        if (
            self._consecutive_read_failures == 1
            or now - self._last_capture_failure_log_at >= CAPTURE_FAILURE_LOG_INTERVAL_SECONDS
        ):
            logger.warning(
                'RTSP capture returned no usable frame (%s consecutive failure%s); '
                'reconnecting capture.',
                self._consecutive_read_failures,
                '' if self._consecutive_read_failures == 1 else 's',
            )
            self._last_capture_failure_log_at = now

    def _record_capture_success(self) -> None:
        if self._consecutive_read_failures:
            logger.info(
                'RTSP capture recovered after %s consecutive empty-frame failure%s.',
                self._consecutive_read_failures,
                '' if self._consecutive_read_failures == 1 else 's',
            )
        self._consecutive_read_failures = 0
        self._next_capture_retry_at = 0.0

    def _capture_retry_delay(self, attempt: int) -> float:
        """Return a bounded exponential delay before the next reconnect."""
        return min(
            CAPTURE_RETRY_MAX_SECONDS,
            CAPTURE_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)),
        )

    def _acquire_raw_frame(self) -> tuple[Any, dict[str, Any]]:
        """Reconnect and read a frame with bounded retries.

        A failed ``retrieve``/``read`` closes the old capture before a new
        ``VideoCapture`` is opened. This ordering prevents a decoder teardown
        racing a new read on the same object, which is the common source of
        repeated ``Picture does not contain data`` messages. Must be called
        while ``self._lock`` is held.
        """
        retry_delay = self._next_capture_retry_at - time.monotonic()
        if retry_delay > 0:
            time.sleep(min(retry_delay, CAPTURE_RETRY_MAX_SECONDS))

        stale_grabs = self._stale_frame_grabs()
        last_failure: str | None = None
        for attempt in range(CAPTURE_READ_ATTEMPTS):
            if self._closed:
                self._release_capture()
                raise RuntimeError('Camera closed.')
            if attempt:
                self._release_capture()
                time.sleep(self._capture_retry_delay(attempt))
            try:
                capture = self._open_capture()
                ok, image, capture_ts = self._read_latest_frame(capture, stale_grabs, self.fps)
            except Exception as exc:  # cv2 can raise during decoder teardown
                ok, image, capture_ts = False, None, time.time()
                last_failure = type(exc).__name__

            if self._closed:
                self._release_capture()
                raise RuntimeError('Camera closed.')
            if ok and self._frame_contains_data(image):
                self._record_capture_success()
                height, width = image.shape[:2]
                self.width = int(width)
                self.height = int(height)
                self.frame_number += 1
                return image, self.get_frame(capture_ts)

            last_failure = last_failure or ('empty_frame' if ok else 'read_failed')
            self._record_capture_failure(last_failure)
            # Release before retrying, including when retrieve returned
            # ``ok=True`` with no decoded picture.
            self._release_capture()

        self.last_error = "Unable to read a frame from the ONVIF/RTSP stream."
        self._next_capture_retry_at = time.monotonic() + min(
            CAPTURE_RETRY_MAX_SECONDS,
            self._capture_retry_delay(self._consecutive_read_failures),
        )
        raise RuntimeError(self.last_error)

    def read_frame(self) -> tuple[Any, dict[str, Any]]:
        """Read the latest frame as a raw BGR numpy array, skipping JPEG encoding.

        This avoids the encode→decode round-trip when the caller (e.g.
        the ONNX detector) works on numpy arrays directly, saving ~30-90 ms
        per detection cycle.
        """
        with self._lock:
            image, frame = self._acquire_raw_frame()
        self.last_error = None
        return image, frame

    def read_jpeg(self) -> tuple[bytes, dict[str, Any]]:
        import cv2
        with self._lock:
            image, frame = self._acquire_raw_frame()
        ok, encoded = cv2.imencode(".jpg", image)
        if not ok:
            self.last_error = "Unable to encode ONVIF/RTSP frame as JPEG."
            raise RuntimeError(self.last_error)
        self.last_error = None
        return encoded.tobytes(), frame

    def _stale_frame_grabs(self) -> int:
        if self._stale_frame_grabs_configured is not None:
            return max(0, self._stale_frame_grabs_configured)
        # Default: grab ~25% of a second worth of frames to drain the RTSP
        # buffer and land on the latest one.  The old formula used 50%
        # (fps/2), which at 15 fps meant 7 grabs (~467 ms of latency).
        # Dropping to 25% (fps/4) halves that to ~233 ms while still
        # discarding enough stale frames on typical IP cameras.
        return max(1, min(8, int(self.effective_fps / 4)))

    @staticmethod
    def _read_latest_frame(capture, stale_frame_grabs: int, fps: int = 15) -> tuple[bool, Any, float]:
        """Drain buffered frames and decode the latest one.

        Returns ``(ok, image, capture_ts)`` where ``capture_ts`` is taken at
        the moment the decoded frame was pulled off the stream. The timestamp
        matters: detection-track samples are stamped with it and replayed over
        recordings, so a stale frame stamped "now" makes every playback
        overlay box trail the object on screen.

        The drain is adaptive rather than a fixed count: it always discards
        ``stale_frame_grabs`` frames (the historical behaviour), then keeps
        draining while grabs return faster than ~half a frame interval -
        a fast grab means the frame came from the buffer, not the live edge.
        Without the adaptive part the buffer grows without bound whenever
        detection cycles run slower than the stream's frame rate, and the
        analyzed frames (and therefore alerts and recordings) lag further and
        further behind reality.
        """
        if stale_frame_grabs <= 0:
            ok, image = capture.read()
            return ok, image, time.time()

        # A grab that had to wait roughly half a frame interval (or more)
        # came from the live edge; faster grabs were buffered backlog.
        live_edge_seconds = 0.5 / max(float(fps or 15), 5.0)
        max_total_grabs = max(stale_frame_grabs, 64)
        grabbed = False
        for index in range(max_total_grabs):
            started = time.monotonic()
            if not capture.grab():
                break
            grabbed = True
            waited = time.monotonic() - started
            if index >= stale_frame_grabs - 1 and waited >= live_edge_seconds:
                break
        capture_ts = time.time()
        if grabbed:
            ok, image = capture.retrieve()
            if ok and image is not None:
                return ok, image, capture_ts
        ok, image = capture.read()
        return ok, image, time.time()

    def snapshot(self) -> dict[str, Any]:
        _, frame = self.read_frame()
        frame["snapshot"] = True
        return frame

    def close(self) -> None:
        # Mark closed FIRST so an in-flight read bails out as soon as its
        # blocking ``cv2.VideoCapture()`` open returns (see ``_open_capture``).
        # Then release the capture if the lock is free within a short grace
        # window. A capture open against a dead RTSP URL blocks inside OpenCV
        # for ~30s and no timeout knob bounds it in current builds; settings
        # applies and database restores call ``close()`` on every old camera
        # instance, so they must not stall on a stuck reader. The bailing read
        # releases the capture itself when the open finally returns.
        self._closed = True
        if not self._lock.acquire(timeout=5.0):
            return
        try:
            self._release_capture()
        finally:
            self._lock.release()
