# Daygle AI Camera

Daygle AI Camera is a self-hosted AI camera platform for Linux servers and local development. It provides a browser-based dashboard for managing RTSP/ONVIF cameras, ONNX YOLO object detection, sound detection, event recordings, alerts, and audit logging.

## Features

- Multi-camera RTSP/ONVIF support with browser management and optional PTZ control
- Object detection via ONNX YOLO models - YOLOv8, YOLO11, and NMS-free YOLO26 families in Nano through Extra Large sizes
- In-app model library that downloads and exports models at a chosen input resolution
- CPU and CUDA (NVIDIA GPU) inference with FP32, FP16, and INT8 precision options
- Sound detection using YAMNet TFLite
- Three-layer detection: pixel-diff motion gate, YOLO object detection, and per-zone motion rules
- Monitoring zones, motion and object rules, per-label confidence and cooldowns
- Continuous per-camera recording plus event clips with pre/post-event buffering
- Email alerts and ntfy-compatible push notifications, including camera offline alerts
- Recording, timeline playback, retention, and manual purge
- User roles: `admin` and `viewer`
- Audit log of admin actions, camera diagnostics, and an in-browser application log viewer
- Database backup / restore (database-only or full with media) and over-the-air updates
- Debian install script with a systemd service bundle

## Documentation

- `docs/ai-detection.md` - ONNX object detection: models, precision, device, and advanced tuning
- `docs/motion-detection.md` - motion detection and object rule tuning
- `docs/sound-detection.md` - sound detection, audio rules, and runtime setup
- `docs/operations.md` - health, logs, backups, and service operation

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
   cd /opt/
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

   The dependency helper defaults to `auto`: it selects `onnxruntime-gpu` only
   when `nvidia-smi -L` successfully enumerates an NVIDIA GPU. Override that
   choice explicitly when needed:

   ```bash
   DAYGLE_ONNXRUNTIME_VARIANT=cpu ./scripts/install_python_deps.sh python requirements.txt
   DAYGLE_ONNXRUNTIME_VARIANT=gpu ./scripts/install_python_deps.sh python requirements.txt
   ```

   The helper removes the opposite ONNX Runtime wheel before installing, so it
   is safe to switch an existing virtual environment between CPU and GPU.

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
./scripts/install_debian.sh
```

The installer will:

- install required system packages
- detect a usable NVIDIA GPU with `nvidia-smi -L`
- install `onnxruntime-gpu` on detected NVIDIA systems, otherwise the CPU wheel
- create a `daygle` maintenance user
- copy the app into `/opt/daygle-ai-camera`
- create `/etc/daygle-ai-camera/config.yaml`
- create `/opt/daygle-ai-camera/data` and `/opt/daygle-ai-camera/models`
- install `daygle-ai-camera.service`
- start the service automatically

To force a deterministic choice, set the variable before running the installer:

```bash
DAYGLE_ONNXRUNTIME_VARIANT=cpu ./scripts/install_debian.sh
DAYGLE_ONNXRUNTIME_VARIANT=gpu ./scripts/install_debian.sh
```

The installer does not install NVIDIA kernel drivers or CUDA system libraries;
those are distribution-, kernel-, and Secure-Boot-dependent. For GPU installs,
install the Debian/NVIDIA driver first, verify `nvidia-smi`, then run the
Daygle installer. `onnxruntime-gpu` and `onnxruntime` must not be installed
together in one virtual environment.

#### Tesla P4 / Pascal notes

A Tesla P4 is a Pascal (compute capability 6.1) GPU. Confirm that the selected
ONNX Runtime GPU release and its CUDA/cuDNN requirements still support Pascal
before upgrading the environment. Avoid CUDA 13-era packages that drop Pascal
support; an older CUDA 11/12-compatible ONNX Runtime release may be required.
The driver must also be new enough for that CUDA runtime. Verify the result
from the installed environment:

```bash
/opt/daygle-ai-camera/.venv/bin/python -c \
  "import onnxruntime as ort; print(ort.__version__); print(ort.get_available_providers())"
```

A healthy GPU installation should list `CUDAExecutionProvider`. The detector
still uses CPU for runtime dynamic INT8 and falls back to FP32 if a selected
model or provider cannot load.

Check service status:

```bash
systemctl status daygle-ai-camera
journalctl -u daygle-ai-camera -f
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

- `server.host` and `server.port` - Uvicorn listen address and port
- `auth.enabled` - whether authentication is enabled
- `storage.database` - SQLite database path

All other app settings are stored in SQLite and managed by the web UI.

## Running

- `/setup` - initial admin creation
- `/login` - user login
- `/` - dashboard and event search
- `/live` - live camera view with detection overlay
- `/cameras` - camera management, recording, and PTZ
- `/zones` - monitoring zone editor and object/motion rules
- `/sounds` - sound detection rules
- `/onnx` - AI model library and detector settings
- `/alerts` - alert history
- `/settings` - detection, recording, notifications, retention, backup, and updates
- `/users` - user management (admin)
- `/profile` - change your own password
- `/audit` - audit log
- `/recordings` - recordings list
- `/recordings/timeline` - timeline playback
- `/camera-log` - camera diagnostics
- `/application-log` - in-browser application log viewer
- `/yamnet-tflite` - sound detection backend status

## AI and sound detection

### ONNX detection

- Open `/onnx` as an admin. The page is split into **Status**, **Models**, and **Settings** tabs.
- On **Models**, pick a YOLO model (YOLOv8, YOLO11, or YOLO26) and a download resolution, then download and install it. Use **Use** to activate an installed model and **Check for Updates** to re-export newer weights.
- On **Settings**, choose the inference device (Auto, CUDA, or CPU), precision (FP32, FP16, or INT8), and any advanced tuning such as concurrency, inference threads, GPU memory limit, execution mode, NMS dedupe, and CUDA IO Binding.
- On **Status**, use **Check Model**, **Reload Detector**, and **Test Detector** to confirm the detector is healthy.

Models are stored under `models/`. The default model is `yolo11n`, downloaded automatically on first start when no model is present. See `docs/ai-detection.md` for the full settings reference.

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
./scripts/install_debian.sh
systemctl restart daygle-ai-camera
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
- ONNX fails to load: verify model and label paths and confirm the expected ONNX Runtime wheel is installed. For GPU, check that `CUDAExecutionProvider` appears in `ort.get_available_providers()` and that the NVIDIA driver/CUDA/cuDNN versions match the ONNX Runtime release.
- Email alerts fail: verify SMTP settings under `/settings`, and confirm email notifications are enabled for the rule.
- Push notifications fail: verify ntfy settings and use the test notification action.
- Camera connection issues: check stream URL or ONVIF credentials in `/cameras` and use camera test connection.
- Offline camera reports: review `/camera-log` and camera offline settings.
- Sound detection unavailable: confirm `ffmpeg` is installed, the RTSP stream includes audio, and TensorFlow Lite runtime is present.
- Service cannot write data or models: verify storage directories and permissions under `/opt/daygle-ai-camera`.

## Logs

- Application logs: `data/logs/app.log`
- Service logs: `journalctl -u daygle-ai-camera -f`
