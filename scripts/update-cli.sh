#!/usr/bin/env bash
# Daygle AI Camera - CLI updater
# Checks for updates from GitHub, shows release info, and applies the update.
# Mirrors the update flow from the web application GUI.
set -euo pipefail

# ── Usage ────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: $(basename "$0")"
  echo ""
  echo "Checks for updates from GitHub, shows release info, and applies the update."
  echo "Mirrors the update flow from the web application GUI."
  echo ""
  echo "Options:"
  echo "  -h, --help    Show this help message"
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

GITHUB_REPO="daygle/daygle-ai-camera"

# ── Pre-flight checks ──────────────────────────────────────────────────────
for cmd in curl git python3; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "ERROR: '${cmd}' is required but not found in PATH." >&2
    exit 1
  fi
done

# ── Helpers ──────────────────────────────────────────────────────────────────

# Read current version from VERSION file
current_version() {
  if [[ -f "${APP_DIR}/VERSION" ]]; then
    cat "${APP_DIR}/VERSION" | tr -d '[:space:]'
  else
    echo "unknown"
  fi
}

# Convert semver string to comparable integer tuple: "1.2.3" → "001002003"
parse_semver() {
  local IFS='.'
  local parts=($1)
  local major="${parts[0]:-0}"
  local minor="${parts[1]:-0}"
  local patch="${parts[2]:-0}"
  printf "%03d%03d%03d" "$((10#$major))" "$((10#$minor))" "$((10#$patch))" 2>/dev/null || echo "0"
}

# Colours (disabled when stdout is not a terminal)
if [[ -t 1 ]]; then
  BOLD='\033[1m' DIM='\033[2m' GREEN='\033[32m' YELLOW='\033[33m' RED='\033[31m' CYAN='\033[36m' RESET='\033[0m'
else
  BOLD='' DIM='' GREEN='' YELLOW='' RED='' CYAN='' RESET=''
fi

info()  { echo -e "${GREEN}✔${RESET} $*"; }
warn()  { echo -e "${YELLOW}⚠${RESET} $*"; }
err()   { echo -e "${RED}✘${RESET} $*" >&2; }
step()  { echo -e "\n${BOLD}${CYAN}▸${RESET} ${BOLD}$*${RESET}"; }

# ── Pre-flight ───────────────────────────────────────────────────────────────

step "Daygle AI Camera - CLI Updater"
echo -e "${DIM}App directory: ${APP_DIR}${RESET}"

if ! git -C "${APP_DIR}" rev-parse --git-dir >/dev/null 2>&1; then
  err "${APP_DIR} is not a git repository. Cannot auto-update."
  exit 1
fi

# Warn if there are uncommitted changes
if ! git -C "${APP_DIR}" diff --quiet HEAD 2>/dev/null; then
  warn "You have uncommitted local changes."
  read -rp "Stash them before updating? [Y/n] " STASH_CONFIRM
  if [[ "${STASH_CONFIRM}" != "n" && "${STASH_CONFIRM}" != "N" ]]; then
    git -C "${APP_DIR}" stash push -m "auto-stash before CLI update"
    info "Changes stashed."
  else
    warn "Proceeding without stashing - git pull may fail."
  fi
fi

CURRENT_VERSION=$(current_version)
CURRENT_BRANCH=$(git -C "${APP_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
CURRENT_COMMIT=$(git -C "${APP_DIR}" rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo -e "${DIM}Version: ${CURRENT_VERSION}  Branch: ${CURRENT_BRANCH}  Commit: ${CURRENT_COMMIT}${RESET}"

# ── Check for updates ────────────────────────────────────────────────────────

step "Checking for updates..."

LATEST_JSON=$(curl -fsSL "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null) || {
  err "Could not reach GitHub API. Check your internet connection."
  exit 1
}

LATEST_VERSION=$(echo "${LATEST_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tag_name','').lstrip('v'))" 2>/dev/null || echo "")
RELEASE_NOTES=$(echo "${LATEST_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('body','No release notes available.'))" 2>/dev/null || echo "No release notes available.")
RELEASE_URL=$(echo "${LATEST_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('html_url',''))" 2>/dev/null || echo "")

if [[ -z "${LATEST_VERSION}" ]]; then
  err "Could not parse the latest version from GitHub."
  exit 1
fi

CURRENT_NUM=$(parse_semver "${CURRENT_VERSION}")
LATEST_NUM=$(parse_semver "${LATEST_VERSION}")

echo ""
echo -e "  ${BOLD}Current version:${RESET}  ${CURRENT_VERSION}"
echo -e "  ${BOLD}Latest version: ${RESET}  ${LATEST_VERSION}"

if [[ "${LATEST_NUM}" -le "${CURRENT_NUM}" ]]; then
  info "You are already running the latest version."
  exit 0
fi

echo ""
echo -e "${YELLOW}An update is available!${RESET}"
echo ""
echo -e "${BOLD}Release notes:${RESET}"
echo -e "${DIM}────────────────────────────────────────────────${RESET}"
# Truncate very long notes
echo "${RELEASE_NOTES}" | cut -c1-800
if [[ ${#RELEASE_NOTES} -gt 800 ]]; then
  echo ""
  echo -e "${DIM}... (truncated, see full notes at: ${RELEASE_URL})${RESET}"
fi
echo ""
echo -e "${DIM}────────────────────────────────────────────────${RESET}"

# ── Confirm ──────────────────────────────────────────────────────────────────

echo ""
read -rp "Apply this update now? [y/N] " CONFIRM
if [[ "${CONFIRM}" != "y" && "${CONFIRM}" != "Y" ]]; then
  echo "Update cancelled."
  exit 0
fi

# ── Apply update ─────────────────────────────────────────────────────────────

step "Applying update..."

CURRENT_COMMIT_BEFORE=$(git -C "${APP_DIR}" rev-parse --short HEAD 2>/dev/null || echo "unknown")

echo "Fetching latest changes from origin..."
git -C "${APP_DIR}" fetch origin

echo "Pulling latest changes on ${CURRENT_BRANCH}..."
git -C "${APP_DIR}" pull origin "${CURRENT_BRANCH}"

NEW_COMMIT=$(git -C "${APP_DIR}" rev-parse --short HEAD 2>/dev/null || echo "unknown")

if [[ "${CURRENT_COMMIT_BEFORE}" == "${NEW_COMMIT}" ]]; then
  warn "No new commits - you may already be up to date."
fi

echo -e "Updated: ${DIM}${CURRENT_COMMIT_BEFORE}${RESET} → ${GREEN}${NEW_COMMIT}${RESET}"

# Update VERSION from the latest git tag
LATEST_TAG=$(git -C "${APP_DIR}" describe --tags --abbrev=0 2>/dev/null || true)
if [[ -n "${LATEST_TAG}" ]]; then
  echo "${LATEST_TAG#v}" > "${APP_DIR}/VERSION"
  info "Updated VERSION to ${LATEST_TAG#v}"
fi

# ── Update Python dependencies ──────────────────────────────────────────────

step "Updating Python dependencies..."

if [[ -f "${APP_DIR}/.venv/bin/pip" ]]; then
  "${APP_DIR}/.venv/bin/pip" install --no-cache-dir -r "${APP_DIR}/requirements.txt"
elif [[ -f "${APP_DIR}/.venv/Scripts/pip.exe" ]]; then
  "${APP_DIR}/.venv/Scripts/pip.exe" install --no-cache-dir -r "${APP_DIR}/requirements.txt"
else
  pip install --no-cache-dir -r "${APP_DIR}/requirements.txt"
fi

info "Dependencies updated."

# ── Restart service ──────────────────────────────────────────────────────────

step "Checking for systemd service..."

if systemctl is-active --quiet daygle-ai-camera 2>/dev/null; then
  step "Restarting daygle-ai-camera service..."
  if sudo -n true 2>/dev/null; then
    sudo systemctl restart daygle-ai-camera
    info "Service restarted successfully."
  else
    warn "Passwordless sudo is not available. Please restart manually:"
    echo -e "  ${DIM}sudo systemctl restart daygle-ai-camera${RESET}"
  fi
elif systemctl is-enabled --quiet daygle-ai-camera 2>/dev/null; then
  warn "Service exists but is not running."
  if sudo -n true 2>/dev/null; then
    sudo systemctl start daygle-ai-camera
    info "Service started."
  else
    warn "Please start manually:"
    echo -e "  ${DIM}sudo systemctl start daygle-ai-camera${RESET}"
  fi
else
  warn "No systemd service found. Restart the application manually to apply changes."
fi

# ── Done ─────────────────────────────────────────────────────────────────────

NEW_VERSION=$(current_version)
echo ""
echo -e "${GREEN}${BOLD}=== Update complete! ===${RESET}"
echo -e "  ${BOLD}Version:${RESET} ${CURRENT_VERSION} → ${NEW_VERSION}"
echo -e "  ${BOLD}Commit: ${RESET} ${CURRENT_COMMIT} → ${NEW_COMMIT}"
echo ""
