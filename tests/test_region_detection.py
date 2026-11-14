"""Tests for motion-region high-res inference (app/region_detection.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

import app.region_detection as rd  # noqa: E402


class _StubDetector:
    """Detector that 'finds' one object dead-centre of whatever crop it sees,
    recording the crop sizes so we can assert zoomed-in inference happened."""

    def __init__(self):
        self.crop_shapes = []

    def detect_frame(self, image, confidence=None):
        self.crop_shapes.append(image.shape[:2])
        return [{'label': 'person', 'confidence': 0.8,
                 'box': {'x': 0.4, 'y': 0.4, 'width': 0.2, 'height': 0.2}}]


def _mask_with_blob(h=240, w=320, region=(0.6, 0.6, 0.15, 0.15)):
    mask = np.zeros((h, w), dtype=bool)
    rx, ry, rw, rh = region
    x1, y1 = int(rx * w), int(ry * h)
    x2, y2 = int((rx + rw) * w), int((ry + rh) * h)
    mask[y1:y2, x1:x2] = True
    return mask


def test_region_boxes_found_and_capped():
    mask = _mask_with_blob()
    boxes = rd._motion_region_boxes(mask, max_regions=3, min_area_frac=0.001, pad_frac=0.2)
    assert 1 <= len(boxes) <= 3
    x, y, w, h = boxes[0]
    assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and w > 0 and h > 0


def test_edge_padding_clips_at_original_region_bounds():
    mask = _mask_with_blob(h=100, w=100, region=(0, 0, 0.1, 0.1))
    x, y, w, h = rd._motion_region_boxes(mask, max_regions=1, min_area_frac=0.001, pad_frac=0.25)[0]
    assert (x, y) == (0, 0)
    assert w == pytest.approx(0.11 * 1.25)
    assert h == pytest.approx(0.11 * 1.25)
    tiles = rd._tile_boxes(3, 2, 0.2)
    assert tiles[0][2] == pytest.approx(1.2 / 3)
    assert tiles[0][3] == pytest.approx(1.2 / 2)


def test_crop_detections_map_from_actual_pixel_bounds(monkeypatch):
    frame = np.zeros((101, 103, 3), dtype=np.uint8)
    monkeypatch.setattr(rd, '_motion_region_boxes', lambda *args, **kwargs: [(0.101, 0.201, 0.3, 0.4)])
    boosted = rd.detect_with_region_boost(_StubDetector(), frame, np.ones((2, 2)), [])[0]['box']
    assert boosted['x'] == round((10 + 0.4 * (41 - 10)) / 103, 4)
    assert boosted['y'] == round((20 + 0.4 * (60 - 20)) / 101, 4)
    tiles = rd.detect_with_tiling(_StubDetector(), frame, [], cols=2, rows=1, overlap=0)
    assert tiles[0]['box']['x'] == round(0.4 * 51 / 103, 4)


def test_region_boxes_skip_full_frame_motion():
    full = np.ones((240, 320), dtype=bool)  # whole frame moving
    assert rd._motion_region_boxes(full, max_regions=3, min_area_frac=0.001, pad_frac=0.2) == []


def test_boost_remaps_crop_boxes_into_full_frame_and_merges():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    mask = _mask_with_blob()
    det = _StubDetector()
    base = [{'label': 'car', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}}]
    out = rd.detect_with_region_boost(det, frame, mask, base, confidence=0.3)
    # Base detection preserved + a boosted one added.
    assert any(d.get('region_boost') for d in out)
    assert any(d['label'] == 'car' for d in out)
    boosted = [d for d in out if d.get('region_boost')][0]
    # The remapped box must land inside the lower-right region, not the crop's
    # own (0.4) coordinate space.
    assert boosted['box']['x'] > 0.4
    # The crop handed to the detector was smaller than the full frame (zoom-in).
    assert det.crop_shapes and det.crop_shapes[0][0] < 720


def test_boost_skips_malformed_crop_boxes():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    mask = _mask_with_blob(h=100, w=100)

    class _MalformedDetector:
        def detect_frame(self, image, confidence=None):
            return [
                {'label': 'person', 'confidence': 0.9, 'box': {'x': float('nan'), 'y': 0.1, 'width': 0.2, 'height': 0.2}},
                {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}},
            ]

    out = rd.detect_with_region_boost(_MalformedDetector(), frame, mask, [])
    assert len(out) == 1
    assert out[0]['box']['x'] >= 0


def test_boost_is_noop_without_mask_or_detect_frame():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    base = [{'label': 'person', 'confidence': 0.5, 'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}}]
    assert rd.detect_with_region_boost(_StubDetector(), frame, None, base) == base

    class _NoDetectFrame:
        pass
    assert rd.detect_with_region_boost(_NoDetectFrame(), frame, _mask_with_blob(), base) == base


def test_region_and_tile_failures_log_coordinates_and_tracebacks(caplog):
    frame = np.zeros((100, 160, 3), dtype=np.uint8)
    base = []

    class _FailingDetector:
        def detect_frame(self, image, confidence=None):
            raise RuntimeError("inference failed")

    with caplog.at_level("DEBUG", logger="daygle.ai"):
        rd.detect_with_region_boost(_FailingDetector(), frame, _mask_with_blob(), base)
        rd.detect_with_tiling(_FailingDetector(), frame, base, cols=2, rows=2)

    crop_records = [r for r in caplog.records if "Region-boost detector failed" in r.getMessage()]
    tile_records = [r for r in caplog.records if "Tiled detector failed" in r.getMessage()]
    assert crop_records and tile_records
    assert all(record.exc_info for record in crop_records + tile_records)
    assert all("pixels=" in record.getMessage() and "normalized=" in record.getMessage()
               for record in crop_records + tile_records)


def test_dedup_keeps_nearby_same_label_boxes_below_iou_threshold():
    dets = [
        {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
        {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.18, 'y': 0.4, 'width': 0.1, 'height': 0.1}},
    ]
    assert len(rd._dedup_by_iou(dets, 0.5)) == 2


def test_dedup_prefers_higher_confidence_same_label():
    dets = [
        {'label': 'person', 'confidence': 0.6, 'box': {'x': 0.5, 'y': 0.5, 'width': 0.2, 'height': 0.2}},
        {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.51, 'y': 0.5, 'width': 0.2, 'height': 0.2}},
    ]
    kept = rd._dedup_by_iou(dets, 0.5)
    assert len(kept) == 1 and kept[0]['confidence'] == 0.9


# --- tiling ----------------------------------------------------------------

def test_parse_tile_grid():
    assert rd.parse_tile_grid('off') is None
    assert rd.parse_tile_grid('') is None
    assert rd.parse_tile_grid(None) is None
    assert rd.parse_tile_grid('3x3') == (3, 3)
    assert rd.parse_tile_grid('3x2') == (3, 2)
    assert rd.parse_tile_grid(True) is None        # only 'CxR' strings
    assert rd.parse_tile_grid('1x1') is None       # no actual tiling
    assert rd.parse_tile_grid('99x99') is None     # out of range
    assert rd.parse_tile_grid('junk') is None


def test_tile_boxes_cover_grid_within_bounds():
    boxes = rd._tile_boxes(3, 2, 0.2)
    assert len(boxes) == 6
    for x, y, w, h in boxes:
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
        assert w > 0 and h > 0 and x + w <= 1.0001 and y + h <= 1.0001


def test_tiling_recovers_small_object_and_remaps():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    det = _StubDetector()  # 'finds' a 0.2x0.2 object centred in each tile
    out = rd.detect_with_tiling(det, frame, [], cols=3, rows=3, confidence=0.3)
    # 3x3 tiles -> 9 inferences; a 0.2-of-tile box is small after remap so it
    # survives the large-object filter.
    assert len(det.crop_shapes) == 9
    assert out and all(d.get('tiled') for d in out)
    for d in out:
        b = d['box']
        assert 0.0 <= b['x'] <= 1.0 and b['width'] < 0.2  # remapped smaller than a full tile


def test_tiling_drops_large_tile_detections(caplog):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    class _BigDetector:
        def detect_frame(self, image, confidence=None):
            # An object filling the whole tile -> remaps large -> must be dropped.
            return [{'label': 'car', 'confidence': 0.9, 'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}}]

    with caplog.at_level("DEBUG", logger="daygle.ai"):
        out = rd.detect_with_tiling(_BigDetector(), frame, [], cols=2, rows=2, confidence=0.3)
    assert out == []  # all tile detections were too large and dropped
    drops = [r for r in caplog.records if "Dropping large tiled detection" in r.getMessage()]
    assert len(drops) == 4
    assert all("detection_area_frac=" in r.getMessage() for r in drops)


def test_tiling_noop_without_detect_frame():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    base = [{'label': 'person', 'confidence': 0.5, 'box': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}}]

    class _NoDetectFrame:
        pass
    assert rd.detect_with_tiling(_NoDetectFrame(), frame, base, cols=2, rows=2) == base


def test_dedup_drops_partial_crop_box_nested_in_full_frame_box():
    """A crop that only saw a person's legs yields a box inside the
    full-frame person box with IoU well under 0.5; it is still a duplicate."""
    dets = [
        {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.6}},
        {'label': 'person', 'confidence': 0.7, 'box': {'x': 0.12, 'y': 0.45, 'width': 0.15, 'height': 0.25}},
    ]
    kept = rd._dedup_by_iou(dets, 0.5)
    assert len(kept) == 1 and kept[0]['confidence'] == 0.9


def test_dedup_keeps_nested_box_of_different_label():
    dets = [
        {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.6}},
        {'label': 'backpack', 'confidence': 0.7, 'box': {'x': 0.12, 'y': 0.2, 'width': 0.08, 'height': 0.1}},
    ]
    assert len(rd._dedup_by_iou(dets, 0.5)) == 2


def test_dedup_drops_top_bottom_split_of_one_person():
    """A crop/tile that saw a person's upper body next to a full-frame box of
    their lower body: same column, IoU ~0.40, containment ~0.59."""
    dets = [
        {'label': 'person', 'confidence': 0.92, 'box': {'x': 0.400, 'y': 0.397, 'width': 0.1775, 'height': 0.540}},
        {'label': 'person', 'confidence': 0.88, 'box': {'x': 0.380, 'y': 0.241, 'width': 0.195, 'height': 0.455}},
    ]
    kept = rd._dedup_by_iou(dets, 0.5)
    assert len(kept) == 1 and kept[0]['confidence'] == 0.92


def test_dedup_keeps_side_by_side_people():
    dets = [
        {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.6}},
        {'label': 'person', 'confidence': 0.8, 'box': {'x': 0.24, 'y': 0.12, 'width': 0.2, 'height': 0.6}},
    ]
    assert len(rd._dedup_by_iou(dets, 0.5)) == 2
