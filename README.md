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
- Umbrella `animal` / `pet` group labels so one rule can match any related class (e.g. a cat misread as a dog at night)
- Optional temporal confirmation gate that requires an object to persist across several detection cycles before it alerts, suppressing single-frame false positives
- Continuous per-camera recording plus event clips with pre/post-event buffering
- Email alerts and ntfy-compatible push notifications, including camera offline and recovery alerts
- A single Events feed for object, motion, and sound detections, with alerted-event badges, linked recordings, and annotated snapshots
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
- Optional remote access: `cloudflared` (installed by the Debian installer or updater)

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
   DAYGLE_CONFIG=config.yaml python -m app.server
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

A healthy GPU installation should list `CUDAExecutionProvider`, but note that
`get_available_providers()` reflects what ONNX Runtime was *built* with, not
what can actually load — it can list the provider even when its CUDA/cuDNN
dependencies are missing. For a definitive check, and for the full CUDA
userspace install (the pinned CUDA 12.4 + cuDNN 9.x wheels and loader setup
that `install_debian.sh` does not manage), see
[docs/tesla-p4-gpu-setup.md](docs/tesla-p4-gpu-setup.md). The detector still
uses CPU for runtime dynamic INT8 and falls back to FP32 if a selected model or
provider cannot load.

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

## Remote access with Cloudflare Tunnel

Daygle can manage one Cloudflare Tunnel connector for secure public HTTPS access without port forwarding, an exposed LAN port, or a reverse-proxy configuration. Cloudflare provisions the public certificate automatically.

1. Sign in to [Cloudflare Zero Trust](https://one.dash.cloudflare.com/).
2. Open **Network → Tunnels → Create a Tunnel**.
3. Choose **Cloudflared**, name the tunnel, and create it.
4. On the connector setup screen, copy the tunnel token (the value after `--token`).
5. In Daygle, open **Settings → System → Cloudflare Tunnel**, paste the token, choose whether it should start automatically, and save it. The field is intentionally cleared after saving; the **Saved securely** indicator confirms that a token is present without revealing it. For tokens saved through the UI, Daygle stores only non-secret tunnel metadata in SQLite and keeps the token in a protected `0600` file next to the database; the token is never returned by the status API or written to logs.
6. In the tunnel's **Public Hostnames** configuration, map your hostname to `http://localhost:8080` (or the port configured in Daygle). The card reports whether the connector is running, stopped, unconfigured, or needs attention. Start, stop, or restart it from the same card, or let it start automatically on boot.
7. Browse to the HTTPS hostname. No port forwarding or additional SSL/reverse-proxy setup is required.

For headless/service deployments, set `DAYGLE_CLOUDFLARED_TOKEN` in the service environment (a protected systemd drop-in is recommended). That token takes precedence over the saved UI value and automatically starts `cloudflared` at application boot. Do not put a token in a shell command or a world-readable config file; Daygle passes it to cloudflared through the child environment rather than its command line. Changing the binding after a UI save takes effect on the next Daygle restart.

When tunnel mode is active, Daygle binds Uvicorn to `127.0.0.1` and enables `--proxy-headers --forwarded-allow-ips=127.0.0.1`; this keeps the connector as the only ingress while preserving the client address supplied by the trusted local connector path for audit and login rate-limit handling.

### Cloudflare Access

Cloudflare Access is optional. If Access is enabled for the hostname, interactive browser logins are redirected to the Cloudflare Access page. The Android app cannot complete that browser flow automatically: configure it to send `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers on every request (including API, image, and recording requests), as required by your Access application policy.

If cloudflared cannot start or later exits, Daygle logs a clear warning and continues serving the LAN normally. Use **Start Tunnel**, **Stop Tunnel**, **Restart Tunnel**, and the status readout in the same Settings panel to manage the connector. Status is refreshed periodically while the Settings page is open.

## Running

- `/setup` - initial admin creation
- `/login` - user login
- `/` - dashboard and event search
- `/live` - live camera view with detection overlay
- `/cameras` - camera management, recording, and PTZ
- `/zones` - monitoring zone editor (use **Add Zone** to draw areas), visibility controls, and object/motion rules
- `/sounds` - sound detection rules
- `/onnx` - AI model library and detector settings
- `/settings` - detection, recording, notifications, retention, backup, Cloudflare Tunnel, and updates
- `/users` - user management (admin)
- `/profile` - change your own password
- `/audit` - audit log
- `/recordings` - recordings list
- `/recordings/timeline` - timeline playback
- `/camera-log` - camera diagnostics
- `/application-log` - in-browser application log viewer
- `/yamnet-tflite` - sound detection backend status

## Events, recordings, and alerts

Daygle uses the **Events** page as its single activity feed; there is no separate Alerts page. Each row represents one object, motion, or sound detection. Events that triggered a notification show an alert badge, and event rows can link to the recording containing that scene and to an annotated snapshot with detection boxes when an image is available.

One recording can contain multiple event rows. Use **Recordings** or **Recordings → Timeline** to review the complete clip, while the event row provides the specific detection context. Access-controlled viewers only see events and recordings they are allowed to view.

Email and push notifications use the same alert title/body format. Configure the channels under **Settings → Notifications**, then enable email or push per zone/sound rule as needed. Camera offline and recovery notifications use the same channels when enabled.

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
./scripts/install_python_deps.sh python requirements.txt
python -m app.server
```

### Service update

For an installed Debian service, use the updater rather than reinstalling the application. The default install directory is `/opt/daygle-ai-camera`; if you set `DAYGLE_APP_DIR`, run the commands from that configured directory instead:

```bash
cd /opt/daygle-ai-camera
sudo ./scripts/update.sh
sudo systemctl restart daygle-ai-camera
```

The updater verifies that the Git origin is the canonical `daygle/daygle-ai-camera` repository, refreshes Python dependencies, provisions the optional `cloudflared` binary, and migrates older systemd launchers to `python -m app.server` when it has the required privileges. It may fall back to installing `cloudflared` inside the application virtual environment when system-wide installation is unavailable.

Admins can also use **Settings → System → Software Updates**. A successful browser-initiated service update schedules a restart when the installation permits it; otherwise restart the service manually.

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
