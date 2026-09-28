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
# For reproducible CPU deployments, scripts/lock_python_deps.sh produces the
# committed requirements.cpu.lock.txt. A GPU lock can also be generated on
# demand, but this repository resolves GPU base dependencies from
# requirements.txt and installs the separate CUDA userspace requirements file.
# The matching lock is preferred; an old generic requirements.lock.txt remains
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

# onnxruntime and onnxruntime-gpu install the same ``onnxruntime`` module, so the
# OTHER variant must go before installing this one. The selected variant is
# left in place: pip upgrades it in place, whereas uninstalling it first meant
# a failed or interrupted install (network drop, the web updater's timeout)
# left the application with no ONNX Runtime at all.
_remove_conflicting_runtime() {
  if [[ "${VARIANT}" == 'gpu' ]]; then
    "${VENV_BIN}" -m pip uninstall -y onnxruntime >/dev/null 2>&1 || true
  else
    "${VENV_BIN}" -m pip uninstall -y onnxruntime-gpu >/dev/null 2>&1 || true
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

# Pascal (e.g. Tesla P4) hosts run on the pinned CUDA 12.4 / cuDNN 9.1 wheels in
# requirements-gpu-pascal.txt (see docs/tesla-p4-gpu-setup.md). A dependency
# update can silently break them: torch (pulled in by ultralytics) brings CUDA 13
# wheels, and nvidia-cudnn-cu13 installs libcudnn.so.9 into the SAME
# nvidia/cudnn/lib directory as nvidia-cudnn-cu12, overwriting the Pascal build
# while pip still reports 9.1.0.70 installed. Detection then fails on every
# frame with CUDNN_STATUS_EXECUTION_FAILED_CUDART. After each install this:
#   1. removes the CUDA 13 wheels the runbook lists as colliding;
#   2. checks every pinned wheel's shared libraries against pip's own RECORD
#      hashes and force-reinstalls (--no-deps) only the ones that changed;
#   3. rewrites the ld.so.conf entry with the cu13 exclusion and runs ldconfig.
# Runs only for GPU installs on a compute-capability < 7.0 card that already
# has the pinned stack. DAYGLE_PASCAL_CUDA_REPAIR=1 forces it, =0 disables it.
PASCAL_REQUIREMENTS="${APP_DIR}/requirements-gpu-pascal.txt"
PASCAL_CUDA13_CONFLICTS=(nvidia-cudnn-cu13 nvidia-cusparselt-cu13 nvidia-nccl-cu13 nvidia-nvshmem-cu13)
CUDA_LDCONF="${DAYGLE_CUDA_LDCONF:-/etc/ld.so.conf.d/daygle-cuda.conf}"

_is_pascal_gpu() {
  local cap
  cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n 1 | tr -d '[:space:]')" || return 1
  [[ "${cap}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || return 1
  (( ${cap%%.*} < 7 ))
}

# torch is only used to export models (Ultralytics), never for detection, and
# current CUDA torch wheels no longer run on Pascal at all. Their CUDA 13
# dependencies are what overwrite the pinned cuDNN, so on Pascal hosts swap
# torch/torchvision for the CPU-only build of the same versions: no NVIDIA
# wheels, ~200 MB instead of ~3 GB, and exports run on the CPU. A later
# ``pip install -r requirements.txt`` keeps the +cpu build because it already
# satisfies ultralytics' torch requirement. Failures only warn: detection does
# not depend on torch.
TORCH_CPU_INDEX="${DAYGLE_TORCH_CPU_INDEX:-https://download.pytorch.org/whl/cpu}"

_use_cpu_torch() {
  local versions torch_version vision_version
  versions="$("${VENV_BIN}" -c '
from importlib import metadata
out = []
for name in ("torch", "torchvision"):
    try:
        out.append(metadata.version(name))
    except metadata.PackageNotFoundError:
        out.append("")
print(" ".join(v or "-" for v in out))
' 2>/dev/null)" || return 0
  read -r torch_version vision_version <<< "${versions}"
  [[ -n "${torch_version}" && "${torch_version}" != '-' ]] || return 0
  [[ "${torch_version}" == *+cpu ]] && return 0
  local specs=("torch==${torch_version%%+*}+cpu")
  if [[ -n "${vision_version}" && "${vision_version}" != '-' ]]; then
    specs+=("torchvision==${vision_version%%+*}+cpu")
  fi
  echo "Switching PyTorch to its CPU-only build (${specs[*]}) so CUDA 13 wheels cannot replace the Pascal libraries."
  if ! "${VENV_BIN}" -m pip install "${PIP_NET_OPTS[@]}" --no-cache-dir --no-deps --index-url "${TORCH_CPU_INDEX}" "${specs[@]}"; then
    echo "WARNING: could not install the CPU-only PyTorch build; model export may need it (detection is unaffected)." >&2
  fi
}

_repair_pascal_cuda_stack() {
  local mode="${DAYGLE_PASCAL_CUDA_REPAIR:-auto}"
  [[ "${VARIANT}" == 'gpu' && -f "${PASCAL_REQUIREMENTS}" ]] || return 0
  case "${mode}" in
    0|false|no|off) return 0 ;;
    1|true|yes|on) ;;
    *) _is_pascal_gpu || return 0 ;;
  esac
  local pins
  pins="$(awk 'tolower($0) ~ /^[[:space:]]*nvidia-[a-z0-9-]+-cu12==[^[:space:]]+/ { print $1 }' "${PASCAL_REQUIREMENTS}")"
  [[ -n "${pins}" ]] || return 0

  _use_cpu_torch

  local conflict
  for conflict in "${PASCAL_CUDA13_CONFLICTS[@]}"; do
    if "${VENV_BIN}" -m pip show "${conflict}" >/dev/null 2>&1; then
      echo "Removing ${conflict}: it overwrites the Pascal CUDA 12 libraries."
      "${VENV_BIN}" -m pip uninstall -y "${conflict}" >/dev/null 2>&1 || true
    fi
  done

  # Prints the pins whose installed version or library files no longer match
  # pip's RECORD. Prints nothing when none of the pins is installed (the
  # Pascal runbook was never applied here), so nothing is downloaded.
  local broken
  # shellcheck disable=SC2086
  broken="$("${VENV_BIN}" -c '
import base64, hashlib, sys
from importlib import metadata
from pathlib import Path
installed, broken = 0, []
for pin in sys.argv[1:]:
    name, _, version = pin.partition("==")
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        broken.append(pin)
        continue
    installed += 1
    if dist.version != version:
        broken.append(pin)
        continue
    for entry in dist.files or []:
        text = str(entry)
        if not entry.hash or entry.hash.mode != "sha256" or ".so" not in Path(text).name:
            continue
        path = Path(dist.locate_file(entry))
        try:
            if entry.size is not None and path.stat().st_size != int(entry.size):
                raise ValueError
            digest = hashlib.sha256(path.read_bytes()).digest()
        except (OSError, ValueError):
            broken.append(pin)
            break
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != entry.hash.value:
            broken.append(pin)
            break
if installed:
    print("\n".join(broken))
' ${pins})" || broken=''
  if [[ -n "${broken}" ]]; then
    echo "Restoring Pascal CUDA 12 wheels overwritten by the update:" ${broken}
    # shellcheck disable=SC2086
    "${VENV_BIN}" -m pip install "${PIP_NET_OPTS[@]}" --no-cache-dir --no-deps --force-reinstall ${broken}
  fi

  if [[ -f "${CUDA_LDCONF}" && -w "${CUDA_LDCONF}" ]]; then
    local site_packages dir name entries=''
    site_packages="$("${VENV_BIN}" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])" 2>/dev/null)" || site_packages=''
    if [[ -n "${site_packages}" ]]; then
      for dir in "${site_packages}"/nvidia/*/lib; do
        [[ -d "${dir}" ]] || continue
        # Match the nvidia/<name> component only, not the whole install path.
        name="${dir#"${site_packages}/nvidia/"}"
        case "${name%%/*}" in *cu13*) continue ;; esac
        entries+="${dir}"$'\n'
      done
      if [[ -n "${entries}" ]]; then
        printf '%s' "${entries}" > "${CUDA_LDCONF}"
        if [[ "${CUDA_LDCONF}" == /etc/* ]] && command -v ldconfig >/dev/null 2>&1; then
          ldconfig || true
        fi
      fi
    fi
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
  _remove_conflicting_runtime
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
  _repair_pascal_cuda_stack
  _verify_runtime
  exit 0
fi

# No lock is available. Remove the old ORT wheel before resolving the
# replacement; the runtime verification below prevents a silent CPU install
# on a requested GPU deployment.
_remove_conflicting_runtime

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
_repair_pascal_cuda_stack
_verify_runtime
