# Tesla P4 / Pascal GPU setup runbook

This runbook captures the full working GPU inference setup for a **Tesla P4**
(Pascal, compute capability 6.1) so it can be rebuilt from scratch. The CUDA
userspace libraries it installs are **not** managed by `install_debian.sh` or
`install_python_deps.sh` - those deliberately stay out of the driver/CUDA
layer - so without this document the working configuration exists only on the
running box.

Follow it end to end after a rebuild, a fresh `install_debian.sh` run, or any
time GPU inference falls back to CPU.

## Baseline (what this was validated against)

| Component        | Value / pin                                  | Why |
|------------------|----------------------------------------------|-----|
| GPU              | Tesla P4, Pascal, compute capability **6.1** (`sm_61`) | Target hardware |
| NVIDIA driver    | **550.163.01** (native CUDA 12.4 driver)     | Installed at the OS level, outside this repo |
| ONNX Runtime     | `onnxruntime-gpu` **1.20.x** (`<1.21` ceiling in `requirements.txt`) | Last line that pairs CUDA 12.x + cuDNN 9.x with Pascal support |
| CUDA runtime     | **12.4** wheels (see `requirements-gpu-pascal.txt`) | Matches the driver; avoids leaning on minor-version forward compat |
| cuDNN            | **9.1.0.70** (`nvidia-cudnn-cu12`)           | Debian does not package cuDNN; installed as a pip wheel |

The driver side is an OS-level prerequisite and is out of scope here: install
the NVIDIA driver first and confirm `nvidia-smi` enumerates the P4 before
proceeding.

## Why the CUDA libraries are pip wheels

`onnxruntime-gpu`'s `CUDAExecutionProvider` (`libonnxruntime_providers_cuda.so`)
dynamically loads CUDA 12.x + cuDNN 9.x at session-create time. Nothing on a
stock Debian system provides those - and Debian does not package cuDNN at all -
so they are installed as pip wheels **into the same virtualenv** as
onnxruntime-gpu.

> **`get_available_providers()` is not proof of a working GPU.** It lists what
> ONNX Runtime was *built* with, not what can actually load. It will show
> `CUDAExecutionProvider` even when every CUDA dependency is missing. The real
> checks are `ldd` on the provider `.so` and actually loading it - both below.

## 1. Install the CUDA userspace wheels

The pins live in [`requirements-gpu-pascal.txt`](../requirements-gpu-pascal.txt).
This is ~2.5 GB, so check `df -h` first.

```bash
V=/opt/daygle-ai-camera/.venv/bin/python

"$V" -m pip install --no-cache-dir -r requirements-gpu-pascal.txt
```

`nvidia-cudnn-cu12` declares a dependency on `nvidia-cublas-cu12`, so confirm
pip did not quietly upgrade cuBLAS past `12.4.5.8` while resolving. If it did,
re-pin it:

```bash
"$V" -m pip install --no-cache-dir "nvidia-cublas-cu12==12.4.5.8"
```

### Why these exact versions

- **Match the driver.** 550.163.01 is a native CUDA 12.4 driver, so pinning the
  12.4 wheels avoids relying on minor-version forward compatibility.
- **Pascal support.** These versions still ship `sm_61` cubins. CUDA 12.8+
  wheels drop Pascal kernels from individual libraries without a clean error
  (you get a kernel-launch failure or a silent fall back to CPU), and CUDA 13
  removes Pascal outright. Do not bump these without re-verifying `sm_61`.

## 2. Register the libraries with the dynamic loader

The wheels land inside the venv, where `ld.so` won't look by default. Because
Daygle runs as a systemd service, an `ld.so.conf.d` entry is more robust than
`LD_LIBRARY_PATH` in a unit override:

```bash
SP=$("$V" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
printf '%s\n' "$SP"/nvidia/*/lib > /etc/ld.so.conf.d/daygle-cuda.conf
ldconfig
```

## 3. Verify

Both CUDA libraries should resolve, and the provider `.so` should have **no**
unmet dependencies:

```bash
ldconfig -p | grep -E 'libcublasLt.so.12|libcudnn.so.9'

ORT_CUDA_SO=$("$V" -c "import onnxruntime, pathlib; print(pathlib.Path(onnxruntime.__file__).parent / 'capi/libonnxruntime_providers_cuda.so')")
ldd "$ORT_CUDA_SO" | grep 'not found'
```

The second command should print **nothing at all**. Then prove the provider
actually loads (what `get_available_providers()` cannot tell you):

```bash
"$V" -c "import ctypes, onnxruntime, pathlib; \
ctypes.CDLL(str(pathlib.Path(onnxruntime.__file__).parent / 'capi/libonnxruntime_providers_cuda.so')); \
print('CUDA EP loads OK')"
```

## 4. Restart and configure the detector

```bash
systemctl restart daygle-ai-camera
journalctl -u daygle-ai-camera -f
```

Then at `http://<server-ip>:8080/onnx`: **Device = CUDA (GPU)**, **Precision =
FP32**, **GPU memory limit = 0**. Save, then **Reload detector → Check model →
Test detector**. In a second shell, `nvidia-smi` should show the Python process
holding GPU memory during the test - that is the definitive confirmation, more
than any log line.

## Gotchas

- **Ignore `TensorrtExecutionProvider`.** It is listed because the wheel is
  built with it, but the TensorRT libraries are not installed and should not be
  - TensorRT is where Pascal support gets actively hostile. Leave the device
  set to CUDA.
- **First inference is slow.** With no `sm_61` cubin match in some kernels, CUDA
  JIT-compiles them at load; it caches afterward. A first `Test detector` taking
  30+ seconds is this, not a failure.
- **If it still falls back to CPU**, grab the ONNX Runtime warning that starts
  `Failed to create CUDAExecutionProvider` from the journal - it names the exact
  library or capability that failed.

## Version ceilings to hold

These are enforced in `requirements.txt` (and documented in
`.github/dependabot.yml`) so Dependabot cannot silently propose a
Pascal-breaking or Python-incompatible bump:

- `onnxruntime` / `onnxruntime-gpu` **< 1.21** - stays on the CUDA 11/12-era
  line that supports Pascal. Newer ORT moves to a CUDA stack that drops it.
- `numpy` **< 2.3** - numpy 2.3 requires Python 3.11 and 2.5 requires 3.12; the
  project still targets Python 3.10.
- The `nvidia-*-cu12` pins in `requirements-gpu-pascal.txt` - bump only after
  re-running the verification in section 3 and confirming `sm_61` support.
