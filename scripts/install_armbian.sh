#!/usr/bin/env bash
set -euo pipefail

APP_NAME="daygle-ai-camera"
APP_USER="${DAYGLE_USER:-daygle}"
APP_DIR="${DAYGLE_APP_DIR:-/opt/daygle-ai-camera}"
CONFIG_DIR="${DAYGLE_CONFIG_DIR:-/etc/daygle-ai-camera}"
DATA_DIR="${DAYGLE_DATA_DIR:-/var/lib/daygle-ai-camera}"
MODEL_DIR="${APP_DIR}/models"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root: sudo ./scripts/install_armbian.sh" >&2
  exit 1
fi

echo "Installing Daygle AI Camera for Armbian 26.x / Orange Pi 3B"

apt-get update
apt-get install -y --no-install-recommends \
  python3 \
  python3-venv \
  python3-pip \
  sqlite3 \
  ca-certificates \
  rsync

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/var/lib/${APP_USER}" --shell /usr/sbin/nologin "${APP_USER}"
fi

mkdir -p "${APP_DIR}" "${CONFIG_DIR}" "${DATA_DIR}" "${MODEL_DIR}"
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'data' \
  --exclude 'models/*.onnx' \
  --exclude 'models/*.pt' \
  "${REPO_DIR}/" "${APP_DIR}/"

mkdir -p "${DATA_DIR}" "${MODEL_DIR}"

# Python virtual environment. The dependency helper defaults to CPU-only
# PyTorch and --no-cache-dir to avoid pulling/caching large CUDA wheels during
# service installs on small disks or container overlays.
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/scripts/install_python_deps.sh" "${APP_DIR}/.venv/bin/python" "${APP_DIR}/requirements.txt"

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  cat > "${CONFIG_DIR}/config.yaml" <<EOF
server:
  host: 0.0.0.0
  port: 8080

auth:
  enabled: true
  cookie_name: daygle_session

storage:
  database: ${DATA_DIR}/daygle_ai_camera.sqlite3
  data_dir: ${DATA_DIR}
  snapshots_dir: ${DATA_DIR}/snapshots
  events_dir: ${DATA_DIR}/events
  recordings_dir: ${DATA_DIR}/recordings
  plates_dir: ${DATA_DIR}/plates
EOF
fi

install -m 0644 "${APP_DIR}/systemd/${APP_NAME}.service" "${SERVICE_FILE}"
# Armbian installs run the application service as root for hardware/device
# access, while explicitly allowing ONNX/PT model exports under models.
sed -i \
  -e "s#WorkingDirectory=/opt/daygle-ai-camera#WorkingDirectory=${APP_DIR}#" \
  -e "s#Environment=DAYGLE_CONFIG=/etc/daygle-ai-camera/config.yaml#Environment=DAYGLE_CONFIG=${CONFIG_DIR}/config.yaml#" \
  -e "s#ExecStart=/opt/daygle-ai-camera/.venv/bin/python#ExecStart=${APP_DIR}/.venv/bin/python#" \
  -e "s#User=.*#User=root#" \
  -e "s#Group=.*#Group=root#" \
  -e "s#ReadWritePaths=/etc/daygle-ai-camera /opt/daygle-ai-camera/data#ReadWritePaths=${CONFIG_DIR} ${DATA_DIR} ${MODEL_DIR}#" \
  "${SERVICE_FILE}"

# Permissions: the Armbian service runs as root, but keep runtime data and
# downloaded/exported ONNX/PT model assets group-writable for the Daygle user.
chown -R root:root "${APP_DIR}" "${CONFIG_DIR}"
chown -R root:"${APP_USER}" "${DATA_DIR}" "${MODEL_DIR}"
chmod 0755 "${APP_DIR}" "${CONFIG_DIR}"
chmod 2775 "${DATA_DIR}" "${MODEL_DIR}"

systemctl daemon-reload
systemctl enable --now "${APP_NAME}.service"

echo "Daygle AI Camera is installed and running as root."
echo "Service status: sudo systemctl status ${APP_NAME}"
echo "Logs: sudo journalctl -u ${APP_NAME} -f"
echo "Dashboard: http://<orange-pi-ip>:8080/"
