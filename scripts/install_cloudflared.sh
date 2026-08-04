#!/usr/bin/env bash
# Install the cloudflared binary required by Daygle's optional Cloudflare Tunnel.
set -euo pipefail

INSTALL_PATH="${DAYGLE_CLOUDFLARED_PATH:-/usr/local/bin/cloudflared}"
VERSION="${DAYGLE_CLOUDFLARED_VERSION:-2026.7.3}"

if [[ -x "${INSTALL_PATH}" && "${DAYGLE_CLOUDFLARED_FORCE_UPDATE:-0}" != "1" ]]; then
  echo "cloudflared already installed at ${INSTALL_PATH}; leaving it unchanged."
  exit 0
fi

case "$(uname -m)" in
  x86_64|amd64)
    ASSET="cloudflared-linux-amd64"
    DEFAULT_SHA256="9d71c677db00134c1bd4144b7783486b654ad281b1ea62b4972098d19f770f17"
    ;;
  aarch64|arm64)
    ASSET="cloudflared-linux-arm64"
    DEFAULT_SHA256="65259e652a7bea08bf5df603233ab22b8bf3116af8df9f9206209af6a1b955c0"
    ;;
  *)
    echo "ERROR: unsupported architecture for bundled cloudflared: $(uname -m)" >&2
    exit 1
    ;;
esac

# A custom release must provide its checksum explicitly. This prevents an
# apparently convenient version override from silently disabling verification.
if [[ "${VERSION}" == "2026.7.3" ]]; then
  EXPECTED_SHA256="${DAYGLE_CLOUDFLARED_SHA256:-${DEFAULT_SHA256}}"
elif [[ -n "${DAYGLE_CLOUDFLARED_SHA256:-}" ]]; then
  EXPECTED_SHA256="${DAYGLE_CLOUDFLARED_SHA256}"
else
  echo "ERROR: DAYGLE_CLOUDFLARED_SHA256 is required when using version ${VERSION}." >&2
  exit 1
fi
if [[ ! "${EXPECTED_SHA256}" =~ ^[[:xdigit:]]{64}$ ]]; then
  echo "ERROR: DAYGLE_CLOUDFLARED_SHA256 must be a 64-character SHA-256 digest." >&2
  exit 1
fi

PARENT_DIR="$(dirname "${INSTALL_PATH}")"
mkdir -p "${PARENT_DIR}"
if [[ "${EUID}" -ne 0 && ! -w "${PARENT_DIR}" ]]; then
  echo "ERROR: installing cloudflared requires root or write access to ${PARENT_DIR}." >&2
  exit 1
fi

DOWNLOAD_URL="https://github.com/cloudflare/cloudflared/releases/download/${VERSION}/${ASSET}"
TEMP_PATH="$(mktemp "${PARENT_DIR}/.cloudflared.tmp.XXXXXX")"
CHECKSUM_FILE="${TEMP_PATH}.sha256"
trap 'rm -f "${TEMP_PATH}" "${CHECKSUM_FILE}"' EXIT

echo "Installing cloudflared ${VERSION} (${ASSET})..."
if command -v wget >/dev/null 2>&1; then
  wget --https-only --timeout=30 --tries=3 -O "${TEMP_PATH}" "${DOWNLOAD_URL}"
elif command -v curl >/dev/null 2>&1; then
  curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 15 --retry 3 -o "${TEMP_PATH}" "${DOWNLOAD_URL}"
else
  echo "ERROR: wget or curl is required to install cloudflared." >&2
  exit 1
fi

printf '%s  %s\n' "${EXPECTED_SHA256}" "${TEMP_PATH}" > "${CHECKSUM_FILE}"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum --check "${CHECKSUM_FILE}"
elif command -v shasum >/dev/null 2>&1; then
  (cd "${PARENT_DIR}" && shasum -a 256 -c "$(basename "${CHECKSUM_FILE}")")
else
  echo "ERROR: sha256sum or shasum is required to verify cloudflared." >&2
  exit 1
fi

chmod 0755 "${TEMP_PATH}"
mv -f "${TEMP_PATH}" "${INSTALL_PATH}"
trap - EXIT
rm -f "${CHECKSUM_FILE}"
echo "cloudflared installed at ${INSTALL_PATH}."
