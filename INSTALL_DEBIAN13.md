# Install on Debian 13 without a virtual environment

This guide installs Daygle AI Camera on Debian 13 without using `venv`.
It includes instructions for a normal Debian 13 machine and for a Debian 13
Proxmox LXC container.

Debian 13 protects the system Python environment. Because this install does
not use a virtual environment, Python packages are installed with
`--break-system-packages`. This is acceptable for a dedicated appliance-style
host or container, but a virtual environment remains safer on a general-purpose
machine.

## Debian apt software

Install these Debian packages before installing the Python requirements:

```bash
sudo apt update
sudo apt install -y --no-install-recommends \
  git \
  python3 \
  python3-pip \
  python3-dev \
  sqlite3 \
  ca-certificates \
  rsync \
  ffmpeg \
  v4l-utils \
  libgl1 \
  libglib2.0-0
```

Package notes:

- `git`: clone or update the project source.
- `python3`, `python3-pip`, `python3-dev`: run the app and install Python packages.
- `sqlite3`: inspect or maintain the local SQLite database.
- `ca-certificates`: allow HTTPS package and model downloads.
- `rsync`: copy the application into `/opt/daygle-ai-camera`.
- `ffmpeg`: media tooling for camera/video workflows.
- `v4l-utils`: inspect Linux camera devices such as `/dev/video0`.
- `libgl1`, `libglib2.0-0`: runtime libraries commonly needed by OpenCV.

Inside a Proxmox LXC container where you are already root, omit `sudo`.

## Install on Debian 13

### 1. Install system packages

Install the Debian apt software listed above:

```bash
sudo apt update
sudo apt install -y --no-install-recommends \
  git \
  python3 \
  python3-pip \
  python3-dev \
  sqlite3 \
  ca-certificates \
  rsync \
  ffmpeg \
  v4l-utils \
  libgl1 \
  libglib2.0-0
```

### 2. Get the application source

Clone the repository, or copy an existing checkout onto the Debian machine.

```bash
git clone <repository-url> daygle-ai-camera
cd daygle-ai-camera
```

If the source is already present, run the remaining commands from the
repository root.

### 3. Install Python dependencies system-wide

```bash
sudo python3 -m pip install --upgrade --break-system-packages pip wheel
sudo python3 -m pip install --break-system-packages -r requirements.txt
```

### 4. Create the service user and install paths

```bash
sudo useradd --system --create-home --home-dir /var/lib/daygle --shell /usr/sbin/nologin daygle

sudo mkdir -p /opt/daygle-ai-camera
sudo mkdir -p /etc/daygle-ai-camera
sudo mkdir -p /var/lib/daygle-ai-camera/snapshots
sudo mkdir -p /var/lib/daygle-ai-camera/events
```

If the `daygle` user already exists, the `useradd` command will fail with a
message that the user exists. That is fine; continue with the next command.

### 5. Copy the application into `/opt`

From the repository root:

```bash
sudo rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'data' \
  ./ /opt/daygle-ai-camera/
```

### 6. Create the configuration file

```bash
sudo cp /opt/daygle-ai-camera/config.example.yaml /etc/daygle-ai-camera/config.yaml
```

Edit the storage paths so the service writes under `/var/lib/daygle-ai-camera`:

```bash
sudo sed -i \
  -e 's#database: data/daygle_ai_camera.sqlite3#database: /var/lib/daygle-ai-camera/daygle_ai_camera.sqlite3#' \
  -e 's#data_dir: data#data_dir: /var/lib/daygle-ai-camera#' \
  -e 's#snapshots_dir: data/snapshots#snapshots_dir: /var/lib/daygle-ai-camera/snapshots#' \
  -e 's#events_dir: data/events#events_dir: /var/lib/daygle-ai-camera/events#' \
  /etc/daygle-ai-camera/config.yaml
```

### 7. Set ownership

```bash
sudo chown -R daygle:daygle \
  /opt/daygle-ai-camera \
  /etc/daygle-ai-camera \
  /var/lib/daygle-ai-camera
```

### 8. Create a no-venv systemd service

Create `/etc/systemd/system/daygle-ai-camera.service`:

```bash
sudo tee /etc/systemd/system/daygle-ai-camera.service >/dev/null <<'EOF'
[Unit]
Description=Daygle AI Camera FastAPI service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=daygle
Group=daygle
WorkingDirectory=/opt/daygle-ai-camera
Environment=DAYGLE_CONFIG=/etc/daygle-ai-camera/config.yaml
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/lib/daygle-ai-camera /etc/daygle-ai-camera /opt/daygle-ai-camera/data

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now daygle-ai-camera.service
```

### 9. Check the service

```bash
sudo systemctl status daygle-ai-camera
sudo journalctl -u daygle-ai-camera -f
```

Open the dashboard:

```text
http://<debian-host-ip>:8080/
```

On first run, create the initial administrator account at `/setup`.

## Install in a Debian 13 Proxmox LXC container

These steps assume you are creating or using a Debian 13 LXC container in
Proxmox VE and want Daygle AI Camera to run inside the container without a
Python virtual environment.

### 1. Container recommendations

Recommended container settings:

- Debian 13 template.
- 1 or more CPU cores.
- 1 GiB RAM minimum; 2 GiB or more recommended when using ONNX inference.
- 4 GiB disk minimum; more if storing many snapshots/events.
- `systemd` enabled in the container.
- Network configured with a static IP or DHCP reservation.
- Unprivileged container for normal mock/test use.
- Privileged container or explicit device passthrough if using a physical
  camera such as `/dev/video0`.

For first validation, keep the default mock camera and mock detector enabled.
That avoids camera passthrough while proving the web app and service work.

### 2. Create or enter the container

Create the Debian 13 container from the Proxmox UI or with `pct`. Then enter it
from the Proxmox host:

```bash
pct enter <container-id>
```

Inside the container, confirm Debian:

```bash
cat /etc/debian_version
```

### 3. Install packages inside the container

Run the same Debian apt software install used for a regular Debian host.
Inside the container you are usually root, so these commands omit `sudo`:

```bash
apt update
apt install -y --no-install-recommends \
  git \
  python3 \
  python3-pip \
  python3-dev \
  sqlite3 \
  ca-certificates \
  rsync \
  ffmpeg \
  v4l-utils \
  libgl1 \
  libglib2.0-0
```

### 4. Install the application inside the container

Inside the container:

```bash
git clone <repository-url> daygle-ai-camera
cd daygle-ai-camera

python3 -m pip install --upgrade --break-system-packages pip wheel
python3 -m pip install --break-system-packages -r requirements.txt

useradd --system --create-home --home-dir /var/lib/daygle --shell /usr/sbin/nologin daygle

mkdir -p /opt/daygle-ai-camera
mkdir -p /etc/daygle-ai-camera
mkdir -p /var/lib/daygle-ai-camera/snapshots
mkdir -p /var/lib/daygle-ai-camera/events

rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'data' \
  ./ /opt/daygle-ai-camera/

cp /opt/daygle-ai-camera/config.example.yaml /etc/daygle-ai-camera/config.yaml

sed -i \
  -e 's#database: data/daygle_ai_camera.sqlite3#database: /var/lib/daygle-ai-camera/daygle_ai_camera.sqlite3#' \
  -e 's#data_dir: data#data_dir: /var/lib/daygle-ai-camera#' \
  -e 's#snapshots_dir: data/snapshots#snapshots_dir: /var/lib/daygle-ai-camera/snapshots#' \
  -e 's#events_dir: data/events#events_dir: /var/lib/daygle-ai-camera/events#' \
  /etc/daygle-ai-camera/config.yaml

chown -R daygle:daygle \
  /opt/daygle-ai-camera \
  /etc/daygle-ai-camera \
  /var/lib/daygle-ai-camera
```

Create the systemd service:

```bash
tee /etc/systemd/system/daygle-ai-camera.service >/dev/null <<'EOF'
[Unit]
Description=Daygle AI Camera FastAPI service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=daygle
Group=daygle
WorkingDirectory=/opt/daygle-ai-camera
Environment=DAYGLE_CONFIG=/etc/daygle-ai-camera/config.yaml
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/lib/daygle-ai-camera /etc/daygle-ai-camera /opt/daygle-ai-camera/data

[Install]
WantedBy=multi-user.target
EOF
```

Start the service:

```bash
systemctl daemon-reload
systemctl enable --now daygle-ai-camera.service
systemctl status daygle-ai-camera
```

Open the dashboard:

```text
http://<container-ip>:8080/
```

### 5. Optional camera passthrough for Proxmox LXC

Skip this section if you are using mock camera mode.

On the Proxmox host, find the camera device:

```bash
ls -l /dev/video*
```

Stop the container:

```bash
pct stop <container-id>
```

Pass `/dev/video0` into the container:

```bash
pct set <container-id> -mp0 /dev/video0,mp=/dev/video0
```

Start and enter the container:

```bash
pct start <container-id>
pct enter <container-id>
```

Inside the container, confirm the device is visible:

```bash
ls -l /dev/video0
v4l2-ctl --list-devices
```

If the service user cannot read the device, add it to the `video` group:

```bash
usermod -aG video daygle
systemctl restart daygle-ai-camera
```

Proxmox device passthrough can vary by host configuration. If `/dev/video0` is
not visible or cannot be opened inside an unprivileged container, use a
privileged container or run Daygle AI Camera directly on the host that owns the
camera.

## Updating

For a Debian host or Proxmox container, update from the repository root:

```bash
git pull
sudo rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'data' \
  ./ /opt/daygle-ai-camera/
sudo chown -R daygle:daygle /opt/daygle-ai-camera
sudo python3 -m pip install --break-system-packages -r /opt/daygle-ai-camera/requirements.txt
sudo systemctl restart daygle-ai-camera
```

Inside a Proxmox container where you are already root, omit `sudo`.

## Useful checks

```bash
python3 -m uvicorn --version
python3 -c "import fastapi, uvicorn, cv2, onnxruntime; print('dependencies ok')"
systemctl status daygle-ai-camera
journalctl -u daygle-ai-camera -f
```

## Troubleshooting

- `externally-managed-environment`: rerun the pip command with
  `--break-system-packages`, or use the virtual environment install path from
  `README.md`.
- `No module named uvicorn`: Python dependencies were not installed into the
  system Python. Run `python3 -m pip install --break-system-packages -r requirements.txt`.
- Service starts but the page does not load: check the host or container IP,
  confirm port `8080` is reachable, and inspect `journalctl -u daygle-ai-camera`.
- Cannot write database or snapshots: check ownership of
  `/var/lib/daygle-ai-camera` and confirm the service runs as `daygle`.
- Camera does not appear in Proxmox LXC: confirm the device exists on the
  Proxmox host, pass it into the container, and confirm the `daygle` user can
  read it.
