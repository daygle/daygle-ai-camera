# AI Object Detection Guide

Daygle AI Camera runs ONNX YOLO models to detect objects in camera frames. All
of the object-detection engine is configured from **ONNX** (`/onnx`), an
admin-only page split into three tabs: **Status**, **Models**, and **Settings**.

This guide explains the model library, the detector settings, and the advanced
tuning options. For how object detection fits together with the motion gate and
zone rules, see [motion-detection.md](motion-detection.md).

---

## Status tab

The Status tab shows the live detector state and quick diagnostic actions.

- **Current Backend** - the execution backend that is actually loaded (for
  example CPU or CUDA).
- **Model** - the active model name and its file path.
- **Model Exists** / **ONNX Runtime Installed** / **Detector Loaded** - health
  checks that must all read *Yes* for detection to run.
- **Mode** - the detector's current operating mode.
- **Model Resolution** - the input size the loaded model expects.
- **Precision** - the precision actually running. If a requested precision
  fell back (for example INT8 or FP16 dropping to FP32), the requested value is
  shown alongside so silent fallbacks are visible.
- **Last Detector Error** - only shown when there is an error to report.

Action buttons:

- **Check Model** - verifies the configured model and label files resolve.
- **Reload Detector** - rebuilds the detector session with the current settings.
- **Test Detector** - runs a one-off inference to confirm the pipeline works.

---

## Models tab

### Model library

The model library lists the supported YOLO models across three families. Larger
models are more accurate but use more CPU/GPU per frame.

| Family | Notes |
|---|---|
| **YOLOv8** (n/s/m/l/x) | Traditional NMS-based detection. Broad compatibility. |
| **YOLO11** (n/s/m/l/x) | Refined backbone/neck - faster than YOLOv8 with better accuracy, ~22% fewer parameters. |
| **YOLO26** (n/s/m/l/x) | End-to-end **NMS-free** detection. Up to ~43% faster CPU inference; the model head dedupes its own boxes. |

Each card shows the approximate download size, install state (Available,
Installed, or Active), the exported resolution, and the installed version.

Actions per model:

- **Download** - export and install the model. Pick a resolution first (320
  "Fast" through 1280 "Max"; 640 is the default). Higher resolutions are more
  accurate on small/distant objects but slower per frame. YOLO26 models default
  to a 768 input size.
- **Use** - make an installed model the active detector.
- **Update** - re-export a model when newer weights are available. On the active
  model this re-exports in place.
- **Delete** - remove an installed model.
- **Check for Updates** - query the upstream source and flag installed models
  that have a newer release.

The default model is `yolo11n`. On first start, if no model is installed, the
app auto-downloads and exports the default so detection works out of the box.
Models are stored under `models/` alongside the label file `models/coco.names`
(the 80 standard COCO classes).

### Loaded labels

The **Loaded Labels** section lists the object classes the current detector can
identify, loaded from `models/coco.names`.

### Umbrella group labels

Besides the individual model classes, object rules and zone allow-lists accept
two **group** labels that match any of several related classes with a single
rule:

- **animal** - matches `bird`, `cat`, `dog`, `horse`, `sheep`, `cow`,
  `elephant`, `bear`, `zebra`, and `giraffe`.
- **pet** - matches `cat`, `dog`, and `bird`.

Groups are useful when a subject is easily confused between neighbouring
classes - for example, an IR-lit cat at night is frequently misclassified as a
`dog`, so an `animal` (or `pet`) rule still fires where a strict `cat` rule
would miss it. Groups only expand on the configured side: a rule for a concrete
class such as `cat` continues to match `cat` only, so adding a group never
changes the behavior of your existing per-class rules. Pick a group from the
**Add Object…** dropdown on the Zones page, under the **Groups** heading.

---

## Settings tab

The main detection settings are always visible; low-level tuning lives under the
**Advanced** disclosure. Save with the **Save** button - the detector reloads
automatically, and any reload warning is surfaced in the Status panel.

### Primary settings

- **AI Enabled** - master toggle for AI object detection. When disabled, live AI
  detection is skipped and the status panel reports *AI DISABLED*. Default:
  Enabled.
- **Device** - inference device. *Auto* detects a CUDA GPU at startup and falls
  back to CPU; *GPU (CUDA)* forces the GPU; *CPU* forces CPU. INT8 precision
  always runs on CPU regardless of this setting. Default: Auto.
- **Concurrent Cameras** - how many cameras can run ONNX inference at the same
  time. Default: 1 (serialised). Set this to your camera count so each camera
  gets its own inference slot and one slow camera cannot block another.
- **Min Confidence** - minimum confidence score (0-1) a detection must reach to
  be reported. Detections below this are discarded before alert matching.
  Per-label thresholds on the Zones page override this global value. Default:
  0.45.

### Advanced settings

Optional inference tuning that works on its defaults - most setups never need to
touch these. Change one at a time so you can attribute any per-frame impact.

- **Precision** - inference precision.
  - *FP32* - full precision (default).
  - *FP16* - half precision, CUDA only. Exported as FP16 when Device is CUDA or
    Auto on a CUDA-capable host; on CPU-only hosts the export silently falls
    back to FP32.
  - *INT8* - CPU-only quantization with automatic calibration. ~4x smaller
    model, ~1-2 mAP cost, and the biggest CPU performance win.
- **IoU Threshold** - intersection-over-union threshold for merging overlapping
  detections. Only applies when the class-aware NMS dedupe actually runs.
  Default: 0.45.
- **Inference Threads** - CPU threads used per inference run. Leave blank to
  auto-detect (up to 4). More threads speed up each inference but use more CPU
  per camera.
- **GPU Memory Limit (GiB)** - maximum GPU memory ONNX Runtime may use on the
  CUDA provider (ignored on CPU). Leaving headroom prevents cuBLAS allocation
  errors. 0 = unlimited (default).
- **Execution Mode** - ONNX Runtime executor mode (CPU path only). *Parallel*
  (default) runs independent graph nodes on a small inter-op thread pool.
  *Sequential* uses a single thread and can be faster on small graphs. A/B test
  on your hardware.
- **NMS Dedupe** - class-aware NMS dedupe pass.
  - *Auto* (recommended) - skips the extra dedupe for NMS-free YOLO26 models and
    keeps it for YOLOv8/YOLO11.
  - *Skip (NMS-Free Models Only)* - confidence threshold only; affects NMS-free
    YOLO26 models. YOLOv8/YOLO11 always run class-aware NMS because their grid
    head emits thousands of raw boxes.
  - *Force Class-Aware NMS* - always run the dedupe, even on NMS-free models.
- **CUDA IO Binding** - Enabled/Disabled. When enabled, frames are copied
  to/from the GPU with ONNX Runtime's `io_binding` API instead of host
  round-trips, which can reduce per-frame latency on GPU systems. CUDA only -
  automatically disabled when CUDA is unavailable. Default: Disabled.
- **Model Path** / **Labels Path** - read-only paths to the active model and
  label file. These are managed by the Model Library above.

---

## Choosing a configuration

- **CPU-only host** - start with `yolo11n` or `yolo26n` at 640. If CPU load is
  high, try INT8 precision (biggest CPU win) or a smaller resolution. Set
  Concurrent Cameras to your camera count only if you have spare cores.
- **NVIDIA GPU host** - install the NVIDIA driver and `onnxruntime-gpu` (see the
  README), set Device to *Auto* or *CUDA*, and confirm *Current Backend* reads
  CUDA on the Status tab. FP16 precision and CUDA IO Binding can further reduce
  latency. Use **GPU Memory Limit** to leave headroom on shared GPUs.
- **Accuracy over speed** - move up a model size (m/l/x) and/or a higher export
  resolution. YOLO26 models offer strong accuracy with NMS-free inference.

---

## Face detection

The detector is label-driven, so it can run a **face-detection** model in place
of (or alongside, on a second camera profile) the COCO object models. A face
model reports a single `face` label that flows through zones, object rules,
cooldowns, the Events feed, and annotated snapshots exactly like `person` or
`car` — you can, for example, write a rule that alerts only when a face is
visible, rather than any time a full body is detected.

> **Detecting a face is not recognising *who* it is.** This feature draws boxes
> around faces; it does not identify individuals. Matching a face to a named
> person (enrolment + embeddings) is a separate, larger capability and is not
> part of this page.

Under the hood, three pieces make a model a face detector (all wired up for you
when you download one from the library — see below):

1. **A face-detection ONNX model** in `models/`.
2. **A labels file** — `models/face.names` ships with the application and
   contains the single label `face`.
3. **`keypoint_count`** — most YOLO-face weights are pose models with a 5-point
   facial-landmark head. Their ONNX output carries `4 bbox + 1 class score +
   5×3 landmark` columns per anchor. Setting `keypoint_count` (5 for those
   weights) tells the detector to read the class score from the correct column
   instead of mistaking a landmark coordinate for a class score. Plain
   detection-head face models (no landmarks) use `keypoint_count = 0`.

### Downloading a face model

The model library ships a **YOLO11 · Face** family — Nano, Small, Medium, and
Large — alongside the COCO models on the Models tab. Downloading one works
exactly like any other model: its source weights are fetched, exported to ONNX
through the same Ultralytics pipeline, and the active AI settings are bound to
`models/face.names` and `keypoint_count = 5` automatically. No manual settings
edit is needed. Switching back to a COCO model resets `labels_path` to
`models/coco.names` and `keypoint_count` to `0`.

Pick a size the same way you would for object detection: Nano for low-power
hosts, Medium/Large for IR or night-vision cameras where small or low-contrast
faces are harder to catch.

> **Licensing.** The bundled face weights come from
> [YapaLab/yolo-face](https://github.com/YapaLab/yolo-face) (release `1.0.0`)
> and are **GPL-3.0** licensed; they are exported through Ultralytics like every
> other catalog model. The weights are downloaded on demand from their upstream
> release — they are not redistributed inside this repository — but a deployment
> that enables the one-click download should be comfortable with those terms.

### Adding your own face model

To use different weights, add an entry to `YOLO_MODELS` (`app/ai_settings.py`)
using the same schema as the `yolo11*-face` entries: the standard model keys
plus `labels: 'models/face.names'`, `keypoint_count` (5 for a 5-point landmark
head, `0` for a plain detection head), and a `weights_url` — an explicit `https`
source for weights Ultralytics cannot resolve by name. The download flow fetches
`weights_url`, exports it, and binds the labels/keypoint settings automatically.

---

## Troubleshooting

- **`MODEL MISSING`** - open the Models tab, download/select a model, then
  Reload Detector on the Status tab.
- **ONNX fails to load** - verify the model and label paths and confirm the
  expected ONNX Runtime wheel is installed. For GPU, check that
  `CUDAExecutionProvider` appears in `onnxruntime.get_available_providers()` and
  that the NVIDIA driver/CUDA/cuDNN versions match the ONNX Runtime release.
- **Requested precision not active** - the Status panel shows the running
  precision next to the requested one. FP16 falls back to FP32 on CPU-only
  hosts; a model or provider that cannot load also falls back to FP32.
- **CUDA IO Binding has no effect** - it only activates when the CUDA provider
  is actually loaded. On CPU it is disabled automatically.
