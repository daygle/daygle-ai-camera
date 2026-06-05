# Daygle AI Camera

Daygle AI Camera is an Orange Pi 3B AI camera platform with a FastAPI backend, SQLite event history, alert rules, and a modern dark-mode dashboard. It runs end-to-end today without camera hardware by using a mock camera backend and synthetic object detector.

## Features

- FastAPI backend with health/status, event, alert, stats, and runtime-config APIs
- SQLite database for events, detections, and alert history
- Mock camera and mock detector so development does not require a physical camera
- Dark-mode dashboard with searchable object detection history
- Configurable alert rule example for cat detections
- Armbian install script and systemd unit for Orange Pi 3B deployment
- Architecture notes for future OV5647 CSI, YOLO ONNX, and RKNN support

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
- `GET /api/status` - runtime status
- `POST /api/mock/detect` - create a mock detection event
- `GET /api/events?label=cat&limit=50` - event search
- `GET /api/events/{event_id}` - event detail
- `GET /api/alerts?limit=25` - alert history
- `GET /api/stats` - aggregate stats
- `GET /api/config` - non-secret runtime config summary

## Development documentation

See [DEVELOPMENT.md](DEVELOPMENT.md) for architecture details and future OV5647 CSI / YOLO ONNX / RKNN integration notes.
