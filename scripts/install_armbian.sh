#!/usr/bin/env bash
set -euo pipefail

APP_NAME="daygle-ai-camera"
APP_USER="${DAYGLE_USER:-daygle}"
APP_DIR="${DAYGLE_APP_DIR:-/opt/daygle-ai-camera}"
CONFIG_DIR="${DAYGLE_CONFIG_DIR:-/etc/daygle-ai-camera}"
DATA_DIR="${DAYGLE_DATA_DIR:-/var/lib/daygle-ai-camera}"
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

mkdir -p "${APP_DIR}" "${CONFIG_DIR}" "${DATA_DIR}/snapshots" "${DATA_DIR}/events"
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'data' \
  "${REPO_DIR}/" "${APP_DIR}/"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel
"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  cp "${APP_DIR}/config.example.yaml" "${CONFIG_DIR}/config.yaml"
  sed -i \
    -e "s#database: data/daygle_ai_camera.sqlite3#database: ${DATA_DIR}/daygle_ai_camera.sqlite3#" \
    -e "s#data_dir: data#data_dir: ${DATA_DIR}#" \
    -e "s#snapshots_dir: data/snapshots#snapshots_dir: ${DATA_DIR}/snapshots#" \
    -e "s#events_dir: data/events#events_dir: ${DATA_DIR}/events#" \
    "${CONFIG_DIR}/config.yaml"
fi

install -m 0644 "${APP_DIR}/systemd/${APP_NAME}.service" "${SERVICE_FILE}"
sed -i \
  -e "s#WorkingDirectory=/opt/daygle-ai-camera#WorkingDirectory=${APP_DIR}#" \
  -e "s#Environment=DAYGLE_CONFIG=/etc/daygle-ai-camera/config.yaml#Environment=DAYGLE_CONFIG=${CONFIG_DIR}/config.yaml#" \
  -e "s#ExecStart=/opt/daygle-ai-camera/.venv/bin/python#ExecStart=${APP_DIR}/.venv/bin/python#" \
  -e "s#User=daygle#User=${APP_USER}#" \
  -e "s#Group=daygle#Group=${APP_USER}#" \
  "${SERVICE_FILE}"

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}" "${CONFIG_DIR}" "${DATA_DIR}"

systemctl daemon-reload
systemctl enable --now "${APP_NAME}.service"

echo "Daygle AI Camera is installed."
echo "Service status: sudo systemctl status ${APP_NAME}"
echo "Logs: sudo journalctl -u ${APP_NAME} -f"
echo "Dashboard: http://<orange-pi-ip>:8080/"
