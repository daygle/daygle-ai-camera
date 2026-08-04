#!/usr/bin/env bash
set -euo pipefail

APP_NAME="daygle-ai-camera"
APP_USER="${DAYGLE_USER:-daygle}"
APP_DIR="${DAYGLE_APP_DIR:-/opt/daygle-ai-camera}"
CONFIG_DIR="${DAYGLE_CONFIG_DIR:-/etc/daygle-ai-camera}"
DATA_DIR="${DAYGLE_DATA_DIR:-${APP_DIR}/data}"
MODEL_DIR="${APP_DIR}/models"
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
  libglib2.0-0 \
  wget \
  coreutils

# Create system user for optional maintenance access to writable runtime assets
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/var/lib/${APP_USER}" --shell /usr/sbin/nologin "${APP_USER}"
fi

# Create directories
mkdir -p "${APP_DIR}" "${CONFIG_DIR}" "${DATA_DIR}" "${MODEL_DIR}"

# Install the cloudflared connector used by Daygle's optional built-in
# Cloudflare Tunnel manager. The helper is idempotent and leaves an existing
# administrator-managed binary unchanged.
"${REPO_DIR}/scripts/install_cloudflared.sh"

# Sync application files
# Origin-URL allowlist - refuse to install from a tampered repo. Mirrors the
# guard in ``scripts/update.sh`` so that any future re-run of the installer
# against a REPO_DIR with a poisoned ``.git/config`` fails FAST instead of
# rsyncing attacker-controlled source into /opt. SSH and HTTPS forms both
# supported, optional ``.git`` suffix tolerated.
EXPECTED_REMOTE_REGEX='github\.com[:/]daygle/daygle-ai-camera(\.git)?$'
REPO_REMOTE="$(git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || true)"
if [[ -n "${REPO_REMOTE}" ]] && ! printf '%s' "${REPO_REMOTE}" | grep -Eq "${EXPECTED_REMOTE_REGEX}"; then
  echo "ERROR: refusing to install from non-allowlisted source repo." >&2
  echo "  Repo origin remote: '${REPO_REMOTE}'" >&2
  echo "  Expected pattern: ${EXPECTED_REMOTE_REGEX}" >&2
  echo "  Fix: 'git -C \"${REPO_DIR}\" remote set-url origin https://github.com/daygle/daygle-ai-camera.git'" >&2
  exit 1
fi
if [[ -n "${REPO_REMOTE}" ]]; then
  echo "Source repo origin verified: ${REPO_REMOTE}"
fi

rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'data' \
  --exclude 'models/*.onnx' \
  --exclude 'models/*.pt' \
  "${REPO_DIR}/" "${APP_DIR}/"

mkdir -p "${DATA_DIR}" "${MODEL_DIR}"

# Resolve the ONNX Runtime wheel before creating/updating the environment.
# ``auto`` selects GPU only when the NVIDIA driver can enumerate a device;
# explicit ``cpu``/``gpu`` values are respected for operators who want a
# deterministic choice. Driver/CUDA installation is intentionally not done by
# this script because it is kernel-, repository-, and Secure-Boot-dependent.
REQUESTED_VARIANT="${DAYGLE_ONNXRUNTIME_VARIANT:-auto}"
case "${REQUESTED_VARIANT}" in
  auto)
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
      INSTALL_VARIANT='gpu'
    else
      INSTALL_VARIANT='cpu'
    fi
    ;;
  cpu|gpu)
    INSTALL_VARIANT="${REQUESTED_VARIANT}"
    ;;
  *)
    echo "ERROR: DAYGLE_ONNXRUNTIME_VARIANT must be 'auto', 'cpu', or 'gpu' (got '${REQUESTED_VARIANT}')." >&2
    exit 1
    ;;
esac
if [[ "${INSTALL_VARIANT}" == 'gpu' ]] && ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "WARNING: GPU dependency variant selected, but nvidia-smi is not installed or not on PATH." >&2
  echo "         Install and verify the NVIDIA driver before starting the service." >&2
fi
export DAYGLE_ONNXRUNTIME_VARIANT="${INSTALL_VARIANT}"
echo "Installing ONNX Runtime variant: ${INSTALL_VARIANT}"

# Python virtual environment. The dependency helper removes the opposite
# ONNX Runtime wheel before installation, so re-running the installer can
# safely switch an existing environment between CPU and GPU variants.
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/scripts/install_python_deps.sh" "${APP_DIR}/.venv/bin/python" "${APP_DIR}/requirements.txt"

# Install optional ONNX simplifier used by some model export workflows.
echo "Installing optional ONNX tooling..."
"${APP_DIR}/.venv/bin/python" -m pip install --no-cache-dir onnxsim

# Minimal bootstrap config
if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  cat > "${CONFIG_DIR}/config.yaml" <<EOF
server:
  host: 0.0.0.0
  port: 8080

auth:
  enabled: true

storage:
  database: ${DATA_DIR}/daygle_ai_camera.sqlite3
  data_dir: ${DATA_DIR}
  snapshots_dir: ${DATA_DIR}/snapshots
  events_dir: ${DATA_DIR}/events
  recordings_dir: ${DATA_DIR}/recordings
EOF
fi

# Install systemd service
install -m 0644 "${APP_DIR}/systemd/${APP_NAME}.service" "${SERVICE_FILE}"

# Patch service file for the configured install paths. Debian installs run the
# application service as root for hardware/device access, while explicitly
# allowing ONNX/PT model exports under the models directory.
_esc() { printf '%s\n' "$1" | sed 's|[&/\\]|\\&|g'; }
sed -i \
  -e "s|WorkingDirectory=/opt/daygle-ai-camera|WorkingDirectory=$(_esc "${APP_DIR}")|" \
  -e "s|Environment=DAYGLE_CONFIG=/etc/daygle-ai-camera/config.yaml|Environment=DAYGLE_CONFIG=$(_esc "${CONFIG_DIR}/config.yaml")|" \
  -e "s|ExecStart=/opt/daygle-ai-camera/.venv/bin/python|ExecStart=$(_esc "${APP_DIR}/.venv/bin/python")|" \
  -e "s|User=.*|User=root|" \
  -e "s|Group=.*|Group=root|" \
  -e "s|ReadWritePaths=/etc/daygle-ai-camera /opt/daygle-ai-camera/data|ReadWritePaths=$(_esc "${CONFIG_DIR} ${DATA_DIR} ${MODEL_DIR}")|" \
  "${SERVICE_FILE}"

# Permissions: the Debian service runs as root, but keep runtime data and
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
echo "Dashboard: http://<server-ip>:8080/"
