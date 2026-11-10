"""Per-label box colours in the server-side snapshot overlay
(app/live_snapshot.render_live_snapshot_jpeg_overlay)."""
from __future__ import annotations

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from app import live_snapshot  # noqa: E402

W, H = 400, 300


def _black_jpeg():
    ok, encoded = cv2.imencode('.jpg', np.zeros((H, W, 3), dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def _left_edge_bgr(jpeg, box_x=0.2):
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    # Mid-height on the box's left edge: clear of the label pill on top.
    return image[H // 2, int(box_x * W)].astype(int)


def _detection(label, **extra):
    return {'label': label, 'confidence': 0.9,
            'box': {'x': 0.2, 'y': 0.2, 'width': 0.5, 'height': 0.6}, **extra}


def _close(actual, expected_bgr, tol=40):
    return all(abs(int(a) - int(e)) <= tol for a, e in zip(actual, expected_bgr))


def test_label_with_custom_colour_draws_in_that_colour():
    out = live_snapshot.render_live_snapshot_jpeg_overlay(
        _black_jpeg(), [_detection('person')], box_colors={'person': '#ff0000'})
    assert _close(_left_edge_bgr(out), (0, 0, 255))  # red, as BGR


def test_label_without_custom_colour_keeps_default():
    out = live_snapshot.render_live_snapshot_jpeg_overlay(
        _black_jpeg(), [_detection('car')], box_colors={'person': '#ff0000'})
    assert _close(_left_edge_bgr(out), live_snapshot._OVERLAY_BOX_BGR)


def test_motion_box_uses_motion_colour():
    out = live_snapshot.render_live_snapshot_jpeg_overlay(
        _black_jpeg(), [_detection('motion', motion_event=True)], box_colors={'motion': '#ff0000'})
    assert _close(_left_edge_bgr(out), (0, 0, 255))  # red, as BGR


def test_motion_event_box_uses_motion_colour_not_its_label_colour():
    out = live_snapshot.render_live_snapshot_jpeg_overlay(
        _black_jpeg(), [_detection('person', motion_event=True)],
        box_colors={'person': '#ff0000', 'motion': '#0000ff'})
    assert _close(_left_edge_bgr(out), (255, 0, 0))  # blue, as BGR


def test_motion_box_without_motion_colour_keeps_default():
    out = live_snapshot.render_live_snapshot_jpeg_overlay(
        _black_jpeg(), [_detection('motion', motion_event=True)], box_colors={'person': '#ff0000'})
    assert _close(_left_edge_bgr(out), live_snapshot._OVERLAY_BOX_BGR)


def test_colours_default_to_object_settings(monkeypatch):
    import app.object_settings as object_settings
    monkeypatch.setattr(object_settings, 'overlay_box_colors', lambda: {'person': '#0000ff'})
    out = live_snapshot.render_live_snapshot_jpeg_overlay(_black_jpeg(), [_detection('person')])
    assert _close(_left_edge_bgr(out), (255, 0, 0))  # blue, as BGR


@pytest.mark.parametrize("value", ['#zzzzzz', 'red', '', None, '#fff'])
def test_hex_to_bgr_rejects_junk(value):
    assert live_snapshot._hex_to_bgr(value) is None
