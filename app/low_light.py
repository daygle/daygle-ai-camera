"""Low-light / IR contrast enhancement for the object detector's input.

YOLO models are trained mostly on daylight photos. A night frame from a
budget camera is dark and flat, and an IR frame is a grey, low-contrast image
where a person at the edge of the illuminator is a faint smudge. Local contrast
equalisation (CLAHE) lifts that subject out of the background without blowing
out the lit foreground the way a global brightness boost would.

Only the copy handed to the detector is enhanced. Snapshots, recordings, the
motion model and the face pass all keep the original frame, so the operator
never sees a processed picture and the motion background is not disturbed by a
mode switch. Detector boxes are normalized to the frame, so they map back to
the original unchanged.

The ``object_detection_low_light`` setting is tri-state:

- ``'off'`` (default) - the detector always sees the raw frame,
- ``'auto'`` - enhance only while the frame is dark (mean brightness below
  ``LOW_LIGHT_BRIGHTNESS``), which covers night colour and IR,
- ``'on'`` - enhance every frame (a camera that is always dim).

Off by default like region boost and tiling: CLAHE can also lift sensor noise,
so validate it on your own night footage (``docs/detection-benchmarking.md``)
before turning it on.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger('daygle.ai')

LOW_LIGHT_MODES = ('off', 'auto', 'on')

# Mean 0-255 brightness below which ``auto`` treats a frame as dark. Matches
# the motion engine's night threshold (``app.state._MOTION_NIGHT_BRIGHTNESS``)
# so "night" means the same thing to both.
LOW_LIGHT_BRIGHTNESS = 50.0

# A frame whose colour channels differ by less than this on average is treated
# as greyscale (IR): CLAHE then runs on the single grey channel instead of LAB.
_GREYSCALE_CHANNEL_SPREAD = 4.0

# CLAHE strength. A clip limit of 2 is the common default: enough to pull a
# subject out of a dark background without turning sensor noise into texture.
_CLAHE_CLIP_LIMIT = 2.0
_CLAHE_TILE_GRID = (8, 8)

# Brightness / greyscale checks run on a small thumbnail; the cost is
# negligible next to inference.
_THUMB_SIZE = (64, 36)


def normalize_low_light_mode(value: Any) -> str:
    """Coerce a setting value to ``'off'`` / ``'auto'`` / ``'on'``.

    Booleans map to ``'on'`` / ``'off'`` so a hand-edited ``true`` works.
    Anything unrecognised is ``'off'``.
    """
    if isinstance(value, bool):
        return 'on' if value else 'off'
    text = str(value or '').strip().lower()
    if text in ('true', '1', 'yes'):
        return 'on'
    return text if text in LOW_LIGHT_MODES else 'off'


def _thumbnail_stats(image: Any) -> tuple[float, bool]:
    """Return ``(mean brightness 0-255, looks greyscale)`` for a BGR frame."""
    import cv2
    import numpy as np

    thumb = cv2.resize(image, _THUMB_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32)
    brightness = float(thumb.mean())
    if thumb.ndim != 3 or thumb.shape[2] < 3:
        return brightness, True
    blue, green, red = thumb[..., 0], thumb[..., 1], thumb[..., 2]
    spread = float((np.abs(blue - green) + np.abs(green - red)).mean() / 2.0)
    return brightness, spread < _GREYSCALE_CHANNEL_SPREAD


def enhance_low_light(image: Any, *, greyscale: bool | None = None) -> Any:
    """Return a contrast-equalised copy of a BGR frame (CLAHE).

    Colour frames are equalised on the LAB lightness channel so hues are kept;
    greyscale / IR frames are equalised on one grey channel and stacked back to
    three channels, the layout the detector expects.
    """
    import cv2

    if greyscale is None:
        _brightness, greyscale = _thumbnail_stats(image)
    clahe = cv2.createCLAHE(clipLimit=_CLAHE_CLIP_LIMIT, tileGridSize=_CLAHE_TILE_GRID)
    if greyscale:
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        return cv2.cvtColor(clahe.apply(grey), cv2.COLOR_GRAY2BGR)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lightness, a_channel, b_channel = cv2.split(lab)
    lab = cv2.merge((clahe.apply(lightness), a_channel, b_channel))
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def low_light_detection_frame(image: Any, mode: Any) -> tuple[Any, bool]:
    """Return ``(frame for the detector, whether it was enhanced)``.

    Returns the original frame unchanged when the mode is off, the frame is not
    a BGR numpy image, ``auto`` finds the scene bright enough, or enhancement
    fails - so it is always safe to call and never raises.
    """
    mode = normalize_low_light_mode(mode)
    if mode == 'off':
        return image, False
    if not (hasattr(image, 'shape') and getattr(image, 'ndim', 0) == 3 and image.shape[2] == 3):
        return image, False
    if image.dtype.name != 'uint8':
        return image, False
    try:
        brightness, greyscale = _thumbnail_stats(image)
        if mode == 'auto' and brightness >= LOW_LIGHT_BRIGHTNESS:
            return image, False
        return enhance_low_light(image, greyscale=greyscale), True
    except Exception:  # noqa: BLE001 - enhancement is best-effort
        logger.debug('Low-light enhancement failed; using the raw frame', exc_info=True)
        return image, False
