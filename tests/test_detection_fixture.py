"""Tests for the checked-in benchmark fixture scenes.

The fixture exists because every per-camera number in the performance roadmap
(segment-timeline caching, post-process pool backpressure, the guided
input-size sweep) needed a labeled dataset to run against, and none was in the
repository -- ``docs/detection-benchmarking.md`` referenced
``fixtures/front-door.json`` as if it existed.

The scenes are drawn FROM their ground-truth file, so the strongest property
worth pinning is that the labels and the pixels cannot disagree: every
annotated box must be visible at exactly its annotated position and size, and
an unannotated frame must be pixel-identical to the empty background. If that
ever breaks, every metric computed from the fixture becomes fiction.

The motion-gate assertions need cv2, which the sandbox does not have, so they
skip rather than fail; CI installs requirements.txt and runs them.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'scripts'))
sys.path.insert(0, str(REPO_ROOT / 'fixtures'))

import render_fixture  # noqa: E402
from evaluate_detection import load_ground_truth  # noqa: E402

FIXTURE_DIR = REPO_ROOT / 'fixtures'
SCENES = ('front-door', 'driveway')


# ─── The scene files themselves ──────────────────────────────────────────────


@pytest.mark.parametrize('name', SCENES)
def test_scene_file_is_a_valid_ground_truth_document(name):
    ground_truth = load_ground_truth(FIXTURE_DIR / f'{name}.json')
    assert ground_truth, f'{name} has no frames'
    # Zero-based, contiguous indexes: the evaluator decodes frames in sorted
    # filename order, so a gap would silently shift every annotation.
    assert sorted(ground_truth) == list(range(len(ground_truth)))


@pytest.mark.parametrize('name', SCENES)
def test_scene_is_labelled_synthetic_and_describes_itself(name):
    document = json.loads((FIXTURE_DIR / f'{name}.json').read_text(encoding='utf-8'))
    assert document['version'] == 1
    # A synthetic scene that claimed otherwise would be a trap for anyone
    # quoting its precision numbers as camera evidence.
    assert document['synthetic'] is True
    assert len(document['description']) > 80
    assert document['frame_count'] == len(document['frames'])


@pytest.mark.parametrize('name', SCENES)
def test_scene_contains_both_negatives_and_positives(name):
    ground_truth = load_ground_truth(FIXTURE_DIR / f'{name}.json')
    empty = [index for index, objects in ground_truth.items() if not objects]
    populated = [index for index, objects in ground_truth.items() if objects]
    # A dataset of only positives cannot reveal a false-positive regression,
    # which is the whole reason the lead-in/follow-through frames are there.
    assert len(empty) >= 5, f'{name} needs empty frames to measure false positives'
    assert len(populated) >= 20, f'{name} needs enough activity frames to measure recall'


def test_driveway_covers_the_cases_a_doorway_cannot():
    ground_truth = load_ground_truth(FIXTURE_DIR / 'driveway.json')
    labels = {
        obj['label']
        for objects in ground_truth.values()
        for obj in objects
    }
    assert labels == {'car', 'dog', 'cat'}
    difficult = [
        obj
        for objects in ground_truth.values()
        for obj in objects
        if obj.get('difficult')
    ]
    assert difficult, 'the difficult/partial-visibility case is unrepresented'

    # The car arrives and then STOPS. That transition is the point of the scene:
    # it is what a "moving only" object mode and a still-for-N dwell alert must
    # disagree about, and a fixture where every object moves would never surface
    # the bug.
    car_present = [index for index, objects in ground_truth.items()
                   if any(obj['label'] == 'car' for obj in objects)]
    car_x = [
        next(obj['box'][0] for obj in ground_truth[index] if obj['label'] == 'car')
        for index in car_present
    ]
    moving = [index for a, b, index in zip(car_x, car_x[1:], car_present) if b != a]
    assert moving, 'the car never moves'
    still = [index for index in car_present if index not in moving]
    assert still, 'the car never comes to rest'
    # ...and it rests for long enough for a dwell threshold to be meaningful.
    assert len(still) >= 15


# ─── Labels and pixels agree ─────────────────────────────────────────────────


def _expected_pixel(objects, x, y, background):
    """The colour a pixel must have, honouring draw order.

    Objects are drawn in the order they appear in the annotation file, so a
    later box paints over an earlier one. The driveway scene deliberately
    overlaps the dog with the parked car, which is the occlusion case -- so the
    expectation has to be "the LAST box covering this pixel", not "the first".
    """
    color = background[y][x]
    for obj in objects:
        bx = int(round(obj['box'][0] * render_fixture.FRAME_WIDTH))
        by = int(round(obj['box'][1] * render_fixture.FRAME_HEIGHT))
        bw = int(round(obj['box'][2] * render_fixture.FRAME_WIDTH))
        bh = int(round(obj['box'][3] * render_fixture.FRAME_HEIGHT))
        if bx <= x < bx + bw and by <= y < by + bh:
            color = render_fixture.LABEL_COLORS[obj['label']]
    return color


@pytest.mark.parametrize('name', SCENES)
def test_every_annotated_box_is_drawn_at_its_annotated_geometry(name):
    scene = render_fixture.load_scene(name, FIXTURE_DIR)
    background = render_fixture.background_frame()
    for frame in scene['frames']:
        objects = frame.get('objects') or []
        rows = render_fixture.render_frame(objects)
        assert len(rows) == render_fixture.FRAME_HEIGHT
        assert all(len(row) == render_fixture.FRAME_WIDTH for row in rows)
        for obj in objects:
            x = int(round(obj['box'][0] * render_fixture.FRAME_WIDTH))
            y = int(round(obj['box'][1] * render_fixture.FRAME_HEIGHT))
            width = int(round(obj['box'][2] * render_fixture.FRAME_WIDTH))
            height = int(round(obj['box'][3] * render_fixture.FRAME_HEIGHT))
            # Centre of the box: the one pixel that is unambiguously inside it.
            cx = min(x + width - 1, x + width // 2)
            cy = min(y + height - 1, y + height // 2)
            assert rows[cy][cx] == _expected_pixel(objects, cx, cy, background), (
                f"{name} frame {frame['index']}: {obj['label']} is not drawn "
                f'where it is labelled'
            )
            # A pixel just outside the labelled box must not carry this box's
            # colour, so the box is not accidentally drawn larger than the
            # annotation -- unless another annotated box legitimately covers it.
            outside_x = min(render_fixture.FRAME_WIDTH - 1, x + width)
            outside_y = min(render_fixture.FRAME_HEIGHT - 1, y + height)
            assert rows[outside_y][outside_x] == _expected_pixel(
                objects, outside_x, outside_y, background,
            )


@pytest.mark.parametrize('name', SCENES)
def test_an_empty_annotated_frame_renders_the_bare_background(name):
    scene = render_fixture.load_scene(name, FIXTURE_DIR)
    background = render_fixture.background_frame()
    empty_frames = [f for f in scene['frames'] if not f.get('objects')]
    assert empty_frames, f'{name} has no negative frames'
    for frame in empty_frames:
        assert render_fixture.render_frame([]) == background


def test_the_background_is_not_flat():
    # A flat background lets a background subtractor learn in a single frame and
    # then report nothing forever, which would make the fixture measure the
    # subtractor's learning rate instead of the gate.
    rows = render_fixture.background_frame()
    colors = {pixel for row in rows for pixel in row}
    assert len(colors) > 10


# ─── PNG output ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize('name', SCENES)
def test_render_scene_writes_one_png_per_annotated_frame(name, tmp_path):
    written = render_fixture.render_scene(name, tmp_path, FIXTURE_DIR)
    scene = render_fixture.load_scene(name, FIXTURE_DIR)
    assert len(written) == len(scene['frames'])
    assert [p.name for p in written] == [
        f"frame_{int(f['index']):04d}.png" for f in scene['frames']
    ]
    for path in written:
        assert path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')


def test_encode_png_produces_a_decodable_image_of_the_right_size():
    rows = render_fixture.render_frame([
        {'label': 'person', 'box': [0.25, 0.25, 0.5, 0.5]},
    ])
    payload = render_fixture.encode_png(rows)
    assert _decode_png(payload) == rows


def _decode_png(payload: bytes) -> list[list[tuple[int, int, int]]]:
    """Minimal PNG reader for the encoder's own output (filter 0, colour type 2).

    Deliberately hand-rolled rather than pulled from cv2/PIL: the point is to
    prove the hand-rolled ENCODER is correct, and a decoder that shares the
    encoder's assumptions would happily agree with a broken pair.
    """
    assert payload[:8] == b'\x89PNG\r\n\x1a\n'
    offset = 8
    width = height = 0
    idat = bytearray()
    while offset < len(payload):
        (length,) = struct.unpack('>I', payload[offset:offset + 4])
        tag = payload[offset + 4:offset + 8]
        body = payload[offset + 8:offset + 8 + length]
        (crc,) = struct.unpack('>I', payload[offset + 8 + length:offset + 12 + length])
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, f'bad CRC in {tag!r}'
        if tag == b'IHDR':
            width, height, depth, colour = struct.unpack('>IIBB', body[:10])
            assert (depth, colour) == (8, 2)
        elif tag == b'IDAT':
            idat.extend(body)
        offset += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = width * 3
    rows: list[list[tuple[int, int, int]]] = []
    for y in range(height):
        start = y * (stride + 1)
        assert raw[start] == 0, 'only the None filter is emitted'
        line = raw[start + 1:start + 1 + stride]
        # PNG stores RGB; the scenes are authored in BGR (cv2's native order),
        # so channel-reverse on the way out.
        rows.append([
            (line[i + 2], line[i + 1], line[i])
            for i in range(0, stride, 3)
        ])
    return rows


# ─── End to end, where the runtime allows it ─────────────────────────────────


@pytest.mark.parametrize('name', SCENES)
def test_motion_gate_finds_the_labelled_activity(name, tmp_path):
    """The fixture's actual job: a real recall number, not a synthetic one."""
    pytest.importorskip('cv2', reason='the evaluator needs opencv to read frames')
    import cv2  # noqa: PLC0415 - only reachable once cv2 exists
    import numpy as np  # noqa: PLC0415

    from app.detection_state import detect_frame_motion  # noqa: PLC0415

    written = render_fixture.render_scene(name, tmp_path, FIXTURE_DIR)
    ground_truth = load_ground_truth(FIXTURE_DIR / f'{name}.json')
    active = {index for index, objects in ground_truth.items() if objects}

    fired: set[int] = set()
    for index, path in enumerate(written):
        decoded = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None, f'frame {path.name} is not readable by cv2'
        has_motion, *_ = detect_frame_motion(f'{name}-cam', decoded)
        if has_motion and index > 0:
            fired.add(index)

    # The gate must be quiet on the empty lead-in: a fixture that fires
    # everywhere would validate nothing.
    lead_in = set(range(min(active))) if active else set()
    assert not (fired & lead_in), f'{name} motion gate fired during the empty lead-in'
    # ...and must catch a real share of the labelled activity. The threshold is
    # deliberately loose: this asserts the fixture and the gate agree, not that
    # a particular gate tuning is correct.
    assert len(fired & active) >= len(active) // 2, (
        f'{name}: gate fired on {len(fired & active)}/{len(active)} labelled frames'
    )
