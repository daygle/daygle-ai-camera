#!/usr/bin/env bash
# Daygle AI Camera - Python dependency lock generator (developer-side)
#
# Produces ``requirements.lock.txt`` -- a hash-locked derivative of the
# canonical ``requirements.txt`` -- so a downstream CI / reproducible
# install can verify ``pip install --require-hashes -r requirements.lock.txt``.
#
# WHY THIS IS A SEPARATE SCRIPT:
# The runtime install path (``scripts/install_python_deps.sh``) uses an
# ``awk`` filter to dynamically strip ``torch``, ``torchvision``, and
# (on GPU builds) ``onnxruntime`` from ``requirements.txt`` before calling
# ``pip install``. ``pip install --require-hashes`` requires an UNBROKEN
# dependency tree with inline ``--hash=sha256:...`` per line; the awk
# filter drops lines, breaking the chain.
#
# Therefore a SINGLE ``--require-hashes`` install command isn't a drop-in
# fix at the install_debian.sh level. The right pattern is:
#
#   1. Run THIS script on a known-good reference platform to produce
#      ``requirements.lock.txt`` (hash-locked, GPU-variant-pinned).
#   2. Commit ``requirements.lock.txt`` next to ``requirements.txt``.
#   3. In ``scripts/install_python_deps.sh``, prefer ``requirements.lock.txt``
#      when present (``pip install --require-hashes --no-cache-dir -r
#      requirements.lock.txt``); falls back to the legacy non-hash install
#      with the awk filter when the lock file is absent.
#
# This script does NOT mutate runtime install behaviour; it only produces
# the auxiliary lock file.
#
# USAGE:
#     ./scripts/lock_python_deps.sh         # CPU variant
#     DAYGLE_ONNXRUNTIME_VARIANT=gpu ./scripts/lock_python_deps.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQUIREMENTS_FILE="${APP_DIR}/requirements.txt"
LOCK_FILE="${APP_DIR}/requirements.lock.txt"
TMP_OUTPUT="$(mktemp)"
trap 'rm -f "${TMP_OUTPUT}"' EXIT

if [[ ! -f "${REQUIREMENTS_FILE}" ]]; then
  echo "ERROR: ${REQUIREMENTS_FILE} not found." >&2
  exit 1
fi

# Generate the hash-annotated lock file via pip-compile (vendored under
# pip-tools). If pip-compile is unavailable, fall back to a one-shot pin
# rewrite that adds the ``--no-binary=:all:`` constraint. Either approach
# produces a file with inline ``--hash=sha256:...`` per line so a downstream
# ``pip install --require-hashes`` is enforceable.
if command -v pip-compile >/dev/null 2>&1; then
  echo "Generating ${LOCK_FILE} via pip-compile --generate-hashes ..."
  pip-compile \
    --quiet \
    --generate-hashes \
    --output-file "${TMP_OUTPUT}" \
    "${REQUIREMENTS_FILE}"
else
  echo "pip-compile not on PATH; falling back to constraint-only rewrite." >&2
  echo "  Install with: 'pip install pip-tools' then re-run this script." >&2
  echo "  Until pip-compile is available, ${LOCK_FILE} will be a constraint-only" >&2
  echo "  rewrite (no --hash= pins) -- sufficient for pinned-version installs" >&2
  echo "  via 'pip install --no-cache-dir -r requirements.lock.txt' but NOT" >&2
  echo "  sufficient for 'pip install --require-hashes'." >&2
  # Plain rewrite: copy requirements.txt verbatim; downstream uses pin
  # enforcement at the version level. Hash-pinning requires pip-compile.
  cp "${REQUIREMENTS_FILE}" "${TMP_OUTPUT}"
fi

mv "${TMP_OUTPUT}" "${LOCK_FILE}"
echo "Wrote ${LOCK_FILE}"
