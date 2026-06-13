#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${1:-python3}"
REQUIREMENTS_FILE="${2:-requirements.txt}"
TORCH_VARIANT="${DAYGLE_TORCH_VARIANT:-cpu}"
ONNXRUNTIME_VARIANT="${DAYGLE_ONNXRUNTIME_VARIANT:-cpu}"
PIP_NO_CACHE_DIR_DEFAULT="${DAYGLE_PIP_NO_CACHE_DIR:-1}"

if [[ ! -f "${REQUIREMENTS_FILE}" ]]; then
  echo "Requirements file not found: ${REQUIREMENTS_FILE}" >&2
  exit 1
fi

PIP_INSTALL_OPTS=()
if [[ "${PIP_NO_CACHE_DIR_DEFAULT}" != "0" ]]; then
  PIP_INSTALL_OPTS+=(--no-cache-dir)
fi

"${PYTHON_BIN}" -m pip install "${PIP_INSTALL_OPTS[@]}" --upgrade pip wheel

REQUIREMENTS_WITHOUT_TORCH="$(mktemp)"
cleanup() {
  rm -f "${REQUIREMENTS_WITHOUT_TORCH}"
}
trap cleanup EXIT

# Ultralytics depends on torch/torchvision. Installing CPU-only PyTorch first
# prevents pip from resolving the default Linux CUDA wheels, which can download
# multiple large nvidia-* packages and temporarily exhaust small /tmp, cache, or
# container overlay filesystems.
case "${TORCH_VARIANT}" in
  cpu)
    "${PYTHON_BIN}" -m pip install "${PIP_INSTALL_OPTS[@]}" \
      --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision
    ;;
  default)
    echo "Using pip's default PyTorch resolution because DAYGLE_TORCH_VARIANT=default."
    ;;
  skip)
    echo "Skipping PyTorch preinstall because DAYGLE_TORCH_VARIANT=skip."
    ;;
  *)
    echo "Unsupported DAYGLE_TORCH_VARIANT='${TORCH_VARIANT}'. Use cpu, default, or skip." >&2
    exit 1
    ;;
esac

# Keep any torch/torchvision pins from the main requirements file from
# overriding the explicit selection above.
#
# When DAYGLE_ONNXRUNTIME_VARIANT=gpu we also strip onnxruntime from the
# requirements so the GPU-capable package installed below is not downgraded.
STRIP_PATTERN='(torch|torchvision)'
if [[ "${ONNXRUNTIME_VARIANT}" == "gpu" ]]; then
  STRIP_PATTERN='(torch|torchvision|onnxruntime)'
fi

awk -v pat="${STRIP_PATTERN}" '
  /^[[:space:]]*($|#)/ { print; next }
  tolower($0) ~ "^[[:space:]]*" pat "([[:space:]]|[<>=!~;[]|$)" { next }
  { print }
' "${REQUIREMENTS_FILE}" > "${REQUIREMENTS_WITHOUT_TORCH}"

# Install the appropriate onnxruntime package.
case "${ONNXRUNTIME_VARIANT}" in
  gpu)
    echo "Installing onnxruntime-gpu (CUDA) because DAYGLE_ONNXRUNTIME_VARIANT=gpu."
    "${PYTHON_BIN}" -m pip install "${PIP_INSTALL_OPTS[@]}" "onnxruntime-gpu>=1.18,<2.0"
    ;;
  cpu)
    # onnxruntime (CPU) is included in requirements.txt; nothing extra needed.
    ;;
  *)
    echo "Unsupported DAYGLE_ONNXRUNTIME_VARIANT='${ONNXRUNTIME_VARIANT}'. Use cpu or gpu." >&2
    exit 1
    ;;
esac

"${PYTHON_BIN}" -m pip install "${PIP_INSTALL_OPTS[@]}" -r "${REQUIREMENTS_WITHOUT_TORCH}"
