# Daygle AI Camera

A modern, self-hosted AI camera project designed for an Orange Pi 3B running Armbian with an OV5647 camera module.

This first version gives you a clean foundation:

- FastAPI backend
- Modern responsive web dashboard
- Live MJPEG camera stream
- Snapshot endpoint
- Pluggable AI detector layer
- YAML configuration
- Local event directory structure
- systemd service file
- Armbian install script

The AI detector is intentionally pluggable. Start with camera streaming first, then add ONNX/TFLite/RKNN inference once the OV5647 capture path is confirmed.

## Hardware target

- Orange Pi 3B
- OV5647 camera module
- Armbian
- Python 3.10+

## Project layout

```text
.
├── app/
│   ├── main.py              # FastAPI app
│   ├── camera.py            # OpenCV/V4L2 camera capture
│   ├── detector.py          # AI detector abstraction
│   ├── settings.py          # YAML config loader
│   └── storage.py           # Snapshot/event storage helpers
├── web/
│   └── static/
│       ├── index.html       # Modern dashboard
│       ├── styles.css
│       └── app.js
├── scripts/
│   └── install_armbian.sh
├── systemd/
│   └── daygle-ai-camera.service
├── config.example.yaml
├── requirements.txt
└── README.md
```

## Quick start on Armbian

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip python3-opencv v4l-utils ffmpeg

git clone https://github.com/daygle/daygle-ai-camera.git
cd daygle-ai-camera

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp config.example.yaml config.yaml
python -m app.main
```

Open:

```text
http://<orange-pi-ip>:8080
```

## Check camera detection

Before running the app, confirm Linux can see the camera:

```bash
v4l2-ctl --list-devices
ls -l /dev/video*
```

If `/dev/video0` exists, the default config should work.

If the OV5647 does not appear, the issue is likely kernel/device-tree/CSI support rather than the Python application.

## Running as a service

After testing manually:

```bash
sudo ./scripts/install_armbian.sh
sudo systemctl status daygle-ai-camera
```

## Configuration

Copy the example config:

```bash
cp config.example.yaml config.yaml
```

Main settings:

```yaml
server:
  host: 0.0.0.0
  port: 8080

camera:
  device: 0
  width: 1280
  height: 720
  fps: 15
  flip: none

ai:
  enabled: false
  backend: mock
  confidence: 0.45
  model_path: models/model.onnx

storage:
  data_dir: data
  snapshots_dir: data/snapshots
  events_dir: data/events
```

## AI roadmap

Recommended build order:

1. Confirm OV5647 appears as `/dev/video0`.
2. Confirm dashboard live stream works.
3. Add motion detection.
4. Add ONNX object detection.
5. Add RKNN/NPU acceleration later if the board/kernel supports it.

## API endpoints

| Endpoint | Purpose |
|---|---|
| `/` | Web dashboard |
| `/api/status` | Camera/app status |
| `/api/config` | Current safe config |
| `/api/snapshot` | Capture snapshot |
| `/stream.mjpg` | Live MJPEG stream |

## Development notes

This project currently uses OpenCV capture because it is easy to test on Armbian. If your OV5647 only works through libcamera, the next step is adding a `libcamera-vid` capture backend.