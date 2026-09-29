"""Where camera video is decoded: on the CPU, or on an NVIDIA GPU (NVDEC).

The per-camera ingest ffmpeg (app.recordings) decodes the camera stream to
produce the frames that motion, object and face detection read. H.264/H.265
cannot skip frames, so the whole stream is decoded even though only a few
frames a second are kept - usually the largest CPU cost of the app. NVIDIA
cards have a dedicated video decoder (NVDEC), separate from the CUDA cores
that run detection, which takes that work off the CPU.

The ``video_decode`` live setting chooses:

* ``auto`` (default) - the GPU when an NVIDIA card is present and this ffmpeg
  build supports CUDA decoding, else the CPU;
* ``gpu`` - the GPU whenever ffmpeg supports it;
* ``cpu`` - always the CPU.

Stream-copied outputs (event pre-roll, continuous recording) and audio are
never decoded, so they are unaffected. A camera whose GPU decode fails falls
back to the CPU on its own (see RecordingService._run_prebuffer_worker).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

VIDEO_DECODE_MODES = ('auto', 'gpu', 'cpu')
DEFAULT_VIDEO_DECODE = 'auto'
# Probe results are cached: a driver or ffmpeg change is picked up within this
# window without spawning two processes per camera reconnect.
_PROBE_TTL_SECONDS = 600.0
# ffmpeg stderr fragments that mean the CUDA decoder itself failed (as opposed
# to the camera or network), so the camera should fall back to the CPU.
GPU_DECODE_ERROR_MARKERS = (
    'cuda', 'nvdec', 'cuvid', 'hwaccel', 'device creation failed', 'no device available',
    'hardware device', 'hw_device',
)

_lock = threading.Lock()
_probe: dict[str, object] = {'at': 0.0, 'ffmpeg_cuda': False, 'nvidia_gpu': False}


def normalize_video_decode(value: object) -> str:
    mode = str(value or DEFAULT_VIDEO_DECODE).strip().lower()
    return mode if mode in VIDEO_DECODE_MODES else DEFAULT_VIDEO_DECODE


def _run(command: list[str], timeout: float = 5.0) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:  # noqa: BLE001 - a failed probe only means "decode on the CPU"
        return None
    return result.stdout if result.returncode == 0 else None


def probe_gpu_decode(*, refresh: bool = False) -> dict[str, bool]:
    """``{'ffmpeg_cuda': ..., 'nvidia_gpu': ...}``, cached for a few minutes."""
    with _lock:
        if not refresh and time.monotonic() - float(_probe['at']) < _PROBE_TTL_SECONDS and _probe['at']:
            return {'ffmpeg_cuda': bool(_probe['ffmpeg_cuda']), 'nvidia_gpu': bool(_probe['nvidia_gpu'])}
    ffmpeg = shutil.which('ffmpeg')
    hwaccels = _run([ffmpeg, '-hide_banner', '-hwaccels']) if ffmpeg else None
    ffmpeg_cuda = bool(hwaccels) and any(line.strip() == 'cuda' for line in hwaccels.splitlines())
    smi = shutil.which('nvidia-smi')
    nvidia_gpu = bool(smi) and bool((_run([smi, '-L']) or '').strip())
    with _lock:
        _probe.update(at=time.monotonic(), ffmpeg_cuda=ffmpeg_cuda, nvidia_gpu=nvidia_gpu)
    return {'ffmpeg_cuda': ffmpeg_cuda, 'nvidia_gpu': nvidia_gpu}


def resolve_video_decode(mode: object) -> str:
    """The decoder to use now for a setting: ``'gpu'`` or ``'cpu'``."""
    mode = normalize_video_decode(mode)
    if mode == 'cpu':
        return 'cpu'
    probe = probe_gpu_decode()
    if not probe['ffmpeg_cuda']:
        return 'cpu'
    if mode == 'gpu':
        return 'gpu'
    return 'gpu' if probe['nvidia_gpu'] else 'cpu'


def hwaccel_input_args(decode: str) -> list[str]:
    """ffmpeg input options for a decoder. Without ``-hwaccel_output_format``
    the decoded frames are copied back to system memory, so the existing fps
    and JPEG outputs work unchanged."""
    return ['-hwaccel', 'cuda'] if decode == 'gpu' else []


def is_gpu_decode_error(stderr: str) -> bool:
    text = (stderr or '').lower()
    return any(marker in text for marker in GPU_DECODE_ERROR_MARKERS)
