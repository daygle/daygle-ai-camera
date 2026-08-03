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
  back to CPU; *CUDA (GPU)* forces the GPU; *CPU* forces CPU. INT8 precision
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
