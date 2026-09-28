"""install_python_deps.sh keeps the Pascal CUDA 12 stack intact across updates.

Regression for a real outage: an update pulled torch 2.13, whose
nvidia-cudnn-cu13 wheel wrote libcudnn.so.9 into the same nvidia/cudnn/lib
directory as the pinned nvidia-cudnn-cu12 9.1.0.70. pip still reported 9.1
installed, but the Tesla P4 now loaded a CUDA 13 cuDNN without sm_61 kernels
and every detection failed with CUDNN_STATUS_EXECUTION_FAILED_CUDART.

These tests run the real script with a wrapper ``python`` whose ``-c`` code is
executed by the real interpreter against a fake site-packages, so the RECORD
hash check itself is exercised; ``-m pip`` calls are only logged.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "bash"
PIN = "nvidia-cudnn-cu12==9.1.0.70"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shell harness")


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


@pytest.fixture
def harness(tmp_path):
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    shutil.copy(REPO_DIR / "scripts" / "install_python_deps.sh", app / "scripts" / "install_python_deps.sh")
    (app / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (app / "requirements-gpu-pascal.txt").write_text(f"# pins\n{PIN}\ntzdata>=2024.1\n", encoding="utf-8")

    site = tmp_path / "site"
    lib = site / "nvidia" / "cudnn" / "lib"
    lib.mkdir(parents=True)
    (site / "nvidia" / "cu13" / "lib").mkdir(parents=True)
    pascal_build = b"cudnn 9.1 with sm_61 kernels"
    (lib / "libcudnn.so.9").write_bytes(pascal_build)
    dist_info = site / "nvidia_cudnn_cu12-9.1.0.70.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: nvidia-cudnn-cu12\nVersion: 9.1.0.70\n", encoding="utf-8")
    (dist_info / "RECORD").write_text(
        f"nvidia/cudnn/lib/libcudnn.so.9,{_record_hash(pascal_build)},{len(pascal_build)}\n"
        "nvidia_cudnn_cu12-9.1.0.70.dist-info/METADATA,,\n",
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pip_log = tmp_path / "pip.log"
    fake_python = bin_dir / "python"
    fake_python.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "-c" ]]; then
  case "$2" in
    *sysconfig*) echo "{site}"; exit 0 ;;
    *onnxruntime*) exit 0 ;;
  esac
  PYTHONPATH="{site}" exec "{sys.executable}" "$@"
fi
if [[ "${{1:-}} ${{2:-}}" == "-m pip" ]]; then
  shift 2
  printf '%s\\n' "$*" >> "{pip_log}"
  if [[ "${{1:-}}" == "show" ]]; then
    [[ " ${{DAYGLE_TEST_INSTALLED:-}} " == *" $2 "* ]] && exit 0 || exit 1
  fi
  exit 0
fi
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    smi = bin_dir / "nvidia-smi"
    smi.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *-L*) echo 'GPU 0: Tesla P4' ;;
  *compute_cap*) echo "${DAYGLE_TEST_COMPUTE_CAP:-6.1}" ;;
esac
""",
        encoding="utf-8",
    )
    smi.chmod(0o755)
    ldconf = tmp_path / "daygle-cuda.conf"
    ldconf.write_text(f"{site}/nvidia/cu13/lib\n", encoding="utf-8")

    def run(**env_overrides):
        pip_log.unlink(missing_ok=True)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "DAYGLE_ONNXRUNTIME_VARIANT": "gpu",
            "DAYGLE_CUDA_LDCONF": str(ldconf),
            **env_overrides,
        }
        result = subprocess.run(
            [BASH, str(app / "scripts" / "install_python_deps.sh"), str(fake_python), str(app / "requirements.txt")],
            cwd=str(app), env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        log = pip_log.read_text(encoding="utf-8").splitlines() if pip_log.exists() else []
        return result.stdout, log

    return {"run": run, "lib": lib / "libcudnn.so.9", "ldconf": ldconf, "site": site}


def _reinstalls(log):
    return [line for line in log if line.startswith("install") and "--force-reinstall" in line]


def test_overwritten_cudnn_is_restored_and_cu13_removed(harness):
    harness["lib"].write_bytes(b"cudnn 9.20 built for CUDA 13, no sm_61")
    stdout, log = harness["run"](DAYGLE_TEST_INSTALLED="nvidia-cudnn-cu13 nvidia-nccl-cu13")
    assert "uninstall -y nvidia-cudnn-cu13" in log
    assert "uninstall -y nvidia-nccl-cu13" in log
    assert not any("uninstall -y nvidia-nvshmem-cu13" in line for line in log)  # not installed
    reinstalls = _reinstalls(log)
    assert len(reinstalls) == 1 and reinstalls[0].endswith(PIN) and "--no-deps" in reinstalls[0]
    assert "Restoring Pascal CUDA 12 wheels" in stdout


def test_intact_stack_downloads_nothing(harness):
    _stdout, log = harness["run"]()
    assert _reinstalls(log) == []


def test_loader_config_drops_cu13_directories(harness):
    harness["run"]()
    lines = harness["ldconf"].read_text(encoding="utf-8").splitlines()
    assert lines == [f"{harness['site']}/nvidia/cudnn/lib"]


def test_non_pascal_gpu_is_left_alone(harness):
    harness["lib"].write_bytes(b"something else")
    _stdout, log = harness["run"](DAYGLE_TEST_COMPUTE_CAP="8.6", DAYGLE_TEST_INSTALLED="nvidia-cudnn-cu13")
    assert _reinstalls(log) == []
    assert not any(line.startswith("uninstall -y nvidia-cudnn-cu13") for line in log)


def test_repair_can_be_disabled_or_forced(harness):
    harness["lib"].write_bytes(b"something else")
    _stdout, log = harness["run"](DAYGLE_PASCAL_CUDA_REPAIR="0")
    assert _reinstalls(log) == []
    _stdout, log = harness["run"](DAYGLE_PASCAL_CUDA_REPAIR="1", DAYGLE_TEST_COMPUTE_CAP="8.6")
    assert len(_reinstalls(log)) == 1


def test_host_without_the_pascal_stack_downloads_nothing(harness):
    shutil.rmtree(next(harness["site"].glob("nvidia_cudnn_cu12-*.dist-info")))
    _stdout, log = harness["run"]()
    assert _reinstalls(log) == []


def test_cpu_variant_is_left_alone(harness):
    harness["lib"].write_bytes(b"something else")
    _stdout, log = harness["run"](DAYGLE_ONNXRUNTIME_VARIANT="cpu")
    assert _reinstalls(log) == []


def _fake_dist(site, name, version):
    dist_info = site / f"{name}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    (dist_info / "RECORD").write_text("", encoding="utf-8")


def _torch_installs(log):
    return [line for line in log if line.startswith("install") and "download.pytorch.org/whl/cpu" in line]


def test_cuda_torch_is_swapped_for_the_cpu_build(harness):
    _fake_dist(harness["site"], "torch", "2.13.0")
    _fake_dist(harness["site"], "torchvision", "0.28.0")
    stdout, log = harness["run"]()
    installs = _torch_installs(log)
    assert len(installs) == 1
    assert "--no-deps" in installs[0]
    assert installs[0].endswith("torch==2.13.0+cpu torchvision==0.28.0+cpu")
    assert "CPU-only build" in stdout


def test_cpu_torch_and_non_pascal_hosts_are_left_alone(harness):
    _fake_dist(harness["site"], "torch", "2.13.0+cpu")
    _stdout, log = harness["run"]()
    assert _torch_installs(log) == []
    shutil.rmtree(next(harness["site"].glob("torch-*.dist-info")))
    _fake_dist(harness["site"], "torch", "2.13.0")
    _stdout, log = harness["run"](DAYGLE_TEST_COMPUTE_CAP="8.6")
    assert _torch_installs(log) == []
