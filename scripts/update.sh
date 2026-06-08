#!/usr/bin/env bash
# Daygle AI Camera - in-place updater
# Pulls the latest code from git and reinstalls Python dependencies.
# Service restart is handled separately by the caller (web API or manual).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${APP_DIR}"

echo "=== Daygle AI Camera Updater ==="
echo "App directory: ${APP_DIR}"

if ! git -C "${APP_DIR}" rev-parse --git-dir >/dev/null 2>&1; then
  echo "ERROR: ${APP_DIR} is not a git repository. Cannot auto-update." >&2
  exit 1
fi

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
CURRENT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo "Current branch: ${CURRENT_BRANCH} (${CURRENT_COMMIT})"
echo ""

echo "Fetching latest changes from origin..."
git fetch origin

echo "Pulling latest changes on ${CURRENT_BRANCH}..."
git pull origin "${CURRENT_BRANCH}"

NEW_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo "Now at commit: ${NEW_COMMIT}"

# Update VERSION from the latest git tag so the web UI reflects the new version
LATEST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
if [[ -n "${LATEST_TAG}" ]]; then
  echo "${LATEST_TAG#v}" > "${APP_DIR}/VERSION"
  echo "Updated VERSION to ${LATEST_TAG#v}"
fi

echo ""
echo "Updating Python dependencies..."
if [[ -f "${APP_DIR}/.venv/bin/pip" ]]; then
  "${APP_DIR}/.venv/bin/pip" install --no-cache-dir -r "${APP_DIR}/requirements.txt"
elif [[ -f "${APP_DIR}/.venv/Scripts/pip.exe" ]]; then
  "${APP_DIR}/.venv/Scripts/pip.exe" install --no-cache-dir -r "${APP_DIR}/requirements.txt"
else
  pip install --no-cache-dir -r "${APP_DIR}/requirements.txt"
fi

echo ""
echo "=== Update complete. Restart the service to apply changes. ==="
