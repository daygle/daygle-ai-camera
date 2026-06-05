# Daygle AI Camera

Daygle AI Camera is an Orange Pi 3B AI camera platform with a FastAPI backend, SQLite event history, alert rules, a modern dark-mode dashboard, and switchable mock/ONNX YOLO detector backends. It still runs end-to-end without camera hardware by using the mock camera and mock detector.

## Features

- FastAPI backend with health/status, event, alert, stats, runtime-config, and test-image detection APIs
- SQLite database for searchable events, object labels, confidences, bounding boxes, and alert history
- Mock camera and mock detector so development does not require a physical camera or model file
- ONNX Runtime YOLOv8 detector backend with COCO labels, confidence filtering, and non-max suppression
- Dashboard with searchable object detection history and uploaded-image detection testing
- Real uploaded-image snapshots saved under the configured snapshots directory
- Configurable alert rule example for cat detections
- Armbian install script and systemd unit for Orange Pi 3B deployment

## Quick start for local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080/>.

Generate a mock event from the dashboard or with curl:

```bash
curl -X POST http://127.0.0.1:8080/api/mock/detect
curl http://127.0.0.1:8080/api/events
```

Test uploaded-image detection with the active detector backend:

```bash
curl -X POST \
  -H 'Content-Type: image/jpeg' \
  --data-binary @test.jpg \
  http://127.0.0.1:8080/api/detect/test-image
```

## ONNX YOLO setup

Install dependencies and download the default YOLOv8n ONNX model:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/download_yolov8n_onnx.py --output models/yolov8n.onnx
cp config.example.yaml config.yaml
```

Configure ONNX mode in `config.yaml`:

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

Then start the app:

```bash
DAYGLE_CONFIG=config.yaml uvicorn app.main:app --host 127.0.0.1 --port 8080
```

If `ai.backend: onnx` is selected and the model is missing or dependencies are not installed, startup remains healthy and `/api/status` reports the AI error. Detection requests return HTTP 400 with a clear message instead of crashing the app.

## Armbian 26.x startup on Orange Pi 3B

From a checkout on the Orange Pi:

```bash
sudo ./scripts/install_armbian.sh
sudo systemctl status daygle-ai-camera
sudo journalctl -u daygle-ai-camera -f
```

Then browse to:

```text
http://<orange-pi-ip>:8080/
```

The installer:

1. Installs Python, venv, pip, SQLite, certificates, and rsync.
2. Copies the app to `/opt/daygle-ai-camera`.
3. Creates a virtual environment in `/opt/daygle-ai-camera/.venv`.
4. Installs `requirements.txt`.
5. Creates `/etc/daygle-ai-camera/config.yaml` from `config.example.yaml` if needed.
6. Stores mutable data under `/var/lib/daygle-ai-camera`.
7. Installs and enables `daygle-ai-camera.service`.

Override install paths if needed:

```bash
sudo DAYGLE_APP_DIR=/opt/daygle-ai-camera \
  DAYGLE_CONFIG_DIR=/etc/daygle-ai-camera \
  DAYGLE_DATA_DIR=/var/lib/daygle-ai-camera \
  DAYGLE_USER=daygle \
  ./scripts/install_armbian.sh
```

## API overview

- `GET /` - dashboard
- `GET /api/status` - runtime status, including AI backend availability
- `POST /api/mock/detect` - create a mock detection event
- `POST /api/detect/test-image` - upload an image, run the active detector, save a snapshot, and store detections as an event
- `GET /api/events?label=cat&limit=50` - event search
- `GET /api/events/{event_id}` - event detail
- `GET /api/alerts?limit=25` - alert history
- `GET /api/stats` - aggregate stats
- `GET /api/config` - non-secret runtime config summary

## Development documentation

See [DEVELOPMENT.md](DEVELOPMENT.md) for architecture details and OV5647 CSI / ONNX / RKNN integration notes.
