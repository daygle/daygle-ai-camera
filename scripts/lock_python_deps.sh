#!/usr/bin/env bash
# Daygle AI Camera - Python dependency lock generator (developer-side)
#
# Produces a variant-specific hash-locked derivative of the canonical
# requirements file: requirements.cpu.lock.txt or requirements.gpu.lock.txt.
# This prevents a GPU host from accidentally installing a CPU-only ORT wheel.
#
# WHY THIS IS A SEPARATE SCRIPT:
# The runtime install path (``scripts/install_python_deps.sh``) selects a
# CPU or GPU ONNX Runtime wheel. A single lock cannot safely represent both
# variants, so this script creates one lock per requested variant.
#
# USAGE:
#     ./scripts/lock_python_deps.sh         # auto-detect; normally CPU on dev hosts
#     DAYGLE_ONNXRUNTIME_VARIANT=cpu ./scripts/lock_python_deps.sh
#     DAYGLE_ONNXRUNTIME_VARIANT=gpu ./scripts/lock_python_deps.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQUIREMENTS_FILE="${APP_DIR}/requirements.txt"
GPU_REQUIREMENT="${DAYGLE_ONNXRUNTIME_GPU_REQUIREMENT:-onnxruntime-gpu>=1.18,<1.21}"
REQUESTED_VARIANT="${DAYGLE_ONNXRUNTIME_VARIANT:-auto}"
case "${REQUESTED_VARIANT}" in
  auto)
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
      VARIANT='gpu'
    else
      VARIANT='cpu'
    fi
    ;;
  cpu|gpu) VARIANT="${REQUESTED_VARIANT}" ;;
  *)
    echo "ERROR: DAYGLE_ONNXRUNTIME_VARIANT must be 'auto', 'cpu', or 'gpu'." >&2
    exit 1
    ;;
esac
LOCK_FILE="${APP_DIR}/requirements.${VARIANT}.lock.txt"
TMP_OUTPUT="$(mktemp)"
TMP_REQUIREMENTS="$(mktemp)"
trap 'rm -f "${TMP_OUTPUT}" "${TMP_REQUIREMENTS}"' EXIT

if [[ ! -f "${REQUIREMENTS_FILE}" ]]; then
  echo "ERROR: ${REQUIREMENTS_FILE} not found." >&2
  exit 1
fi

# Build the selected input first so pip-compile resolves the correct ORT
# distribution. The GPU input replaces the base CPU ORT requirement.
if [[ "${VARIANT}" == 'gpu' ]]; then
  awk '
    /^[[:space:]]*($|#)/ { print; next }
    tolower($0) ~ "^[[:space:]]*onnxruntime(-gpu)?([[:space:]]|[<>=!~;]|$)" { next }
    { print }
  ' "${REQUIREMENTS_FILE}" > "${TMP_REQUIREMENTS}"
  printf '\n%s\n' "${GPU_REQUIREMENT}" >> "${TMP_REQUIREMENTS}"
else
  awk '
    /^[[:space:]]*($|#)/ { print; next }
    tolower($0) ~ "^[[:space:]]*onnxruntime-gpu([[:space:]]|[<>=!~;]|$)" { next }
    { print }
  ' "${REQUIREMENTS_FILE}" > "${TMP_REQUIREMENTS}"
fi

# Generate the hash-annotated lock file via pip-compile (vendored under
# pip-tools). If pip-compile is unavailable, preserve the selected variant
# constraints as a non-hash fallback; the runtime installer will not pretend
# that this fallback is hash-verified.
if command -v pip-compile >/dev/null 2>&1; then
  echo "Generating ${LOCK_FILE} for ${VARIANT} via pip-compile --generate-hashes ..."
  pip-compile \
    --quiet \
    --generate-hashes \
    --output-file "${TMP_OUTPUT}" \
    "${TMP_REQUIREMENTS}"
else
  echo "pip-compile not on PATH; falling back to constraint-only rewrite." >&2
  echo "  Install with: 'pip install pip-tools' then re-run this script." >&2
  echo "  Until pip-compile is available, ${LOCK_FILE} will contain the selected" >&2
  echo "  constraints without hashes and will be installed without --require-hashes." >&2
  cp "${TMP_REQUIREMENTS}" "${TMP_OUTPUT}"
fi

mv "${TMP_OUTPUT}" "${LOCK_FILE}"
echo "Wrote ${LOCK_FILE}"
