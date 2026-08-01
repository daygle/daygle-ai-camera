"""Tests for CPU/GPU dependency selection in the install helper."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_DIR / "scripts" / "install_python_deps.sh"


class DependencyVariantSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="daygle_install_variant_"))
        self.bin_dir = self.tmpdir / "bin"
        self.bin_dir.mkdir()
        self.capture = self.tmpdir / "selected-requirements.txt"
        self.fake_python = self.bin_dir / "fake-python"
        self.fake_python.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-c" ]]; then
  if [[ "${DAYGLE_TEST_PROVIDER_FAIL:-0}" == "1" ]]; then
    exit 1
  fi
  exit 0
fi
if [[ "$*" == *" uninstall "* ]]; then
  exit 0
fi
for ((i = 1; i <= $#; i++)); do
  if [[ "${!i}" == "-r" ]]; then
    next=$((i + 1))
    cp "${!next}" "${DAYGLE_TEST_CAPTURE}"
  fi
done
""",
            encoding="utf-8",
        )
        self.fake_python.chmod(0o755)
        fake_smi = self.bin_dir / "nvidia-smi"
        fake_smi.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-L" ]]; then
  if [[ "${DAYGLE_TEST_NO_GPU:-0}" == "1" ]]; then
    exit 1
  fi
  printf '%s\\n' 'GPU 0: Tesla P4'
fi
""",
            encoding="utf-8",
        )
        fake_smi.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, variant: str, provider_fail: bool = False) -> str:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["DAYGLE_ONNXRUNTIME_VARIANT"] = variant
        env["DAYGLE_TEST_CAPTURE"] = str(self.capture)
        env["DAYGLE_TEST_PROVIDER_FAIL"] = "1" if provider_fail else "0"
        env["DAYGLE_TEST_NO_GPU"] = "0"
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPT), str(self.fake_python), str(REPO_DIR / "requirements.txt")],
            cwd=str(REPO_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.capture.read_text(encoding="utf-8")

    @staticmethod
    def _package_lines(requirements: str) -> list[str]:
        return [
            line.strip().lower()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def test_gpu_variant_replaces_cpu_onnxruntime(self):
        lines = self._package_lines(self._run("gpu"))
        self.assertTrue(any(line.startswith("onnxruntime-gpu") for line in lines))
        self.assertFalse(any(line.startswith("onnxruntime=") for line in lines))

    def test_auto_variant_detects_nvidia_smi(self):
        lines = self._package_lines(self._run("auto"))
        self.assertTrue(any(line.startswith("onnxruntime-gpu") for line in lines))
        self.assertFalse(any(line.startswith("onnxruntime=") for line in lines))

    def test_cpu_variant_keeps_cpu_onnxruntime(self):
        lines = self._package_lines(self._run("cpu"))
        self.assertTrue(any(line.startswith("onnxruntime>=") for line in lines))
        self.assertFalse(any(line.startswith("onnxruntime-gpu") for line in lines))

    def test_auto_variant_falls_back_to_cpu_without_usable_nvidia_smi(self):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["DAYGLE_ONNXRUNTIME_VARIANT"] = "auto"
        env["DAYGLE_TEST_CAPTURE"] = str(self.capture)
        env["DAYGLE_TEST_NO_GPU"] = "1"
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPT), str(self.fake_python), str(REPO_DIR / "requirements.txt")],
            cwd=str(REPO_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._package_lines(self.capture.read_text(encoding="utf-8"))
        self.assertTrue(any(line.startswith("onnxruntime>=") for line in lines))
        self.assertFalse(any(line.startswith("onnxruntime-gpu") for line in lines))

    def test_gpu_provider_registration_failure_is_reported(self):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["DAYGLE_ONNXRUNTIME_VARIANT"] = "gpu"
        env["DAYGLE_TEST_PROVIDER_FAIL"] = "1"
        env["DAYGLE_TEST_CAPTURE"] = str(self.capture)
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPT), str(self.fake_python), str(REPO_DIR / "requirements.txt")],
            cwd=str(REPO_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CUDAExecutionProvider is unavailable", result.stderr)

    def test_invalid_variant_is_rejected(self):
        env = dict(os.environ)
        env["DAYGLE_ONNXRUNTIME_VARIANT"] = "cuda"
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPT), str(self.fake_python), str(REPO_DIR / "requirements.txt")],
            cwd=str(REPO_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be 'auto', 'cpu', or 'gpu'", result.stderr)


if __name__ == "__main__":
    unittest.main()
