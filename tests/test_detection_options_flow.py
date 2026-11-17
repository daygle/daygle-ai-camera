"""The optional detection passes on real pixels: region boost, tiling, Second
Look and low-light enhancement, end to end through ``process_live_stream_alerts``.

Each cycle renders a frame with the cats painted in. The stub detector finds
them in whatever image it is handed - the frame, a boosted crop, a tile, a
mirrored re-check crop, an enhanced copy - and scores each by its apparent
size at the model input and its contrast, so a distant or dark cat scores
under the rule like a real model's would, and each pass has to recover it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

np = pytest.importorskip('numpy')
cv2 = pytest.importorskip('cv2')

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.main as app_main  # noqa: E402  -- break the circular-import gate
assert app_main is sys.modules['app.main']

from tests.test_live_track_ordering import _load_app  # noqa: E402

W, H, MODEL_INPUT = 1280, 720, 640
DISTANT = (0.012, 0.016)  # ~15 x 12 px: ~8 px at the model input


def _render(cats, background=110, contrast=60, noise=0, seed=0):
    frame = np.full((H, W, 3), background, dtype=np.int16)
    for x, y, w, h in cats:
        frame[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)] = background + contrast
    if noise:
        frame = frame + np.random.default_rng(seed).integers(-noise, noise + 1, frame.shape)
    return frame.clip(0, 255).astype(np.uint8)


class _PixelDetector:
    backend = 'onnx'
    available = True
    unavailable_reason = None

    def __init__(self):
        self.calls = 0

    def detect_frame(self, image, confidence=None):
        self.calls += 1
        grey = image.mean(axis=2)
        background = float(grey.min()) + 3.0
        noise = float(np.std(grey[grey < background + 8])) if (grey < background + 8).any() else 0.0
        mask = (np.abs(grey - background) > max(6.0, 3.0 * noise)).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
        height, width = grey.shape
        scale = MODEL_INPUT / max(height, width)
        found = []
        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if area < 4:
                continue
            contrast = abs(float(grey[y:y + h, x:x + w].mean()) - background)
            score = round(0.2 + 0.7 * min(1.0, max(w, h) * scale / 48.0) * min(1.0, contrast / 40.0), 3)
            if confidence is not None and score < confidence:
                continue
            found.append({'label': 'cat', 'confidence': score,
                          'box': {'x': x / width, 'y': y / height, 'width': w / width, 'height': h / height}})
        return found


def _drive(tmp_path, monkeypatch, scenes, live=None):
    main = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as lm

    settings = {'id': 'cam-options', 'name': 'Garden', 'recording': {'continuous': False},
                'detection': {'object_detection_enabled': True, 'zones': [{
                    'id': 'yard', 'name': 'Yard', 'enabled': True, 'monitor_objects': True, 'monitor_motion': False,
                    'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}, {'x': 0, 'y': 1}],
                    'object_rules': [{'label': 'cat', 'enabled': True, 'min_confidence': 0.5,
                                      'alert_schedules': [{'push_enabled': True}]}],
                }]}}
    if live:
        main.database.set_setting('live', live, main.utc_now())
    detector = _PixelDetector()
    monkeypatch.setattr(main._state, 'detector', detector)
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    current = {'cats': []}

    def motion(*_args, **_kwargs):
        mask = np.zeros((240, 320), dtype=bool)
        for x, y, w, h in current['cats']:
            mask[int(y * 240):int((y + h) * 240) + 1, int(x * 320):int((x + w) * 320) + 1] = True
        return bool(mask.any()), 0.5, mask, 0.02

    monkeypatch.setattr(lm, 'detect_frame_motion', motion)
    monkeypatch.setattr(lm, 'submit_alert_notification_with_verification', lambda *_a, **_k: None)
    statuses: list[dict] = []
    real_status = lm.update_live_detection_status
    monkeypatch.setattr(lm, 'update_live_detection_status',
                        lambda camera_id, **kwargs: (statuses.append(kwargs), real_status(camera_id, **kwargs)))
    start = time.time()
    results = []
    for index, scene in enumerate(scenes):
        current['cats'] = scene['cats']
        statuses.clear()
        now = start + index * 0.5
        monkeypatch.setattr(time, 'time', lambda now=now: now)
        monkeypatch.setattr(time, 'monotonic', lambda now=now: now)
        event_id = lm.process_live_stream_alerts(
            _render(scene['cats'], **scene.get('look', {})), {'width': W, 'height': H, 'timestamp': now},
            settings, enforce_interval=False,
        )
        cats = [d for d in statuses[-1].get('detections') or [] if d.get('label') == 'cat']
        results.append({'event_id': event_id, 'cats': cats})
    return results


def _alerts(results):
    return [index for index, result in enumerate(results) if result['event_id'] is not None]


def _distant_walk(cycles=5):
    # 0.9% of the frame per cycle: three quarters of the cat's own width.
    return [{'cats': [(0.30 + 0.009 * index, 0.40, *DISTANT)]} for index in range(cycles)]


@pytest.mark.parametrize('live', [
    {'object_detection_region_boost': True},
    {'object_detection_tiling': '4x4'},
    {'object_detection_second_look': True},
    {'object_detection_region_boost': True, 'object_detection_tiling': '3x3', 'object_detection_second_look': True},
], ids=['region-boost', 'tiling', 'second-look', 'all-on'])
def test_each_pass_recovers_a_distant_cat_once_and_follows_it(tmp_path, monkeypatch, live):
    assert _alerts(_drive(tmp_path, monkeypatch, _distant_walk())) == [], 'too small for the full-frame pass'
    results = _drive(tmp_path, monkeypatch, _distant_walk(), live=live)
    assert _alerts(results) == [0]
    for index, result in enumerate(results):
        assert len(result['cats']) == 1, 'one box, no duplicate from the extra pass'
        cat = result['cats'][0]
        assert cat['track_id'] == 1 and cat['motion_state'] == 'moving'
        assert cat['box']['x'] == pytest.approx(0.30 + 0.009 * index, abs=0.002)
        assert cat['box']['width'] == pytest.approx(DISTANT[0], abs=0.002)


def test_two_distant_cats_side_by_side_stay_two(tmp_path, monkeypatch):
    scenes = [{'cats': [(0.400 + 0.005 * i, 0.40, *DISTANT), (0.416 + 0.005 * i, 0.40, *DISTANT)]} for i in range(4)]
    live = {'object_detection_region_boost': True, 'object_detection_tiling': '4x4', 'object_detection_second_look': True}
    results = _drive(tmp_path, monkeypatch, scenes, live=live)
    assert all(len(result['cats']) == 2 for result in results)
    assert {cat['track_id'] for result in results for cat in result['cats']} == {1, 2}


@pytest.mark.parametrize('grid', ['3x3', '6x6'])
def test_tiling_never_splits_a_large_cat(tmp_path, monkeypatch, grid):
    scenes = [{'cats': [(0.25 + 0.02 * index, 0.25, 0.35, 0.45)]} for index in range(4)]
    results = _drive(tmp_path, monkeypatch, scenes, live={'object_detection_tiling': grid})
    assert _alerts(results) == [0] and all(len(result['cats']) == 1 for result in results)


def test_low_light_enhancement_recovers_a_dark_cat(tmp_path, monkeypatch):
    dark = {'background': 15, 'contrast': 14, 'noise': 3}
    scenes = [{'cats': [(0.30 + 0.02 * index, 0.40, 0.08, 0.08)], 'look': dark} for index in range(4)]
    assert _alerts(_drive(tmp_path, monkeypatch, scenes)) == []
    results = _drive(tmp_path, monkeypatch, scenes, live={'object_detection_low_light': 'auto'})
    assert _alerts(results) == [0] and all(len(result['cats']) == 1 for result in results)
