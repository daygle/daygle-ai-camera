"""Low-light enhancement (app/low_light.py) and second look (app/second_look.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

import app.low_light as ll  # noqa: E402
import app.second_look as sl  # noqa: E402


# --- Low-light enhancement -------------------------------------------------

def _dark_frame(colour=True):
    rng = np.random.default_rng(1)
    frame = rng.integers(10, 30, size=(180, 320, 3), dtype=np.uint8)
    frame[60:120, 140:180] = 45  # a faint subject
    if not colour:
        frame[..., 1] = frame[..., 0]
        frame[..., 2] = frame[..., 0]
    return frame


def test_mode_normalization():
    assert ll.normalize_low_light_mode('AUTO') == 'auto'
    assert ll.normalize_low_light_mode(True) == 'on'
    assert ll.normalize_low_light_mode(False) == 'off'
    assert ll.normalize_low_light_mode('bogus') == 'off'
    assert ll.normalize_low_light_mode(None) == 'off'


def test_off_returns_the_same_frame():
    frame = _dark_frame()
    out, applied = ll.low_light_detection_frame(frame, 'off')
    assert out is frame and applied is False


def test_auto_skips_bright_frames_and_enhances_dark_ones():
    bright = np.full((180, 320, 3), 160, dtype=np.uint8)
    out, applied = ll.low_light_detection_frame(bright, 'auto')
    assert out is bright and not applied

    dark = _dark_frame()
    original = dark.copy()
    out, applied = ll.low_light_detection_frame(dark, 'auto')
    assert applied
    assert out.shape == dark.shape and out.dtype == np.uint8
    assert np.array_equal(dark, original)  # the source frame is never modified
    # Subject-vs-background contrast is higher after enhancement.
    def contrast(img):
        return float(img[60:120, 140:180].mean() - img[:50].mean())
    assert contrast(out) > contrast(dark)


def test_ir_frames_stay_greyscale():
    out, applied = ll.low_light_detection_frame(_dark_frame(colour=False), 'on')
    assert applied
    assert np.array_equal(out[..., 0], out[..., 1]) and np.array_equal(out[..., 1], out[..., 2])


def test_non_bgr_input_is_passed_through():
    grey = np.zeros((10, 10), dtype=np.uint8)
    assert ll.low_light_detection_frame(grey, 'on') == (grey, False)
    assert ll.low_light_detection_frame(b'jpeg', 'on') == (b'jpeg', False)


# --- Second look -----------------------------------------------------------

class _CropDetector:
    """Answers each crop with a fixed result list, recording what it saw.

    ``crop_result`` / ``mirror_result`` are the detections returned for the
    plain crop and the mirrored crop (alternate calls)."""

    def __init__(self, crop_result, mirror_result):
        self.results = [crop_result, mirror_result]
        self.calls = []

    def detect_frame(self, image, confidence=None):
        self.calls.append((image.shape[:2], confidence))
        return self.results[(len(self.calls) - 1) % 2]


def _centre(label='person', confidence=0.7):
    # The candidate sits in the middle of its context crop, so a centred crop
    # box maps back onto it (and mirroring a centred box is a no-op).
    return {'label': label, 'confidence': confidence,
            'box': {'x': 0.3, 'y': 0.3, 'width': 0.4, 'height': 0.4}}


def _frame():
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _candidate(confidence=0.35):
    return {'label': 'person', 'confidence': confidence,
            'box': {'x': 0.45, 'y': 0.45, 'width': 0.04, 'height': 0.08}}


def test_floor():
    assert sl.second_look_floor(0.5) == pytest.approx(0.3)
    assert sl.second_look_floor(0.05) == pytest.approx(0.05)


def test_above_threshold_passes_untouched_and_no_extra_inference():
    detector = _CropDetector([], [])
    strong = {'label': 'car', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}
    out = sl.confirm_borderline_detections(detector, _frame(), [strong], threshold=0.5)
    assert out == [strong] and detector.calls == []


def test_confirmed_near_miss_is_kept_with_rechecked_confidence():
    detector = _CropDetector([_centre(confidence=0.7)], [_centre(confidence=0.6)])
    out = sl.confirm_borderline_detections(detector, _frame(), [_candidate()], threshold=0.5)
    assert len(out) == 1
    kept = out[0]
    assert kept['second_look'] is True
    assert kept['confidence'] == pytest.approx(0.65)
    assert abs(kept['box']['x'] + kept['box']['width'] / 2 - 0.47) < 0.02  # mapped back near the candidate
    assert len(detector.calls) == 2
    crop_shape, floor = detector.calls[0]
    assert crop_shape[0] < 720 and crop_shape[1] < 1280  # zoomed in
    assert floor == pytest.approx(0.3)


def test_unconfirmed_near_miss_is_dropped():
    # The mirrored view does not agree, so the average stays under 0.5.
    detector = _CropDetector([_centre(confidence=0.6)], [])
    assert sl.confirm_borderline_detections(detector, _frame(), [_candidate()], threshold=0.5) == []


def test_wrong_label_or_wrong_place_does_not_confirm():
    elsewhere = {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.0, 'y': 0.0, 'width': 0.1, 'height': 0.1}}
    detector = _CropDetector([_centre(label='dog', confidence=0.9), elsewhere], [_centre(label='dog', confidence=0.9)])
    assert sl.confirm_borderline_detections(detector, _frame(), [_candidate()], threshold=0.5) == []


def test_relevance_filter_and_candidate_cap():
    detector = _CropDetector([_centre(confidence=0.9)], [_centre(confidence=0.9)])
    candidates = [_candidate(0.3), _candidate(0.45), _candidate(0.4)]
    out = sl.confirm_borderline_detections(
        detector, _frame(), candidates, threshold=0.5, max_candidates=1,
    )
    assert len(out) == 1 and len(detector.calls) == 2  # only the strongest (0.45) re-checked

    detector = _CropDetector([_centre(confidence=0.9)], [_centre(confidence=0.9)])
    out = sl.confirm_borderline_detections(
        detector, _frame(), candidates, threshold=0.5, is_relevant=lambda _c: [],
    )
    assert out == [] and detector.calls == []


def test_detector_failure_drops_candidate_without_raising():
    class _Broken:
        def detect_frame(self, image, confidence=None):
            raise RuntimeError('boom')

    assert sl.confirm_borderline_detections(_Broken(), _frame(), [_candidate()], threshold=0.5) == []


def test_enabled_flag():
    assert sl.second_look_enabled({}) is False
    assert sl.second_look_enabled({'object_detection_second_look': 'true'}) is True
