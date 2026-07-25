#!/usr/bin/env bash
# Daygle AI Camera - Python dependency installer
#
# Installs the project's Python dependencies into a virtual environment
# ``$1`` from ``$2`` (the requirements file). Selects CPU or GPU variants
# of torch / onnxruntime via the ``DAYGLE_ONNXRUNTIME_VARIANT`` env
# variable (``cpu`` [default] or ``gpu``).
#
# ROUND-5 (N3) NOTE:
# ``pip install --require-hashes`` would be the canonical way to enforce
# bit-for-bit reproducibility of the Python dependency tree, but it is
# INCOMPATIBLE with the awk-based GPU/CPU variant selection below:
# ``--require-hashes`` requires an UNBROKEN chain of ``--hash=sha256:``
# annotations per line, and the awk filter drops lines before pip sees
# them, breaking the chain. The fix lives in ``scripts/lock_python_deps.sh``,
# which produces ``requirements.lock.txt`` -- a hash-pinned derivative of
# ``requirements.txt`` -- on a known-good reference platform. When
# ``requirements.lock.txt`` is present in the project root, this script
# installs from it with ``--require-hashes``; otherwise it falls back to
# the legacy non-hash install with the awk filter.
set -euo pipefail

VENV_BIN="${1:?usage: install_python_deps.sh VENV_BIN REQUIREMENTS_FILE}"
REQUIREMENTS_FILE="${2:?usage: install_python_deps.sh VENV_BIN REQUIREMENTS_FILE}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${APP_DIR}/requirements.lock.txt"

DEFAULT_VARIANT='cpu'
VARIANT="${DAYGLE_ONNXRUNTIME_VARIANT:-${DEFAULT_VARIANT}}"
case "${VARIANT}" in
  cpu|gpu) ;;
  *)
    echo "ERROR: DAYGLE_ONNXRUNTIME_VARIANT must be 'cpu' or 'gpu' (got '${VARIANT}')." >&2
    exit 1
    ;;
esac

# Prefer the hash-locked lock file when present (round-5 N3 fix). On
# systems where the lock has been vendored (developer ran lock_python_deps.sh
# on a reference platform) this is the strict-install path.
if [[ -f "${LOCK_FILE}" ]]; then
  echo "Installing from hash-locked ${LOCK_FILE} (variants pre-pinned by developer)."
  "${VENV_BIN}/pip" install --no-cache-dir --require-hashes -r "${LOCK_FILE}"
  exit 0
fi

# Fallback: legacy non-hash install with the GPU/CPU variant filter.
echo "Lock file not present, falling back to legacy filtered install (variant=${VARIANT})."
STRIP_PATTERN='(torch|torchvision)'
if [[ "${VARIANT}" == 'gpu' ]]; then
  STRIP_PATTERN='(torch|torchvision|onnxruntime)'
fi
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
REQUIREMENTS_WITHOUT_TORCH="${WORK_DIR}/requirements-stripped.txt"
awk -v pat="${STRIP_PATTERN}" '
  /^[[:space:]]*($|#)/ { print; next }
  tolower($0) ~ "^[[:space:]]*" pat "([[:space:]]|[<>=!~;[]|$)" { next }
  { print }
' "${REQUIREMENTS_FILE}" > "${REQUIREMENTS_WITHOUT_TORCH}"

"${VENV_BIN}/pip" install --no-cache-dir -r "${REQUIREMENTS_WITHOUT_TORCH}"
