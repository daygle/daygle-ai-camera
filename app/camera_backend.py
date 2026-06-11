from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import threading
import time
from typing import Any


logger = logging.getLogger('daygle.ai')
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
    global _avutil_libs, _avutil_libs_searched

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
                except Exception:
                    pass

                # Scan /proc/self/maps for any libavutil already mapped into this
                # process (populated after cv2.VideoCapture() opens the stream).
                try:
                    with open('/proc/self/maps') as _maps:
                        for _line in _maps:
                            if 'libavutil' in _line and '.so' in _line:
                                _parts = _line.rstrip().split()
                                if _parts and _parts[-1].startswith('/') and _parts[-1] not in lib_paths:
                                    lib_paths.append(_parts[-1])
                except Exception:
                    pass

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

    def __init__(self, stream_url: str, width: int = 1280, height: int = 720, fps: int = 15, stale_frame_grabs: int | None = None) -> None:
        self.stream_url = stream_url
        self.width = width
        self.height = height
        self.fps = fps
        self._stale_frame_grabs_configured = stale_frame_grabs
        self.frame_number = 0
        self.started_at = time.time()
        self.last_error: str | None = None
        self._capture: Any | None = None
        self._lock = threading.RLock()

    @property
    def backend(self) -> str:
        return "onvif"

    def get_frame(self, timestamp: float | None = None) -> dict[str, Any]:
        return {
            "frame_number": self.frame_number,
            "timestamp": timestamp if timestamp is not None else time.time(),
            "width": self.width,
            "height": self.height,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "stream_url": self.stream_url,
            "last_error": self.last_error,
        }

    def _open_capture(self):
        if not self.stream_url:
            raise RuntimeError("ONVIF/RTSP stream URL is not configured.")

        # Prefer TCP for RTSP cameras. UDP packet loss and frequent reconnects
        # can make inexpensive ONVIF cameras fail during session setup.
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|max_delay;500000|stimeout;5000000|fflags;discardcorrupt")

        import cv2

        if self._capture is None:
            self._capture = cv2.VideoCapture(self.stream_url)
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
        return self._capture

    def _release_capture(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def read_frame(self) -> tuple[Any, dict[str, Any]]:
        """Read the latest frame as a raw BGR numpy array, skipping JPEG encoding.

        This avoids the encode→decode round-trip when the caller (e.g.
        the ONNX detector) works on numpy arrays directly, saving ~30-90 ms
        per detection cycle.
        """
        with self._lock:
            capture = self._open_capture()
            import cv2

            capture_ts = time.time()
            ok, image = self._read_latest_frame(capture, self._stale_frame_grabs())
            if not ok or image is None:
                self._release_capture()
                capture = self._open_capture()
                ok, image = self._read_latest_frame(capture, self._stale_frame_grabs())

            if not ok or image is None:
                self._release_capture()
                self.last_error = "Unable to read a frame from the ONVIF/RTSP stream."
                raise RuntimeError(self.last_error)

            height, width = image.shape[:2]
            self.width = int(width)
            self.height = int(height)
            self.frame_number += 1
        self.last_error = None
        return image, self.get_frame(capture_ts)

    def read_jpeg(self) -> tuple[bytes, dict[str, Any]]:
        with self._lock:
            capture = self._open_capture()
            import cv2

            # Capture timestamp BEFORE reading the frame so detection track
            # samples are stamped close to when the frame left the camera,
            # not after stale-frame grabs + JPEG encoding which can lag by
            # hundreds of milliseconds and shift every playback overlay box.
            capture_ts = time.time()

            ok, image = self._read_latest_frame(capture, self._stale_frame_grabs())
            if not ok or image is None:
                self._release_capture()
                capture = self._open_capture()
                ok, image = self._read_latest_frame(capture, self._stale_frame_grabs())

            if not ok or image is None:
                self._release_capture()
                self.last_error = "Unable to read a frame from the ONVIF/RTSP stream."
                raise RuntimeError(self.last_error)

            height, width = image.shape[:2]
            self.width = int(width)
            self.height = int(height)
            self.frame_number += 1
            ok, encoded = cv2.imencode(".jpg", image)
            if not ok:
                self.last_error = "Unable to encode ONVIF/RTSP frame as JPEG."
                raise RuntimeError(self.last_error)
        self.last_error = None
        return encoded.tobytes(), self.get_frame(capture_ts)

    def _stale_frame_grabs(self) -> int:
        if self._stale_frame_grabs_configured is not None:
            return max(0, self._stale_frame_grabs_configured)
        # Default: grab ~25% of a second worth of frames to drain the RTSP
        # buffer and land on the latest one.  The old formula used 50%
        # (fps/2), which at 15 fps meant 7 grabs (~467 ms of latency).
        # Dropping to 25% (fps/4) halves that to ~233 ms while still
        # discarding enough stale frames on typical IP cameras.
        return max(1, min(8, int(self.fps / 4)))

    @staticmethod
    def _read_latest_frame(capture, stale_frame_grabs: int) -> tuple[bool, Any]:
        if hasattr(capture, "grab"):
            for _ in range(stale_frame_grabs):
                if not capture.grab():
                    break
        return capture.read()

    def snapshot(self) -> dict[str, Any]:
        _jpeg, frame = self.read_jpeg()
        frame["snapshot"] = True
        return frame

    def close(self) -> None:
        with self._lock:
            self._release_capture()
