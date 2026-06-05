# Daygle AI Camera Development Guide

Daygle AI Camera is a FastAPI-based Orange Pi 3B AI camera platform. The implementation is runnable without camera hardware by using a mock camera and mock detector, and it can also run YOLOv8 ONNX inference for uploaded-image testing.

## Architecture

```text
Browser dashboard (web/)
        |
        v
FastAPI app (app/main.py)
        |
        +--> Camera backend (app/mock_camera.py today; OV5647 later)
        +--> Detector backend (app/detector.py mock or ONNX YOLO)
        +--> Alert engine (app/alerts.py)
        +--> SQLite event store (app/database.py)
        +--> Snapshot/event files (app/storage.py)
```

### Backend modules

- `app/main.py` creates the FastAPI app, serves `web/index.html`, mounts static assets, exposes the API endpoints, selects the configured detector, and converts detections into stored events.
- `app/settings.py` loads defaults, optional `config.yaml`, or the path in `DAYGLE_CONFIG`.
- `app/mock_camera.py` provides frame metadata so the rest of the pipeline behaves like a live camera is present.
- `app/detector.py` contains `MockDetector`, `OnnxYoloDetector`, backend selection, YOLO output parsing, and non-max suppression.
- `app/alerts.py` evaluates configured object alert rules with cooldowns.
- `app/database.py` stores events, detections, and alert history in SQLite.
- `app/storage.py` creates data directories, writes mock snapshot metadata files, and saves uploaded image snapshots.

### Dashboard

The dashboard is a static dark-mode app in `web/`. It calls the FastAPI API directly and supports:

- Runtime status display, including AI backend availability
- Forced mock detection generation
- Uploaded-image detection testing through `POST /api/detect/test-image`
- Event and alert history
- Object-label search
- Object count chips

## API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Dashboard HTML, or JSON status if the dashboard is missing |
| `GET` | `/api/status` | Runtime health, camera mode, AI backend status, frame number, uptime, and resolution |
| `POST` | `/api/mock/detect` | Generate a mock detection event; `force=true` by default |
| `POST` | `/api/detect/test-image` | Upload an image, run the active detector, save the uploaded image, and store detections as an event |
| `GET` | `/api/events?label=&limit=` | List recent events, optionally filtered by object label |
| `GET` | `/api/events/{event_id}` | Fetch one event and its detections |
| `GET` | `/api/alerts?limit=` | List alert history |
| `GET` | `/api/stats` | Event, alert, and object-count statistics |
| `GET` | `/api/config` | Non-secret runtime configuration summary |

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Then open <http://127.0.0.1:8080/>.

Run tests and syntax checks:

```bash
python -m compileall app tests
pytest -q
```

## Configuration

Copy the example file when you want persistent local overrides:

```bash
cp config.example.yaml config.yaml
```

For service installs, the installer writes `/etc/daygle-ai-camera/config.yaml` and starts the service with `DAYGLE_CONFIG=/etc/daygle-ai-camera/config.yaml`.

Important settings:

- `server.host` / `server.port`: bind address for direct `python app/main.py` startup.
- `camera.backend`: `mock` today; expected future values include `ov5647` or `v4l2`.
- `ai.backend`: `mock` or `onnx`.
- `ai.model_path`: ONNX model path, usually `models/yolov8n.onnx`.
- `ai.input_size`: YOLO input size as an integer such as `640`, a `WIDTHxHEIGHT` string, or a two-item list.
- `ai.confidence`: minimum detection confidence.
- `ai.iou_threshold`: overlap threshold for non-max suppression.
- `ai.labels_path`: label file path, defaulting to `models/coco.names`.
- `alerts.rules`: object-specific alert rules with minimum confidence and cooldown.
- `storage.*`: SQLite and snapshot paths.

## ONNX YOLO workflow

Download the default model:

```bash
python scripts/download_yolov8n_onnx.py --output models/yolov8n.onnx
```

Example ONNX configuration:

```yaml
ai:
  enabled: true
  backend: onnx
  confidence: 0.45
  iou_threshold: 0.45
  input_size: 640
  model_path: models/yolov8n.onnx
  labels_path: models/coco.names
```

`OnnxYoloDetector` expects YOLO-style output shaped like YOLOv8 exports, including `(1, 84, 8400)` or `(1, 8400, 84)`. It letterboxes input images, runs CPU ONNX Runtime, filters by confidence, applies class-aware non-max suppression, and emits detection dictionaries in this shape:

```json
{
  "label": "cat",
  "confidence": 0.92,
  "box": {"x": 120.5, "y": 44.0, "width": 320.0, "height": 180.0}
}
```

The mock detector remains the default and is still used by `POST /api/mock/detect` even when ONNX is configured, so dashboard demos and alert tests continue to work without a model.

## Future OV5647 CSI camera integration

The app should keep the same pipeline shape and swap only the camera implementation.

Recommended steps:

1. Add a camera interface/protocol with `get_frame()` and `snapshot()` methods.
2. Keep `MockCamera` as the default backend for development and CI.
3. Add an OV5647 backend using the camera stack available on the target Armbian image. Depending on kernel and board support this may be `libcamera`, V4L2, or a board-specific CSI pipeline.
4. Normalize real frames into a common structure that includes frame number, timestamp, width, height, and image bytes or image path.
5. Reuse `Storage.save_image_snapshot()` for real frame snapshots.
6. Update `config.example.yaml` with camera backend-specific options only after validating them on the Orange Pi 3B.

## Future RKNN integration

The detector layer is intentionally interchangeable:

- `mock`: synthetic detections for UI and API testing.
- `onnx`: YOLO model through ONNX Runtime for portable CPU/NPU experimentation.
- `rknn`: future Rockchip RKNN model for accelerated inference on supported hardware.

Keep the detector output JSON shape stable so the API, database, dashboard, alerts, and object search do not need to change when a hardware-accelerated backend is added.
