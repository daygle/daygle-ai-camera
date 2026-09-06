"""Guard dependency/install conventions against configuration drift."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_optional_model_simplifier_is_not_installed_on_every_server():
    """The exporter must keep its optional simplifier out of the base install."""
    installer = _read("scripts/install_debian.sh")
    model_management = _read("app/model_management.py")
    readme = _read("README.md")

    assert "onnxsim" not in installer
    assert "_onnxsim_available" in model_management
    assert "pip install --no-cache-dir onnxsim" in readme


def test_dependency_variants_keep_cpu_and_gpu_onnx_runtime_exclusive():
    """The installer must select one ONNX Runtime wheel, never both."""
    installer = _read("scripts/install_python_deps.sh")

    assert "pip uninstall -y onnxruntime onnxruntime-gpu" in installer
    assert "DAYGLE_ONNXRUNTIME_VARIANT" in installer
    assert "onnxruntime-gpu" in installer
    assert "onnxruntime>=1.20.1,<1.21" in _read("requirements.txt")


def test_pascal_cuda_dependencies_are_kept_out_of_generic_requirements():
    """CUDA userspace pins remain an explicit Pascal-only installation step."""
    requirements = _read("requirements.txt")
    pascal_requirements = _read("requirements-gpu-pascal.txt")

    assert "nvidia-cuda-runtime-cu12" not in requirements
    assert "nvidia-cudnn-cu12" not in requirements
    assert "nvidia-cuda-runtime-cu12==12.4.127" in pascal_requirements
    assert "nvidia-cudnn-cu12==9.1.0.70" in pascal_requirements
