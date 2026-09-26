"""Deterministic, dependency-free renderer for the benchmark fixture scenes.

Why this exists
---------------
``scripts/evaluate_detection.py`` measures a *pipeline*: the motion gate plus
the ONNX detector. To exercise it you need frames, and a labelled ground-truth
file that describes those frames. Historically the docs pointed at
``fixtures/front-door.json`` with no fixture actually in the repository, so
every per-camera number in the roadmap (segment-timeline caching, post-process
pool backpressure, the guided input-size sweep) had nothing to run against.

The two shipped scenes are SYNTHETIC and are labelled as such. They are drawn
from the ground-truth file itself: every annotated box is rendered at exactly
its annotated position and size, so the pixels and the labels cannot drift
apart. That makes the fixture a genuine, reproducible test of the motion gate
and of the evaluator's metric math, and it is committed as JSON plus code
rather than as binary frames.

It is NOT a substitute for real camera footage when judging detector accuracy.
A drawn rectangle will never be recognised as a person by a real COCO model, so
``fixtures/`` scenes measure the *gate* (does motion appear where the label says
it should, and stay quiet elsewhere) and the *plumbing*. For per-class
precision/recall and the input-size recommendation, label your own footage --
``fixtures/README.md`` has the capture recipe and the annotation format.

Deliberately stdlib-only (zlib + struct write the PNGs) so the fixture can be
rendered and tested on a machine with no numpy, cv2 or ONNX installed. The
evaluator itself still needs cv2 to read the frames, which is a runtime
dependency it already declares.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from typing import Any

# Scene geometry. Small enough that 48 frames stay a few hundred KB of PNG and
# large enough that a 320x240 motion thumbnail and a 640px letterbox are both
# exercised honestly.
FRAME_WIDTH = 320
FRAME_HEIGHT = 240

# The static background each scene starts from: a vertical gradient plus a
# noise-free ground band. A gradient (rather than a flat colour) matters --
# a flat background lets a background subtractor learn in one frame and report
# nothing, which would make the fixture test the wrong thing.
BACKGROUND_TOP = (58, 66, 74)
BACKGROUND_BOTTOM = (24, 27, 31)
GROUND_COLOR = (74, 80, 86)

# Per-label fill colours, in BGR so the rendered PNG is already in the order
# cv2.imread hands back. A model will not learn these; they exist so a human
# opening the fixture in an image viewer can see the labelled region.
LABEL_COLORS: dict[str, tuple[int, int, int]] = {
    'person': (96, 168, 232),
    'dog': (86, 214, 168),
    'car': (86, 168, 240),
    'cat': (120, 120, 220),
    'bicycle': (200, 120, 90),
}

FALLBACK_COLOR = (200, 200, 200)

FIXTURE_DIR = Path(__file__).resolve().parent


def _clamp(value: float) -> int:
    return max(0, min(255, int(value)))


def background_frame() -> list[list[tuple[int, int, int]]]:
    """One frame of the static scene, as rows of BGR pixels."""
    rows: list[list[tuple[int, int, int]]] = []
    for y in range(FRAME_HEIGHT):
        # A horizontal band at the bottom stands in for a floor, so a subject
        # crossing it produces a hard edge rather than a soft gradient.
        if y >= int(FRAME_HEIGHT * 0.72):
            row = [GROUND_COLOR] * FRAME_WIDTH
        else:
            ratio = y / max(1, FRAME_HEIGHT - 1)
            color = tuple(
                _clamp(top + (bottom - top) * ratio)
                for top, bottom in zip(BACKGROUND_TOP, BACKGROUND_BOTTOM)
            )  # type: ignore[assignment]
            row = [color] * FRAME_WIDTH  # type: ignore[list-item]
        rows.append(row)
    return rows


def draw_box(
    frame: list[list[tuple[int, int, int]]],
    box: list[float],
    color: tuple[int, int, int],
) -> None:
    """Fill the normalized ``[x, y, width, height]`` box with a solid color."""
    x = max(0, min(FRAME_WIDTH - 1, int(round(box[0] * FRAME_WIDTH))))
    y = max(0, min(FRAME_HEIGHT - 1, int(round(box[1] * FRAME_HEIGHT))))
    width = max(1, min(FRAME_WIDTH - x, int(round(box[2] * FRAME_WIDTH))))
    height = max(1, min(FRAME_HEIGHT - y, int(round(box[3] * FRAME_HEIGHT))))
    for row in range(y, y + height):
        target = frame[row]
        for column in range(x, x + width):
            target[column] = color


def render_frame(objects: list[dict[str, Any]]) -> list[list[tuple[int, int, int]]]:
    """Render one frame containing exactly the annotated ``objects``."""
    frame = background_frame()
    for obj in objects:
        draw_box(frame, obj['box'], LABEL_COLORS.get(str(obj.get('label')), FALLBACK_COLOR))
    return frame


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack('>I', len(payload))
        + tag
        + payload
        + struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(rows: list[list[tuple[int, int, int]]]) -> bytes:
    """Encode rows of BGR pixels as a colour PNG.

    Written by hand rather than via PIL/cv2 so the fixture renders on a bare
    Python install. The scanline filter byte 0 (None) keeps the encoder to a
    straight zlib compress, which is plenty for a 320x240 synthetic frame.
    """
    height = len(rows)
    width = len(rows[0]) if rows else 0
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for blue, green, red in row:
            raw.extend((red, green, blue))
    return (
        b'\x89PNG\r\n\x1a\n'
        + _png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b'IDAT', zlib.compress(bytes(raw), 9))
        + _png_chunk(b'IEND', b'')
    )


def load_scene(name: str, directory: Path | None = None) -> dict[str, Any]:
    """Read one scene's ground-truth document by name (without the suffix)."""
    base = directory or FIXTURE_DIR
    return json.loads((base / f'{name}.json').read_text(encoding='utf-8'))


def scene_names(directory: Path | None = None) -> list[str]:
    base = directory or FIXTURE_DIR
    return sorted(path.stem for path in base.glob('*.json'))


def render_scene(name: str, out_dir: Path, directory: Path | None = None) -> list[Path]:
    """Render every frame of a scene into ``out_dir`` as ``frame_%04d.png``.

    Frames are named by their ANNOTATED index, so the evaluator's zero-based
    decoded-frame order matches the annotation order regardless of how many
    frames the scene declares.
    """
    scene = load_scene(name, directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for frame in scene['frames']:
        path = out_dir / f"frame_{int(frame['index']):04d}.png"
        path.write_bytes(encode_png(render_frame(frame.get('objects') or [])))
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', required=True, help='Directory to write the frames into.')
    parser.add_argument('--scene', action='append', help='Scene name (repeatable). Default: all.')
    args = parser.parse_args(argv)
    out = Path(args.out)
    names = args.scene or scene_names()
    if not names:
        print('No scenes found in', FIXTURE_DIR)
        return 1
    for name in names:
        written = render_scene(name, out / name)
        print(f'{name}: wrote {len(written)} frames to {out / name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
