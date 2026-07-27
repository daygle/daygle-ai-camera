# Daygle AI Camera

Daygle AI Camera is a self-hosted AI camera platform for Linux servers and local development. It provides a browser-based dashboard for managing RTSP/ONVIF cameras, ONNX YOLO object detection, sound detection, event recordings, alerts, and audit logging.

## Features

- Multi-camera RTSP/ONVIF support with browser management
- Object detection via ONNX YOLO models
- Sound detection using YAMNet TFLite
- Monitoring zones, motion and object rules, per-zone cooldowns
- Email alerts and ntfy-compatible push notifications
- Recording, timeline playback, retention, and manual purge
- User roles: `admin` and `viewer`
- Audit log of admin actions and camera diagnostics
- Database backup / restore and over-the-air updates
- Debian install script with a systemd service bundle

## Documentation

- `docs/motion-detection.md` — motion detection and object rule tuning
- `docs/sound-detection.md` — sound detection, audio rules, and runtime setup
- `docs/operations.md` — health, logs, backups, and service operation

## Requirements

- Python 3.10 or newer
- `pip`
- Modern web browser
- Optional: ONNX model for object detection
- Optional sound detection: RTSP audio-enabled cameras and TensorFlow Lite runtime (`ai-edge-litert` or `tflite-runtime`)

### Debian / Linux server deployment

- Debian or Debian-based Linux distribution
- `sudo` or root access for installation
- Network access for `apt` and `pip`
- Optional: reverse proxy or VPN for public exposure

## Installation

### Local development

1. Clone the repository:

   ```bash
   git clone https://github.com/daygle/daygle-ai-camera.git
   cd daygle-ai-camera
   ```

2. Create and activate a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install Python dependencies:

   ```bash
   ./scripts/install_python_deps.sh python requirements.txt
   pip install --no-cache-dir pytest
   ```

   If you want the default PyTorch/CUDA resolution instead of the CPU-first dependency set, run:

   ```bash
   DAYGLE_TORCH_VARIANT=default ./scripts/install_python_deps.sh python requirements.txt
   ```

4. Create the bootstrap config:

   ```bash
   cp config.example.yaml config.yaml
   ```

5. Start the application:

   ```bash
   DAYGLE_CONFIG=config.yaml uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
   ```

6. Open <http://127.0.0.1:8080/> and complete the first-run setup.

### Debian service install

Run the installer from the repository root with `sudo`:

```bash
sudo ./scripts/install_debian.sh
```

The installer will:

- install required system packages
- create a `daygle` maintenance user
- copy the app into `/opt/daygle-ai-camera`
- create `/etc/daygle-ai-camera/config.yaml`
- create `/opt/daygle-ai-camera/data` and `/opt/daygle-ai-camera/models`
- install `daygle-ai-camera.service`
- start the service automatically

Check service status:

```bash
sudo systemctl status daygle-ai-camera
sudo journalctl -u daygle-ai-camera -f
```

Open:

```text
http://<server-ip>:8080/
```

Then create the first admin user and configure the app from the web UI.

## Configuration

The bootstrap config file is small and only contains trusted startup settings. Most runtime settings are managed through the dashboard.

Example `config.yaml`:

```yaml
server:
  host: 0.0.0.0
  port: 8080

auth:
  enabled: true

storage:
  database: data/daygle_ai_camera.sqlite3
```

Important bootstrap values:

- `server.host` and `server.port` — Uvicorn listen address and port
- `auth.enabled` — whether authentication is enabled
- `storage.database` — SQLite database path

All other app settings are stored in SQLite and managed by the web UI.

## Running

- `/setup` — initial admin creation
- `/login` — user login
- `/` — dashboard
- `/cameras` — camera management
- `/zones` — monitoring zone editor
- `/sounds` — sound detection rules
- `/onnx` — AI model and detector settings
- `/settings` — system settings, notifications, retention, backup, and updates
- `/audit` — audit log
- `/recordings` — recordings list
- `/recordings/timeline` — timeline playback
- `/camera-log` — camera diagnostics

## AI and sound detection

### ONNX detection

- Open `/onnx` as an admin
- Select a YOLO model size and download it
- Save AI settings
- Check model, reload detector, and test detector

Models are stored under `models/`.

### Sound detection

- Open `/sounds`
- Enable sound detection for a camera
- Add sound classes and configure thresholds, recording, and notifications
- Confirm runtime availability on `/yamnet-tflite`

If the TFLite runtime is missing, install `ai-edge-litert` or `tflite-runtime`.

## Updating

### Local update

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

### Service update

```bash
sudo ./scripts/install_debian.sh
sudo systemctl restart daygle-ai-camera
```

The web UI also exposes a software update flow under `/settings`.

## Tests

```bash
python -m compileall app
python -m pytest
```

## Troubleshooting

- Cannot log in after first start: open `/setup` and create the initial admin user.
- Setup redirects to login: a user already exists.
- `MODEL MISSING`: open `/onnx`, download/select a model, and reload the detector.
- ONNX fails to load: verify model and label paths and confirm `onnxruntime` is installed.
- Email alerts fail: verify SMTP settings under `/settings`, and confirm email notifications are enabled for the rule.
- Push notifications fail: verify ntfy settings and use the test notification action.
- Camera connection issues: check stream URL or ONVIF credentials in `/cameras` and use camera test connection.
- Offline camera reports: review `/camera-log` and camera offline settings.
- Sound detection unavailable: confirm `ffmpeg` is installed, the RTSP stream includes audio, and TensorFlow Lite runtime is present.
- Service cannot write data or models: verify storage directories and permissions under `/opt/daygle-ai-camera`.

## Logs

- Application logs: `data/logs/app.log`
- Service logs: `sudo journalctl -u daygle-ai-camera -f`
