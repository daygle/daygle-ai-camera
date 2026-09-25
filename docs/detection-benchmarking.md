# Object Detection Benchmarking

The detection evaluator replays camera footage through the same motion gate and
ONNX object detector used by live monitoring. With a ground-truth annotation
file, it changes from a profiling tool into a repeatable object-detection
quality benchmark.

The evaluator does not write to the application database or configuration. An
explicit `--output` path writes only the generated JSON report.

## 1. Build a representative dataset

Use short recordings or folders of extracted frames from the actual cameras.
A useful set includes:

- day, night, and infrared footage;
- empty scenes and important negative scenes;
- people, vehicles, animals, and the classes that matter operationally;
- shadows, glare, rain, vegetation movement, and sensor noise;
- small, distant, partially hidden, and stopped subjects;
- multiple camera angles and lighting profiles.

Sample clips long enough to include empty lead-in and follow-through. Avoid
including only obvious positives: negative scenes are necessary to reveal false
positives. Keep raw camera footage and annotations outside Git when they contain
private information; the format below is portable and can live with a curated,
non-sensitive fixture.

## 2. Create ground-truth annotations

Annotations are JSON. Frame indexes are **zero-based decoded-frame indexes** in
the same order as the evaluator reads the video or image directory. Boxes are
normalized `[x, y, width, height]` values from `0` to `1`.

```json
{
  "version": 1,
  "frames": [
    {
      "index": 0,
      "objects": []
    },
    {
      "index": 125,
      "objects": [
        {
          "label": "person",
          "box": [0.42, 0.18, 0.18, 0.65]
        },
        {
          "label": "dog",
          "box": [0.65, 0.55, 0.20, 0.25],
          "difficult": true
        }
      ]
    }
  ]
}
```

Rules:

- Every listed `index` must be unique and must exist in the evaluated input.
- Empty `objects` arrays are important: they measure false positives.
- Labels are matched case-insensitively.
- `difficult: true` marks an object that should be ignored when a prediction
  overlaps it, such as a tiny or severely occluded object whose exact box is
  uncertain. Difficult objects are not counted as missed.
- Predictions on frames not listed in the annotations are not evaluated, so keep
  a separate benchmark input containing only labeled frames or label the whole
  input sequence.

## 3. Run a quality benchmark

```bash
python scripts/evaluate_detection.py \
  --input fixtures/front-door-frames \
  --ground-truth fixtures/front-door.json \
  --model models/yolo11n.onnx \
  --labels models/coco.names \
  --shadow auto \
  --output reports/front-door-baseline.json
```

The report includes:

- true positives, false positives, and false negatives;
- precision, recall, and F1 at the selected IoU threshold (default `0.5`);
- per-class precision, recall, F1, support, and average precision;
- mAP@0.5 and mAP@0.5:0.95;
- motion rate and changed-pixel distribution;
- detection rate, per-label counts, and mean confidence;
- p50/p95 motion and inference latency.

Use `--iou-threshold 0.75` to make box-localization quality stricter. The JSON
report is suitable for checking into a curated test project, storing as a CI
artifact, or comparing between runs.

## 4. Sweep confidence thresholds

Run the same footage and annotations at several object confidence floors:

```bash
python scripts/evaluate_detection.py \
  --input fixtures/front-door-frames \
  --ground-truth fixtures/front-door.json \
  --model models/yolo11n.onnx \
  --confidence-sweep 0.25,0.35,0.45,0.55 \
  --output reports/front-door-sweep.json
```

Compare `map_50`, `map_50_95`, false positives, and recall. A higher confidence
floor often reduces false positives but can miss small, distant, or low-light
objects. Choose the operating point from the measured trade-off rather than a
generic default.

The same pattern can be used to compare other supported settings: run with
`--gated` versus the always-on path, change `--shadow`, or add `--tiling 3x3`.
Keep the input and annotations identical when comparing configurations.

## 5. Make it a regression check

Keep a small, deterministic, non-sensitive fixture in the repository and a
larger private dataset for local benchmarking. In CI, validate the annotation
format and run metric tests. A full camera benchmark should run on demand and
publish its JSON report as an artifact. The evaluator can enforce minimum
quality while still writing the report:

```bash
python scripts/evaluate_detection.py \
  --input fixtures/front-door-frames \
  --ground-truth fixtures/front-door.json \
  --model models/yolo11n.onnx \
  --fail-below-precision 0.90 \
  --fail-below-recall 0.85 \
  --fail-below-f1 0.87 \
  --fail-below-map-50 0.85 \
  --output reports/ci-quality.json
```

A failed floor exits with status `1`. Pin thresholds against the model, labels,
and fixture version so a model upgrade is an explicit decision rather than an
accidental regression. When running a confidence sweep, every selected
confidence floor must pass the configured gates.
