# Daygle AI Camera Development Guide

Daygle AI Camera is a FastAPI-based Orange Pi 3B AI camera platform. The current implementation is intentionally runnable without camera hardware by using a mock camera and mock detector. This lets the dashboard, event database, alert rules, and deployment flow be tested before OV5647 CSI camera and YOLO model support are added.

## Architecture

```text
Browser dashboard (web/)
        |
        v
FastAPI app (app/main.py)
        |
        +--> Camera backend (app/mock_camera.py today; OV5647 later)
        +--> Detector backend (app/detector.py mock today; ONNX/RKNN later)
        +--> Alert engine (app/alerts.py)
        +--> SQLite event store (app/database.py)
        +--> Snapshot/event files (app/storage.py)
```

### Backend modules

- `app/main.py` creates the FastAPI app, serves `web/index.html`, mounts static assets, and exposes the API endpoints.
- `app/settings.py` loads defaults, optional `config.yaml`, or the path in `DAYGLE_CONFIG`.
- `app/mock_camera.py` provides frame metadata so the rest of the pipeline behaves like a live camera is present.
- `app/detector.py` produces synthetic detections for dashboard and alert testing.
- `app/alerts.py` evaluates configured object alert rules with cooldowns.
- `app/database.py` stores events, detections, and alert history in SQLite.
- `app/storage.py` creates data directories and writes mock snapshot metadata files.

### Dashboard

The dashboard is a static dark-mode app in `web/`. It calls the FastAPI API directly and supports:

- Runtime status display
- Forced mock detection generation
- Event and alert history
- Object-label search
- Object count chips

## API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Dashboard HTML, or JSON status if the dashboard is missing |
| `GET` | `/api/status` | Runtime health, camera mode, frame number, uptime, and resolution |
| `POST` | `/api/mock/detect` | Generate a mock detection event; `force=true` by default |
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
- `ai.backend`: `mock` today; expected future values include `onnx` and `rknn`.
- `alerts.rules`: object-specific alert rules with minimum confidence and cooldown.
- `storage.*`: SQLite and snapshot paths.

## Future OV5647 CSI camera integration

The app should keep the same pipeline shape and swap only the camera implementation.

Recommended steps:

1. Add a camera interface/protocol with `get_frame()` and `snapshot()` methods.
2. Keep `MockCamera` as the default backend for development and CI.
3. Add an OV5647 backend using the camera stack available on the target Armbian image. Depending on kernel and board support this may be `libcamera`, V4L2, or a board-specific CSI pipeline.
4. Normalize real frames into a common structure that includes frame number, timestamp, width, height, and image data or image path.
5. Add snapshot image writing in `Storage` while preserving the current JSON metadata format for tests.
6. Update `config.example.yaml` with camera backend-specific options only after validating them on the Orange Pi 3B.

## Future YOLO ONNX and RKNN integration

The detector layer should evolve into interchangeable backends:

- `mock`: deterministic synthetic detections for UI and API testing.
- `onnx`: YOLO model through ONNX Runtime for portable CPU/NPU experimentation.
- `rknn`: Rockchip RKNN model for accelerated inference on supported hardware.

Recommended detector output should stay compatible with the current API:

```json
{
  "label": "cat",
  "confidence": 0.92,
  "box": {"x": 0.15, "y": 0.22, "width": 0.25, "height": 0.18}
}
```

Coordinates should remain normalized from `0.0` to `1.0` so the dashboard and database schema do not need to change when real images arrive.
