#!/usr/bin/env bash
# Create .venv with just the dependencies the web / auth / middleware test
# suites need.
#
# requirements.txt installs the detection stack (ultralytics and its torch
# dependency, onnxruntime, opencv, the sound runtime). None of it is needed to
# exercise the HTTP layers, but importing app.main does import cv2 and numpy at
# module scope, so the light native pair stays. Overlapping pins mirror
# requirements.txt -- bump them together, and only after the review that file
# asks for (the numpy/onnxruntime ceilings are deliberate).
#
# The coverage gate in pytest.ini (60% of the whole app) cannot be met by one
# suite, so targeted runs pass --no-cov.
#
# Usage:
#   scripts/install_test_deps.sh
#   .venv/Scripts/python.exe -m pytest tests/test_middleware.py -q --no-cov   # Windows
#   .venv/bin/python -m pytest tests/test_middleware.py -q --no-cov           # Linux/macOS
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PACKAGES=(
  "fastapi>=0.162.0,<1.0"
  "uvicorn>=0.54.0,<1.0"
  "PyYAML>=6.0.3,<7.0"
  "bcrypt>=5.0.0,<6.0"
  "python-multipart>=0.0.33,<1.0"
  "numpy>=2.2.6,<2.3"
  "opencv-python-headless>=5.0.0.93,<6.0"
  pytest
  pytest-asyncio
  pytest-cov
)

# uv is optional: it creates the venv on a Python CI supports (the repo tests
# on 3.11 and 3.13) and downloads that interpreter when it is missing, which
# also avoids a newest-Python host lacking wheels for the pinned numpy/opencv.
if command -v uv >/dev/null 2>&1; then
  [ -d .venv ] || uv venv .venv --python "${PYTHON_VERSION:-3.13}"
else
  [ -d .venv ] || "${PYTHON:-python3}" -m venv .venv
fi

VENV_PY=".venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY=".venv/Scripts/python.exe"

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV_PY" "${PACKAGES[@]}"
else
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install "${PACKAGES[@]}"
fi

echo
echo "Targeted suites (coverage gate off, as pytest.ini's 60% is app-wide):"
echo "  $VENV_PY -m pytest tests/test_middleware.py tests/test_web_auth_router_integration.py -q --no-cov"
