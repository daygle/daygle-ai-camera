# Benchmark fixtures

Ground-truth annotation files for `scripts/evaluate_detection.py`, plus the
renderer that turns them into frames.

## What is here

| File | Camera | Frames | Labels | Built around |
| --- | --- | --- | --- | --- |
| `front-door.json` | Porch / entry | 48 | `person` | A single subject crossing a fixed frame, with an empty lead-in and follow-through |
| `driveway.json` | Driveway / yard | 48 | `car`, `dog`, `cat` | A subject that arrives and then **stops**, an occlusion, and a partially visible object |

Both are annotated in the format documented in
[`docs/detection-benchmarking.md`](../docs/detection-benchmarking.md) §2, and both
load cleanly through `load_ground_truth`.

## The scenes are synthetic, and that is a limitation

`render_fixture.py` draws every frame **from the annotation file**: each
annotated box is painted at exactly its annotated position and size. That is
what makes the fixture trustworthy for what it measures, and it also draws a
hard line around what it does not measure.

**Good for:**

- the motion gate — does motion appear where the label says it should, and stay
  quiet on the empty frames;
- the evaluator's metric math — precision/recall/F1/AP against a known-correct
  answer, with `difficult` objects excluded from the recall denominator;
- the whole benchmark path end to end, on any machine, with no dataset download.

**Not good for:** detector accuracy. A drawn rectangle is not a person, so a
real COCO model will score ~0 recall against these scenes. Do not quote their
per-class precision/recall as evidence about a model, and do not use them to
choose an input size. `tests/test_detection_fixture.py` asserts the *gate*
behaves on them, deliberately not the model.

For per-class accuracy and the input-size recommendation, label your own
footage. Both scenes carry a `description` explaining the capture recipe and the
per-frame intent so a real clip can be annotated to the same structure.

## Rendering frames

The renderer is stdlib-only (it writes PNGs with `zlib` + `struct`), so it runs
on a bare Python install:

```bash
python fixtures/render_fixture.py --out /tmp/frames
# /tmp/frames/front-door/frame_0000.png ...
```

One scene at a time:

```bash
python fixtures/render_fixture.py --out /tmp/frames --scene driveway
```

## Running the evaluator

Motion gate only — no model needed beyond opencv:

```bash
python scripts/evaluate_detection.py \
  --input /tmp/frames/front-door \
  --ground-truth fixtures/front-door.json
```

With the post-pipeline decision path and a real model, on your own footage
(the gates below are realistic for a *real* labelled clip, not for these
scenes):

```bash
python scripts/evaluate_detection.py \
  --input /tmp/frames/driveway \
  --ground-truth fixtures/driveway.json \
  --model models/yolo11n.onnx \
  --post-pipeline \
  --camera-config config.yaml \
  --scenarios \
  --output reports/driveway.json
```

## Adding a scene

1. Export frames from a real camera at the cadence you actually run
   (`ingest_frame_fps`), keeping the empty lead-in and follow-through.
2. Annotate the **decoded-frame index**, zero-based, in the order
   `evaluate_detection.py` reads them (sorted filename for a directory). Boxes
   are normalized `[x, y, width, height]`.
3. Set `"synthetic": true` and write a `description` that explains what the
   scene is built to catch.
4. Add a test case. `tests/test_detection_fixture.py` pins that a scene has
   negatives as well as positives, that each labelled box is actually drawn,
   and — where opencv is installed — that the motion gate is quiet on the
   lead-in and fires on the activity.

`render_fixture.py` only needs to learn a new label colour in `LABEL_COLORS`;
a real-footage scene does not need the renderer at all, only the JSON.
