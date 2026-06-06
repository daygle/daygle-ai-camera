#!/usr/bin/env bash
set -euo pipefail

APP_NAME="daygle-ai-camera"
APP_USER="${DAYGLE_USER:-daygle}"
APP_DIR="${DAYGLE_APP_DIR:-/opt/daygle-ai-camera}"
CONFIG_DIR="${DAYGLE_CONFIG_DIR:-/etc/daygle-ai-camera}"
DATA_DIR="${DAYGLE_DATA_DIR:-${APP_DIR}/data}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root: sudo ./scripts/install_debian.sh" >&2
  exit 1
fi

echo "Installing Daygle AI Camera on Debian 13 (Trixie)"

apt-get update
apt-get install -y --no-install-recommends \
  git \
  python3 \
  python3-pip \
  python3-dev \
  python3-venv \
  sqlite3 \
  ca-certificates \
  rsync \
  ffmpeg \
  v4l-utils \
  libgl1 \
  libglib2.0-0

# Create system user if missing
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/var/lib/${APP_USER}" --shell /usr/sbin/nologin "${APP_USER}"
fi

# Create directories
mkdir -p "${APP_DIR}" "${CONFIG_DIR}" "${DATA_DIR}"

# Sync application files (DO NOT EXCLUDE data/)
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  "${REPO_DIR}/" "${APP_DIR}/"

# Ensure data directory exists (GitHub + installer safety)
mkdir -p "${APP_DIR}/data"

# Python virtual environment
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel

# Install CPU-only PyTorch FIRST to avoid CUDA wheels
"${APP_DIR}/.venv/bin/python" -m pip install torch --index-url https://download.pytorch.org/whl/cpu

# Install remaining dependencies (pytest intentionally excluded)
grep -v '^torch' "${APP_DIR}/requirements.txt" > "${APP_DIR}/requirements.no-torch.txt"
"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.no-torch.txt"
rm "${APP_DIR}/requirements.no-torch.txt"

# Minimal bootstrap config
if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  cat > "${CONFIG_DIR}/config.yaml" <<EOF
# Minimal bootstrap config.
server:
  host: 0.0.0.0
  port: 8080

auth:
  enabled: true

storage:
  database: data/daygle_ai_camera.sqlite3
EOF
fi

# Install systemd service
install -m 0644 "${APP_DIR}/systemd/${APP_NAME}.service" "${SERVICE_FILE}"

# Patch service file with correct paths
sed -i \
  -e "s#WorkingDirectory=/opt/daygle-ai-camera#WorkingDirectory=${APP_DIR}#" \
  -e "s#Environment=DAYGLE_CONFIG=/etc/daygle-ai-camera/config.yaml#Environment=DAYGLE_CONFIG=${CONFIG_DIR}/config.yaml#" \
  -e "s#ExecStart=/opt/daygle-ai-camera/.venv/bin/python#ExecStart=${APP_DIR}/.venv/bin/python#" \
  -e "s#User=daygle#User=${APP_USER}#" \
  -e "s#Group=daygle#Group=${APP_USER}#" \
  "${SERVICE_FILE}"

# Permissions
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}" "${CONFIG_DIR}" "${DATA_DIR}"

# Enable service
systemctl daemon-reload
systemctl enable --now "${APP_NAME}.service"

echo "Daygle AI Camera is installed."
echo "Service status: sudo systemctl status ${APP_NAME}"
echo "Logs: sudo journalctl -u ${APP_NAME} -f"
echo "Dashboard: http://<server-ip>:8080/"
