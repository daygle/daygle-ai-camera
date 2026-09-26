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

## 5. Validate tuning choices (Items 11–12)

Use `--post-pipeline` to compare the raw detector with the policy stages that
can be evaluated without camera-zone configuration:

```bash
python scripts/evaluate_detection.py \
  --input fixtures/front-door-frames \
  --ground-truth fixtures/front-door.json \
  --model models/yolo11n.onnx \
  --post-pipeline \
  --label-thresholds person=0.50,car=0.60 \
  --confirm-frames 2 \
  --confirm-window 3 \
  --confirm-iou 0.05 \
  --output reports/front-door-tuned.json
```

The JSON report keeps both views:

- `benchmark` is the detector output after the global confidence floor and
  optional tiling;
- `post_pipeline_benchmark` applies the full production decision path.

`--post-pipeline` replays the same stages in the same order as
`process_live_stream_alerts`:

| Stage | What it does | Reported key |
| --- | --- | --- |
| label rule floors | drops detections under their per-label rule threshold | `after_label_rules` |
| tracking | stamps stable track ids and displacement history | `tracked` |
| camera/zone scope | keeps detections this camera actually monitors | `after_zone_scope` |
| N-of-M confirmation | the live confirmation window | `after_confirmation` |
| post-zone action decision | alert rules + record-on-detect | `actionable` |

Survivor counts per stage land in `post_pipeline_stages`, so a tuning change
can be attributed to a specific stage rather than guessed at.

`--confirm-frames 1` disables confirmation. `--confirm-window` defaults to the
required frame count. `--confirm-iou 0` uses label-only persistence; a positive
value additionally requires the same-label box to overlap across the window.
Face detections retain the live monitor's face exemption.

The evaluator lowers the detector's internal floor to the lowest supplied label
threshold, then applies the individual rule floors afterward. This mirrors the
live distinction between a shared detector floor and per-zone action rules.
Labels without an explicit threshold remain visible to the benchmark; they are
not silently treated as disabled rules.

### Replaying your real zone policy

Zone matching and the action decision need a camera's own configuration. Pass it
with `--camera-config`; the file is a camera settings object shaped like the
persisted `detection` block, so you can export what the app already stores:

```bash
python scripts/evaluate_detection.py \
  --input fixtures/front-door-frames \
  --ground-truth fixtures/front-door.json \
  --model models/yolo11n.onnx \
  --post-pipeline \
  --camera-config fixtures/front-door-camera.json \
  --confirm-frames 2 --confirm-window 3 \
  --output reports/front-door-tuned.json
```

With a camera config, `--label-thresholds` is optional: it defaults to the
camera's own per-label rule floors, so the run measures the camera as actually
configured instead of an invented policy. Override it to study a different
threshold.

**Without `--camera-config` the zone and action stages are not measured.** The
report says so explicitly (`zone_policy_replayed: false`) and the human output
prints a note, so a run can never imply zone coverage it did not exercise.

### Comparing operating modes

`--scenarios` replays the same clip under both inference modes and reports each
separately:

```bash
python scripts/evaluate_detection.py \
  --input fixtures/front-door-frames \
  --ground-truth fixtures/front-door.json \
  --model models/yolo11n.onnx \
  --post-pipeline --scenarios --output reports/front-door-modes.json
```

Always-on is the live default; motion-gated is the CPU-saving mode. Their
recall differs **by construction**: a subject that never trips the pixel gate is
invisible in motion-gated mode. Sizing a floor from one blended number hides
exactly the regression that matters, so measure the mode the camera will
actually run in — and if you intend to run both, check the gated number before
choosing thresholds.

For a threshold study, hold the input, annotations, model, IoU threshold, and
inference strategy constant. Compare at least:

1. the current global confidence floor;
2. lower and higher global floors with `--confidence-sweep`;
3. per-label floors using `--label-thresholds`;
4. `1-of-1` versus `2-of-3` confirmation;
5. label-only versus spatial confirmation with `--confirm-iou`;
6. always-on versus motion-gated with `--scenarios`.

Choose an operating point from the measured precision/recall trade-off. A
higher floor or stronger confirmation is not automatically better if it
removes valid small or low-light subjects. Keep negative scenes in the dataset;
they are what make false-positive reductions visible.

## 6. Choose an input size (Item 17)

A smaller YOLO input size is the single biggest lever on inference cost, and
also the biggest lever on accuracy - so picking one by hand on an arbitrary
clip is how people end up either wasting CPU or missing small subjects.
`--sweep-input-sizes` runs the same labeled clip at several sizes and
recommends one, given floors you state:

```bash
python scripts/evaluate_detection.py \
  --input fixtures/front-door-frames \
  --ground-truth fixtures/front-door.json \
  --model models/yolo11n.onnx \
  --sweep-input-sizes 320,416,512,640 \
  --min-recall 0.85 \
  --min-precision 0.75 \
  --latency-budget-ms 50 \
  --output reports/input-size.json
```

It prints the trade-off before it prints the answer:

```
Input size trade-off (lower input size = cheaper inference):
    size  precision   recall   mAP@50   p95 ms  verdict
  -----------------------------------------------------
     320      0.620    0.550    0.500     12.0  recall 0.550 < 0.800; precision 0.620 < 0.750
     416      0.840    0.860    0.700     21.0  meets floors
     640      0.930    0.950    0.800     58.0  p95 58.0ms > 50.0ms

  Recommended input size: 416
```

The rule is deliberately **not** "highest mAP wins". Detection cost scales
with the square of the input side, so the recommendation is the *smallest*
size that still clears your quality and latency floors - the cheapest
acceptable answer rather than the best-scoring one. Sizes that miss a floor
are listed with the reason they were rejected, so a sweep that recommends
nothing tells you which constraint is unreachable rather than just failing.

Two caveats worth knowing before you trust a number:

- **Use a representative clip.** The recommendation is only as good as the
  ground truth. A clip of empty driveway will happily recommend 320.
- **Check the whole pipeline, not just the detector.** Add `--post-pipeline`
  with `--camera-config` to see how the size interacts with confirmation
  windows and zone rules; a size that scores well raw can still lose
  detections to N-of-M confirmation.

## 7. Make it a regression check

Keep a small, deterministic, non-sensitive fixture in the repository and a
larger private dataset for local benchmarking. In CI, validate the annotation
format and run metric tests. A full camera benchmark should run on demand and
publish its JSON report as an artifact. The evaluator can enforce minimum quality while still writing the report. When
`--post-pipeline` is used, quality gates apply to the post-pipeline benchmark,
not the raw detector view:

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
accidental regression. When running a confidence sweep, every selected confidence floor must pass the configured gates — and with `--scenarios`, **every scenario** must pass, so a camera that silently loses recall under the motion gate cannot go green on its always-on number alone. Store the input
fixture version, model/labels version, and selected operating point with the
report so a later threshold change is explainable.
