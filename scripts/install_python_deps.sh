#!/usr/bin/env bash
# Daygle AI Camera - Python dependency installer
#
# Installs the project's Python dependencies into a virtual environment
# ``$1`` from ``$2`` (the requirements file). Selects CPU or GPU variants
# of ONNX Runtime via ``DAYGLE_ONNXRUNTIME_VARIANT`` (``auto`` [default],
# ``cpu``, or ``gpu``). ``auto`` selects GPU only when ``nvidia-smi`` can
# successfully enumerate an NVIDIA device; use an explicit value to override.

# This script deliberately does not install NVIDIA drivers or CUDA system
# libraries. Install and verify those at the OS level first, then use the GPU
# variant below. ONNX Runtime GPU includes the CPU execution provider as a
# fallback, while the CPU and GPU pip wheels must not coexist in one venv.
#
# For reproducible deployments, scripts/lock_python_deps.sh can produce
# variant-specific requirements.cpu.lock.txt and requirements.gpu.lock.txt.
# The matching lock is preferred; an old generic requirements.lock.txt is
# accepted only for CPU installs for backwards compatibility.
set -euo pipefail

VENV_BIN="${1:?usage: install_python_deps.sh VENV_BIN REQUIREMENTS_FILE}"
REQUIREMENTS_FILE="${2:?usage: install_python_deps.sh VENV_BIN REQUIREMENTS_FILE}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Keep the default in the CUDA 11/12-era ORT line commonly used with Pascal;
# operators may override this after validating their driver/runtime matrix.
GPU_REQUIREMENT="${DAYGLE_ONNXRUNTIME_GPU_REQUIREMENT:-onnxruntime-gpu>=1.18,<1.21}"
GPU_REQUIREMENT_SUFFIX="${GPU_REQUIREMENT#onnxruntime-gpu}"
if [[ "${GPU_REQUIREMENT}" == "${GPU_REQUIREMENT_SUFFIX}" || "${GPU_REQUIREMENT_SUFFIX}" == *$'\n'* || "${GPU_REQUIREMENT_SUFFIX}" == *$'\r'* ]]; then
  echo "ERROR: DAYGLE_ONNXRUNTIME_GPU_REQUIREMENT must be one onnxruntime-gpu requirement with an optional version specifier." >&2
  exit 1
fi
case "${GPU_REQUIREMENT_SUFFIX}" in
  ""|[[:space:]]*|[\<\>\=\!\~]*) ;;
  *)
    echo "ERROR: DAYGLE_ONNXRUNTIME_GPU_REQUIREMENT must name only onnxruntime-gpu." >&2
    exit 1
    ;;
esac
DEFAULT_VARIANT='auto'
VARIANT="${DAYGLE_ONNXRUNTIME_VARIANT:-${DEFAULT_VARIANT}}"
case "${VARIANT}" in
  auto)
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
      VARIANT='gpu'
    else
      VARIANT='cpu'
    fi
    ;;
  cpu|gpu) ;;
  *)
    echo "ERROR: DAYGLE_ONNXRUNTIME_VARIANT must be 'auto', 'cpu', or 'gpu' (got '${VARIANT}')." >&2
    exit 1
    ;;
esac
echo "Resolved ONNX Runtime dependency variant: ${VARIANT}"

# Large wheels (notably the ~300 MB onnxruntime-gpu build, plus the CUDA
# runtime packages torch pulls in) are prone to mid-download connection
# timeouts on slow or flaky links. pip reports that as "incomplete-download"
# and aborts the whole install after discarding the partial file. Give every
# pip install a longer socket timeout and more retries, and -- where pip
# supports it (>= 25.1) -- enable download resumption so an interrupted large
# download continues from where it stopped instead of restarting from zero.
# All three knobs are overridable via the environment.
PIP_NET_OPTS=(--retries "${DAYGLE_PIP_RETRIES:-5}" --timeout "${DAYGLE_PIP_TIMEOUT:-120}")
if "${VENV_BIN}" -m pip install --help 2>/dev/null | grep -q -- '--resume-retries'; then
  PIP_NET_OPTS+=(--resume-retries "${DAYGLE_PIP_RESUME_RETRIES:-5}")
fi

_validate_lock() {
  local lock_file="$1"
  local cpu_count gpu_count
  cpu_count="$(awk 'tolower($0) ~ /^[[:space:]]*onnxruntime([[:space:]]|[<>=!~;]|$)/ { count++ } END { print count + 0 }' "${lock_file}")"
  gpu_count="$(awk 'tolower($0) ~ /^[[:space:]]*onnxruntime-gpu([[:space:]]|[<>=!~;]|$)/ { count++ } END { print count + 0 }' "${lock_file}")"
  if [[ "${VARIANT}" == 'gpu' ]]; then
    if [[ "${gpu_count}" -ne 1 || "${cpu_count}" -ne 0 ]]; then
      echo "ERROR: ${lock_file} does not contain exactly the GPU ONNX Runtime package." >&2
      echo "       Refusing to install an ambiguous dependency lock." >&2
      exit 1
    fi
  elif [[ "${cpu_count}" -ne 1 || "${gpu_count}" -ne 0 ]]; then
    echo "ERROR: ${lock_file} does not contain exactly the CPU ONNX Runtime package." >&2
    echo "       Refusing to install an ambiguous dependency lock." >&2
    exit 1
  fi
}

_verify_runtime() {
  # This confirms provider registration after installation. The first actual
  # model session will still be the definitive CUDA initialization test.
  if [[ "${VARIANT}" == 'gpu' ]] && ! "${VENV_BIN}" -c 'import sys, onnxruntime as ort; sys.exit(0 if "CUDAExecutionProvider" in ort.get_available_providers() else 1)'; then
    echo "ERROR: onnxruntime-gpu installed, but CUDAExecutionProvider is unavailable." >&2
    echo "       Verify the NVIDIA driver and CUDA/cuDNN compatibility, or rerun with DAYGLE_ONNXRUNTIME_VARIANT=cpu." >&2
    exit 1
  fi
}

# A lock is variant-specific. Never install a generic CPU lock for a GPU
# deployment: that was the source of clean installs silently receiving the
# CPU-only onnxruntime wheel. A legacy generic lock remains usable only for
# an explicitly/automatically selected CPU deployment.
LOCK_FILE="${APP_DIR}/requirements.${VARIANT}.lock.txt"
if [[ ! -f "${LOCK_FILE}" && "${VARIANT}" == 'cpu' && -f "${APP_DIR}/requirements.lock.txt" ]]; then
  LOCK_FILE="${APP_DIR}/requirements.lock.txt"
fi
if [[ -f "${LOCK_FILE}" ]]; then
  _validate_lock "${LOCK_FILE}"
  echo "Installing from ${LOCK_FILE}."
  # Do not remove the existing ORT wheel until the lock has been validated.
  "${VENV_BIN}" -m pip uninstall -y onnxruntime onnxruntime-gpu >/dev/null 2>&1 || true
  # ai-edge-litert currently declares backports-strenum unconditionally,
  # although that backport's metadata incorrectly excludes Python 3.11+.
  # LiteRT itself imports and runs on Python 3.13; ignore only this stale
  # Requires-Python metadata while retaining hash verification for every
  # downloaded artifact. Remove this compatibility flag when LiteRT fixes its
  # dependency metadata upstream.
  PIP_PYTHON_COMPAT_OPTS=()
  if grep -q '^backports-strenum==' "${LOCK_FILE}"; then
    PIP_PYTHON_COMPAT_OPTS+=(--ignore-requires-python)
  fi
  if grep -q -- '--hash=sha256:' "${LOCK_FILE}"; then
    "${VENV_BIN}" -m pip install "${PIP_NET_OPTS[@]}" "${PIP_PYTHON_COMPAT_OPTS[@]}" --no-cache-dir --require-hashes -r "${LOCK_FILE}"
  else
    echo "WARNING: ${LOCK_FILE} has no hashes; installing its pinned constraints without --require-hashes." >&2
    "${VENV_BIN}" -m pip install "${PIP_NET_OPTS[@]}" "${PIP_PYTHON_COMPAT_OPTS[@]}" --no-cache-dir -r "${LOCK_FILE}"
  fi
  _verify_runtime
  exit 0
fi

# No lock is available. Remove the old ORT wheel before resolving the
# replacement; the runtime verification below prevents a silent CPU install
# on a requested GPU deployment.
"${VENV_BIN}" -m pip uninstall -y onnxruntime onnxruntime-gpu >/dev/null 2>&1 || true

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
REQUIREMENTS_VARIANT="${WORK_DIR}/requirements-${VARIANT}.txt"

# Keep torch/torchvision out of this filter because they are not runtime
# requirements of the detector. Ultralytics may install them as export-time
# dependencies; ONNX Runtime is the component that controls inference here.
if [[ "${VARIANT}" == 'gpu' ]]; then
  awk '
    /^[[:space:]]*($|#)/ { print; next }
    tolower($0) ~ "^[[:space:]]*onnxruntime(-gpu)?([[:space:]]|[<>=!~;]|$)" { next }
    { print }
  ' "${REQUIREMENTS_FILE}" > "${REQUIREMENTS_VARIANT}"
  printf '\n# Selected by install_python_deps.sh for NVIDIA inference.\n%s\n' "${GPU_REQUIREMENT}" >> "${REQUIREMENTS_VARIANT}"
else
  awk '
    /^[[:space:]]*($|#)/ { print; next }
    tolower($0) ~ "^[[:space:]]*onnxruntime-gpu([[:space:]]|[<>=!~;]|$)" { next }
    { print }
  ' "${REQUIREMENTS_FILE}" > "${REQUIREMENTS_VARIANT}"
fi

# Same LiteRT/backports-strenum metadata workaround as above, applied to the
# no-lock resolution path used by deployments without a committed variant
# lock (e.g. GPU hosts: only requirements.cpu.lock.txt is committed).
PIP_PYTHON_COMPAT_OPTS=()
if grep -q '^ai-edge-litert' "${REQUIREMENTS_VARIANT}"; then
  PIP_PYTHON_COMPAT_OPTS+=(--ignore-requires-python)
fi

"${VENV_BIN}" -m pip install "${PIP_NET_OPTS[@]}" "${PIP_PYTHON_COMPAT_OPTS[@]}" --no-cache-dir -r "${REQUIREMENTS_VARIANT}"
_verify_runtime
