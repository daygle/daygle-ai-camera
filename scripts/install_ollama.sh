#!/usr/bin/env bash
# Install (or upgrade) a pinned Ollama release as a systemd service, for the
# local vision model behind AI verification, descriptions and search
# (docs/ai-verification.md).
#
# Run by the AI page's "Install Ollama" button (app/ollama_manager.py) and by
# scripts/update.sh. It takes no arguments, so the web UI can only ever run
# this exact, checksummed install.
#
# - Downloads the official release archive from GitHub and verifies its
#   SHA-256 before anything is unpacked. (Ollama's own install.sh is not
#   used: it pipes an unverified download straight into tar.)
# - Installs to /usr/local (bin/ollama and lib/ollama).
# - Creates an "ollama" system user and /etc/systemd/system/ollama.service,
#   listening on 127.0.0.1 only and keeping the model loaded between alerts.
# - Does not install NVIDIA drivers. The archive bundles the CUDA runtime
#   libraries; with a working driver Ollama uses the GPU by itself.
#
# Ownership: the unit it writes carries DAYGLE_MARKER. An Ollama that was
# installed some other way (binary present, no marked unit) is left alone
# unless DAYGLE_OLLAMA_FORCE=1. With DAYGLE_OLLAMA_UPDATE_ONLY=1 (used by
# update.sh) the script only upgrades an installation it made itself and
# otherwise exits 0 without doing anything.
set -euo pipefail

VERSION="${DAYGLE_OLLAMA_VERSION:-0.40.16}"
PINNED_VERSION="0.40.16"
PREFIX="${DAYGLE_OLLAMA_PREFIX:-/usr/local}"
UNIT_PATH="${DAYGLE_OLLAMA_UNIT_PATH:-/etc/systemd/system/ollama.service}"
DOWNLOAD_DIR="${DAYGLE_OLLAMA_TMPDIR:-/var/tmp}"
LISTEN_ADDRESS="127.0.0.1:11434"
DAYGLE_MARKER="# Managed by Daygle AI Camera (scripts/install_ollama.sh)"
BINARY="${PREFIX}/bin/ollama"

# Free space needed, in KiB: the archive is ~1.5 GB, unpacked ~4-5 GB.
DOWNLOAD_SPACE_KIB=$((2 * 1024 * 1024))
INSTALL_SPACE_KIB=$((6 * 1024 * 1024))

log() { echo ">>> $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

if [[ "$(uname -s)" != "Linux" ]]; then
  fail "Ollama can only be installed by Daygle on Linux."
fi
if [[ "${EUID}" -ne 0 ]]; then
  fail "installing Ollama requires root (run with sudo)."
fi
if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
  fail "systemd is not running; Ollama is installed as a systemd service. Install it manually (see docs/ai-verification.md)."
fi

managed_by_daygle() {
  [[ -f "${UNIT_PATH}" ]] && grep -qF "${DAYGLE_MARKER}" "${UNIT_PATH}"
}

installed_version() {
  if [[ -x "${BINARY}" ]]; then
    # "ollama version is 0.40.16" (plus a client/server warning when the
    # server is down); keep just the first version number.
    "${BINARY}" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true
  fi
}

existing_binary=""
if [[ -x "${BINARY}" ]]; then
  existing_binary="${BINARY}"
elif command -v ollama >/dev/null 2>&1; then
  existing_binary="$(command -v ollama)"
fi

if [[ "${DAYGLE_OLLAMA_UPDATE_ONLY:-0}" == "1" ]] && ! managed_by_daygle; then
  echo "Ollama was not installed by Daygle; leaving it unchanged."
  exit 0
fi
if [[ -n "${existing_binary}" ]] && ! managed_by_daygle && [[ "${DAYGLE_OLLAMA_FORCE:-0}" != "1" ]]; then
  fail "Ollama is already installed at ${existing_binary} but was not installed by Daygle. Leaving it unchanged; manage it yourself, or set DAYGLE_OLLAMA_FORCE=1 to replace it."
fi

write_unit() {
  local temp_unit
  temp_unit="$(mktemp "${UNIT_PATH}.tmp.XXXXXX")"
  cat > "${temp_unit}" <<EOF
${DAYGLE_MARKER}
# Rewritten by the installer and updater; put local changes in a drop-in:
#   sudo systemctl edit ollama
[Unit]
Description=Ollama (local vision model for Daygle AI Camera)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=${BINARY} serve
User=ollama
Group=ollama
# Only this server can reach the model API.
Environment="OLLAMA_HOST=${LISTEN_ADDRESS}"
# Keep the model in (GPU) memory, so the first alert after a quiet period
# does not wait for it to reload.
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="HOME=/usr/share/ollama"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "${temp_unit}"
  mv -f "${temp_unit}" "${UNIT_PATH}"
}

ensure_user() {
  if ! id -u ollama >/dev/null 2>&1; then
    log "Creating the ollama system user..."
    useradd --system --user-group --shell /usr/sbin/nologin --create-home --home-dir /usr/share/ollama ollama
  fi
  # GPU device access.
  local group
  for group in render video; do
    if getent group "${group}" >/dev/null 2>&1; then
      usermod -a -G "${group}" ollama
    fi
  done
}

start_service() {
  systemctl daemon-reload
  systemctl enable ollama >/dev/null 2>&1 || systemctl enable ollama
  log "Starting the ollama service..."
  systemctl restart ollama
  local attempt
  for attempt in $(seq 1 30); do
    if curl -fsS --max-time 2 "http://${LISTEN_ADDRESS}/api/version" >/dev/null 2>&1; then
      log "Ollama is answering on ${LISTEN_ADDRESS}."
      return 0
    fi
    sleep 1
  done
  echo "WARNING: the ollama service did not answer within 30 seconds. Check: journalctl -u ollama" >&2
}

CURRENT_VERSION="$(installed_version)"
if [[ "${CURRENT_VERSION}" == "${VERSION}" && "${DAYGLE_OLLAMA_FORCE_UPDATE:-0}" != "1" ]] && managed_by_daygle; then
  echo "Ollama ${VERSION} is already installed at ${BINARY}."
  # Refresh the unit and make sure the service runs (an earlier run may have
  # been interrupted after unpacking).
  ensure_user
  write_unit
  if ! systemctl is-active --quiet ollama; then
    start_service
  else
    systemctl daemon-reload
  fi
  exit 0
fi

case "$(uname -m)" in
  x86_64|amd64)
    ASSET="ollama-linux-amd64.tar.zst"
    DEFAULT_SHA256="87bfafb2acbc89b2e6a1be0a9043de6aa75fb450696f86bc426e65fc448f788d"
    ;;
  aarch64|arm64)
    ASSET="ollama-linux-arm64.tar.zst"
    DEFAULT_SHA256="e45b8e6dcb86cb9f3057396351655079c9a4879b4ced2678c9350d375dcbc224"
    ;;
  *)
    fail "unsupported architecture for Ollama: $(uname -m)"
    ;;
esac

# A different release must bring its own checksum, so a version override can
# never quietly turn verification off.
if [[ "${VERSION}" == "${PINNED_VERSION}" ]]; then
  EXPECTED_SHA256="${DAYGLE_OLLAMA_SHA256:-${DEFAULT_SHA256}}"
elif [[ -n "${DAYGLE_OLLAMA_SHA256:-}" ]]; then
  EXPECTED_SHA256="${DAYGLE_OLLAMA_SHA256}"
else
  fail "DAYGLE_OLLAMA_SHA256 is required when installing Ollama ${VERSION}."
fi
if [[ ! "${EXPECTED_SHA256}" =~ ^[[:xdigit:]]{64}$ ]]; then
  fail "DAYGLE_OLLAMA_SHA256 must be a 64-character SHA-256 digest."
fi

if ! command -v zstd >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    log "Installing zstd (needed to unpack Ollama)..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends zstd \
      || { apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends zstd; }
  else
    fail "zstd is required to unpack Ollama. Install the zstd package and try again."
  fi
fi
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  fail "wget or curl is required to download Ollama."
fi

free_kib() {
  df -Pk "$1" 2>/dev/null | awk 'NR == 2 { print $4 }'
}
mkdir -p "${DOWNLOAD_DIR}" "${PREFIX}/bin" "${PREFIX}/lib"
download_free="$(free_kib "${DOWNLOAD_DIR}")"
install_free="$(free_kib "${PREFIX}/lib")"
if [[ -n "${download_free}" && "${download_free}" -lt "${DOWNLOAD_SPACE_KIB}" ]]; then
  fail "not enough free space in ${DOWNLOAD_DIR} for the download (need 2 GB, have $((download_free / 1024)) MB)."
fi
if [[ -n "${install_free}" && "${install_free}" -lt "${INSTALL_SPACE_KIB}" ]]; then
  fail "not enough free space in ${PREFIX}/lib to install Ollama (need 6 GB, have $((install_free / 1024)) MB)."
fi

WORK_DIR="$(mktemp -d "${DOWNLOAD_DIR}/daygle-ollama.XXXXXX")"
STAGE_DIR="$(mktemp -d "${PREFIX}/lib/.ollama-stage.XXXXXX")"
cleanup() { rm -rf "${WORK_DIR}" "${STAGE_DIR}"; }
trap cleanup EXIT

ARCHIVE="${WORK_DIR}/${ASSET}"
DOWNLOAD_URL="https://github.com/ollama/ollama/releases/download/v${VERSION}/${ASSET}"
log "Downloading Ollama ${VERSION} (${ASSET}, about 1.5 GB)..."
if command -v curl >/dev/null 2>&1; then
  # --no-progress-meter: the output goes to the application log.
  curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 15 --retry 3 --no-progress-meter -o "${ARCHIVE}" "${DOWNLOAD_URL}"
else
  wget --https-only --timeout=30 --tries=3 --no-verbose -O "${ARCHIVE}" "${DOWNLOAD_URL}"
fi

log "Verifying the download..."
printf '%s  %s\n' "${EXPECTED_SHA256}" "${ARCHIVE}" | sha256sum --check --status \
  || fail "checksum mismatch for ${ASSET}; nothing was installed."
echo "Checksum OK."

log "Unpacking..."
zstd -d -c "${ARCHIVE}" | tar -xf - -C "${STAGE_DIR}"
rm -f "${ARCHIVE}"
[[ -x "${STAGE_DIR}/bin/ollama" ]] || fail "the archive does not contain bin/ollama; nothing was installed."

ensure_user
if systemctl is-active --quiet ollama; then
  log "Stopping the running ollama service..."
  systemctl stop ollama
fi

log "Installing to ${PREFIX}..."
rm -rf "${PREFIX}/lib/ollama"
if [[ -d "${STAGE_DIR}/lib/ollama" ]]; then
  mv "${STAGE_DIR}/lib/ollama" "${PREFIX}/lib/ollama"
  chown -R root:root "${PREFIX}/lib/ollama"
fi
install -o root -g root -m 0755 "${STAGE_DIR}/bin/ollama" "${BINARY}"

write_unit
start_service
log "Ollama $(installed_version || echo "${VERSION}") installed. Next: download a vision model on the AI page."
