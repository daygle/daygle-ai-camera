#!/usr/bin/env bash
# Daygle AI Camera - in-place updater
# Pulls the latest code from git and reinstalls Python dependencies.
# Service restart is handled separately by the caller (web API or manual).
set -euo pipefail

# The GUI invokes the updater version that was installed before the update.
# After git pull, re-exec the freshly pulled script so newly added migration
# steps (such as cloudflared/systemd provisioning) run on the first GUI update.
POST_PULL="${1:-}"

run_privileged() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    sudo -n "$@"
  else
    return 127
  fi
}

# ── Origin-URL allowlist ─────────────────────────────────────────────────────
# Refuse to fetch from any remote other than the canonical daygle/daygle-ai-camera
# repo. Without this guard a tampered .git/config (post any service-side breach
# or future path-traversal reintroduction) can redirect 'git pull origin …' to a
# malicious fork and achieve RCE-as-app-user on every Update click. The regex
# matches SSH (``git@github.com:user/repo[.git]``) and HTTPS
# (``https://github.com/user/repo[.git]``) forms; adds an optional ``.git``
# suffix so the bare-URL form is also accepted.
#
# Two scopes are enforced so a tampered EITHER side fails the same way:
#   (1) The invoking caller cwd - runs FIRST, before the ``cd ${APP_DIR}``,
#       so a tricked-admin running this script from a malicious fork's
#       checkout dir fails fast and never reaches the always-allowlisted
#       APP_DIR-side machinery. Without this check the original round-4 H1
#       guard below would silently pass because APP_DIR is always the
#       canonical repo, regardless of how dangerous the invoking cwd is.
#   (2) The APP_DIR itself (after the cd) - the original round-4 H1 guard,
#       catches a tampered APP_DIR .git/config from a service-side breach.
EXPECTED_REMOTE_REGEX='github\.com[:/]daygle/daygle-ai-camera(\.git)?$'
if [[ -d "${PWD}/.git" ]]; then
  CALLER_REMOTE="$(git -C "${PWD}" remote get-url origin 2>/dev/null || true)"
  if [[ -n "${CALLER_REMOTE}" ]] && ! printf '%s' "${CALLER_REMOTE}" | grep -Eq "${EXPECTED_REMOTE_REGEX}"; then
    echo "ERROR: refusing to update from non-allowlisted origin remote." >&2
    echo "  Caller cwd: '${PWD}'" >&2
    echo "  Caller remote: '${CALLER_REMOTE:-<empty>}'" >&2
    echo "  Expected pattern: ${EXPECTED_REMOTE_REGEX}" >&2
    echo "  Fix: 'git remote set-url origin https://github.com/daygle/daygle-ai-camera.git'" >&2
    exit 1
  fi
fi

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

if [[ "${CURRENT_BRANCH}" == "HEAD" ]]; then
  echo "Error: Repository is in a detached HEAD state. Check out a branch before updating." >&2
  exit 1
fi

# APP_DIR-side allowlist (round-4 H1, kept). ``EXPECTED_REMOTE_REGEX`` is
# defined once at the top of this script (shared with the caller-cwd check
# above); this block reuses it after the ``cd ${APP_DIR}`` so a tampered
# APP_DIR .git/config is still refused by the same regex.
CURRENT_REMOTE="$(git remote get-url origin 2>/dev/null || true)"
if [[ -z "${CURRENT_REMOTE}" ]] || ! printf '%s' "${CURRENT_REMOTE}" | grep -Eq "${EXPECTED_REMOTE_REGEX}"; then
  echo "ERROR: refusing to update from non-allowlisted origin remote." >&2
  echo "  Current remote: '${CURRENT_REMOTE:-<empty>}'" >&2
  echo "  Expected pattern: ${EXPECTED_REMOTE_REGEX}" >&2
  echo "  Fix: 'git remote set-url origin https://github.com/daygle/daygle-ai-camera.git'" >&2
  exit 1
fi
echo "Origin remote verified: ${CURRENT_REMOTE}"

if [[ "${POST_PULL}" != "--post-pull" ]]; then
  echo "Fetching latest changes from origin..."
  git fetch origin

  echo "Pulling latest changes on ${CURRENT_BRANCH}..."
  git pull origin "${CURRENT_BRANCH}"

  # The currently running script may be the pre-update version. Re-enter the
  # freshly pulled script so its complete post-update migration path executes.
  exec bash "${APP_DIR}/scripts/update.sh" --post-pull
fi

NEW_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo "Now at commit: ${NEW_COMMIT}"
echo ""
echo "Updating Python dependencies..."
if [[ -f "${APP_DIR}/.venv/bin/python" ]]; then
  "${APP_DIR}/scripts/install_python_deps.sh" "${APP_DIR}/.venv/bin/python" "${APP_DIR}/requirements.txt"
elif [[ -f "${APP_DIR}/.venv/Scripts/python.exe" ]]; then
  "${APP_DIR}/scripts/install_python_deps.sh" "${APP_DIR}/.venv/Scripts/python.exe" "${APP_DIR}/requirements.txt"
else
  echo "Error: Virtual environment not found at ${APP_DIR}/.venv. Run the installer first." >&2
  exit 1
fi

echo ""
echo "Installing optional Cloudflare Tunnel runtime..."
if run_privileged env DAYGLE_CLOUDFLARED_PATH=/usr/local/bin/cloudflared \
    "${APP_DIR}/scripts/install_cloudflared.sh"; then
  echo "cloudflared installed system-wide."
else
  echo "INFO: installing cloudflared into the application virtual environment..."
  DAYGLE_CLOUDFLARED_PATH="${APP_DIR}/.venv/bin/cloudflared" \
    "${APP_DIR}/scripts/install_cloudflared.sh"
fi

# Existing installations used a direct ``uvicorn app.main:app`` command.
# Install a narrow drop-in instead of replacing the administrator's unit,
# preserving its paths, environment, user/group, hardening, and other drop-ins.
SYSTEMD_DIR="/etc/systemd/system"
DROPIN_DIR="${SYSTEMD_DIR}/daygle-ai-camera.service.d"
DROPIN_FILE="${DROPIN_DIR}/20-daygle-launcher.conf"
if command -v systemctl >/dev/null 2>&1 && run_privileged systemctl list-unit-files daygle-ai-camera.service >/dev/null 2>&1; then
  DROPIN_TEMP="$(mktemp)"
  cat > "${DROPIN_TEMP}" <<EOF
[Service]
ExecStart=
ExecStart=${APP_DIR}/.venv/bin/python -m app.server
EOF
  if run_privileged mkdir -p "${DROPIN_DIR}" && run_privileged install -m 0644 "${DROPIN_TEMP}" "${DROPIN_FILE}" && run_privileged systemctl daemon-reload; then
    echo "Systemd launcher migrated to app.server."
  else
    echo "WARNING: unable to migrate the systemd launcher; tunnel mode may not be loopback-only." >&2
  fi
  rm -f "${DROPIN_TEMP}"
else
  echo "WARNING: systemd launcher migration requires root or passwordless sudo; tunnel mode may not be loopback-only." >&2
fi

echo ""
echo "=== Update complete. Restart the service to apply changes. ==="
