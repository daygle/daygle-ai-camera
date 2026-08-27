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
| NVIDIA driver    | **580.178.04** (CUDA 13.0-capable; **last** driver branch with Pascal support) | Installed at the OS level, outside this repo |
| ONNX Runtime     | `onnxruntime-gpu` **1.20.x** (`<1.21` ceiling in `requirements.txt`) | Last line that pairs CUDA 12.x + cuDNN 9.x with Pascal support |
| CUDA runtime     | **12.4** wheels (see `requirements-gpu-pascal.txt`) | Last line shipping `sm_61` Pascal cubins; loads on the 580 driver via backward compat (newer driver runs older runtime) |
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

- **Driver backward compatibility.** 580.178.04 is a CUDA 13.0-capable driver,
  and a newer driver always runs an older CUDA runtime, so the pinned 12.4
  wheels load against it fine - this leans on guaranteed backward compat, not
  the risky minor-version forward compat. Keep the wheels on 12.4 regardless of
  how far the driver moves; they are what still ship `sm_61` cubins (next
  bullet). 580 is also the **last** NVIDIA driver branch that supports Pascal -
  the next major branch drops the P4 outright, so the `apt-mark hold` below is
  load-bearing, not optional.
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

Then at `http://<server-ip>:8080/onnx`: **Device = GPU (CUDA)**, **Precision =
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
- `numpy` **< 2.3** - held as a no-runtime-change freeze on the detection
  stack; 2.3+ is compatible with the Python 3.11 floor but must be re-validated
  against model export and P4 GPU inference before the ceiling moves.
- The `nvidia-*-cu12` pins in `requirements-gpu-pascal.txt` - bump only after
  re-running the verification in section 3 and confirming `sm_61` support.

## Pinning the kernel and driver against unattended upgrades

The validated pairing (driver 580.178.04 + CUDA 12.4 wheels) breaks silently
if the OS moves the kernel or the NVIDIA driver - `onnxruntime-gpu` falls back
to CPU without a loud error. The driver hold matters even more here: 580 is the
last branch with Pascal support, so an unattended jump to the next major branch
does not just risk the pairing - it drops the P4 entirely. On a Debian host running `unattended-upgrades`
(see [operations.md](operations.md)), pin both with `apt-mark hold` once GPU
inference verifies. Holds are respected by `unattended-upgrades` and a manual
`apt upgrade`, and they stop `autoremove` from pruning the previous fallback
kernel. Commands below assume a root shell.

### Verify the current stack first

```bash
uname -r                        # running kernel
nvidia-smi                      # driver 580.178.04, CUDA 13.0, "Tesla P4"
dkms status                     # nvidia-current/580.178.04, <kernel>: installed
modinfo nvidia | grep vermagic  # must contain the exact `uname -r` string
```

The `dkms status` line for the running kernel must end in `installed` (not
`built`), and `nvidia-smi` must enumerate the P4. The driver is the Debian
`nvidia-*` package family; `nvidia-current` is only the DKMS source name, not
an apt package.

### Hold the kernel

```bash
apt-mark hold linux-image-amd64 linux-headers-amd64
dpkg-query -W -f='${Package} ${Status}\n' 'linux-image-*' 'linux-headers-*' \
  | awk '/install ok installed/{print $1}' | xargs -r apt-mark hold
```

### Hold the driver

```bash
dpkg -l 'nvidia-*' 'cuda-*' 2>/dev/null | grep '^ii' | awk '{print $2}' | xargs -r apt-mark hold
```

Confirm both with `apt-mark showhold` - the kernel image/header packages and
the whole `nvidia-*` stack should be listed.

### Periodic manual refresh

Holding the kernel and driver also stops their security fixes (the driver is
in Debian `non-free`, which receives no security updates anyway). Refresh them
by hand on a schedule - roughly monthly to quarterly:

```bash
apt-mark unhold $(apt-mark showhold)
apt update && apt upgrade
reboot
```

After the reboot, re-run the verification above, then re-apply both hold
commands. A failed DKMS rebuild shows up in `dkms status` as `built` without
`installed`, `nvidia-smi` fails, and inference silently falls back to CPU.
