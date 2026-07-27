#!/usr/bin/env bash
# Daygle AI Camera - in-place updater
# Pulls the latest code from git and reinstalls Python dependencies.
# Service restart is handled separately by the caller (web API or manual).
set -euo pipefail

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
#   (1) The invoking caller cwd — runs FIRST, before the ``cd ${APP_DIR}``,
#       so a tricked-admin running this script from a malicious fork's
#       checkout dir fails fast and never reaches the always-allowlisted
#       APP_DIR-side machinery. Without this check the original round-4 H1
#       guard below would silently pass because APP_DIR is always the
#       canonical repo, regardless of how dangerous the invoking cwd is.
#   (2) The APP_DIR itself (after the cd) — the original round-4 H1 guard,
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

echo "Fetching latest changes from origin..."
git fetch origin

echo "Pulling latest changes on ${CURRENT_BRANCH}..."
git pull origin "${CURRENT_BRANCH}"

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
echo "=== Update complete. Restart the service to apply changes. ==="
