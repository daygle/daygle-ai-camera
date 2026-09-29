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
for d in "$SP"/nvidia/*/lib; do case "$d" in *cu13*) continue ;; esac; printf '%s\n' "$d"; done > /etc/ld.so.conf.d/daygle-cuda.conf
ldconfig
```

**The `cu13` exclusion is load-bearing, not defensive tidying.** A bare
`nvidia/*/lib` glob also matches the CUDA 13 wheel layout
(`nvidia/cu13/lib`). CUDA 13 removes Pascal outright, so once that directory is
on the loader path any process on the box can resolve `libcudart.so.13` in
preference to the pinned 12.4 one, and the P4 then reports as no CUDA-capable
device. This fails with a perfectly healthy card - see
"Troubleshooting: silent CPU fallback" below.

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
  library or capability that failed. If instead it names `cudaGetDeviceCount`,
  go to the troubleshooting section below: that is a different failure with a
  healthy-looking GPU.

## Troubleshooting: silent CPU fallback with a healthy GPU

The variant that wastes the most time, because every obvious check passes.
`nvidia-smi` enumerates the P4, `cuInit(0)` returns 0 with one device, the
driver version is correct, and yet the detector logs:

```
CUDA failure 100: no CUDA-capable device is detected ; GPU=-1
GPU acceleration was requested (device=auto) but the model is running on CPU
```

`100` - and `999: unknown error` before a reboot - with `GPU=-1` is what a CUDA
13 runtime reports when asked about an `sm_61` card. Nothing is wrong with the
card, the driver, or the wheels in `requirements-gpu-pascal.txt`. The cause is a
`*-cu13` wheel (`nvidia-cudnn-cu13`, `nvidia-nccl-cu13`,
`nvidia-cusparselt-cu13`, `nvidia-nvshmem-cu13`) that landed in the venv, whose
`nvidia/cu13/lib` directory then got registered with `ld.so`.

Confirm in one line - `libcudart.so.13` must not appear:

```bash
ldconfig -p | grep -E 'libcudart|libcuda'
```

If it does, drop the cu13 directory from the loader path and refresh:

```bash
SP=$("$V" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
for d in "$SP"/nvidia/*/lib; do case "$d" in *cu13*) continue ;; esac; printf '%s\n' "$d"; done > /etc/ld.so.conf.d/daygle-cuda.conf
ldconfig
systemctl restart daygle-ai-camera
```

Restarting alone is not enough - the running process keeps whatever it already
resolved, and the stale `ld.so.cache` entry outlives the restart. Then remove
the orphan wheels so they cannot re-register. Check the pinned cu12 stack is
complete first (`"$V" -m pip list | grep cu12` should show all six from
`requirements-gpu-pascal.txt`):

```bash
"$V" -m pip uninstall -y nvidia-cudnn-cu13 nvidia-cusparselt-cu13 nvidia-nccl-cu13 nvidia-nvshmem-cu13
```

### Kernel-launch failures after an update: cuDNN overwritten in place

A different symptom with the same root cause. The GPU is still used, but every
detection fails with:

```
CUDNN_STATUS_EXECUTION_FAILED_CUDART ... CUDNN_FE failure 11: CUDNN_BACKEND_API_FAILED
Live detection skipped for camera ...: Non-zero status code returned while running Conv node
```

`nvidia-cudnn-cu13` (pulled in by a torch upgrade via ultralytics) installs
`libcudnn.so.9` into the **same** `nvidia/cudnn/lib` directory as the pinned
`nvidia-cudnn-cu12`. It overwrites the Pascal build, but pip still reports
`nvidia-cudnn-cu12 9.1.0.70` as installed, so
`pip install -r requirements-gpu-pascal.txt` answers "already satisfied" and
changes nothing. `pip show -f nvidia-cudnn-cu13 | grep libcudnn.so` listing
`nvidia/cudnn/lib/libcudnn.so.9` confirms it.

`scripts/install_python_deps.sh`, which every in-app update and
`scripts/update.sh` run, now repairs this automatically on Pascal GPUs
(compute capability below 7.0) that already have the pinned stack. It does
three things:

- downloads the pinned `nvidia-cudnn-cu12` wheel first when `nvidia-cudnn-cu13`
  is installed (if that download fails, it leaves cu13 in place and the next
  update retries);
- removes the four colliding `*-cu13` wheels listed above;
- checks each pinned wheel's shared libraries against pip's own RECORD hashes,
  and force-reinstalls (`--no-deps`) only the wheels whose files changed;
- rewrites `/etc/ld.so.conf.d/daygle-cuda.conf` with the cu13 exclusion.

The same step switches **torch/torchvision to their CPU-only builds** (same
versions, from `https://download.pytorch.org/whl/cpu`). Torch is only used to
export models, current CUDA torch wheels no longer run on Pascal, and their
CUDA 13 dependencies are what overwrite cuDNN in the first place. The CPU build
carries no NVIDIA wheels, so exports run on the CPU and cannot break GPU
inference. FP16 export needs a CUDA torch; on a P4, export FP32 models.

If the model **Update** button fails after removing the cu13 wheels by hand,
`import torch` is broken. Install the CPU build:

```bash
"$V" -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision
```

Set `DAYGLE_PASCAL_CUDA_REPAIR=0` to disable the repair, or `=1` to force it.
To repair by hand:

```bash
"$V" -m pip uninstall -y nvidia-cudnn-cu13 nvidia-cusparselt-cu13 nvidia-nccl-cu13 nvidia-nvshmem-cu13
"$V" -m pip install --no-cache-dir --force-reinstall --no-deps -r requirements-gpu-pascal.txt
ldconfig && systemctl restart daygle-ai-camera
```

Uninstall the cu13 wheels **before** reinstalling. Uninstalling
`nvidia-cudnn-cu13` deletes the shared `libcudnn*.so.9` files it claims, and
the forced reinstall then writes clean Pascal copies.

### `libcudnn.so.9: cannot open shared object file`

If the log shows this and the detector falls back to CPU, `pip list` still
shows `nvidia-cudnn-cu12 9.1.0.70` but `nvidia/cudnn/lib` holds only
`__init__.py`. The cu13 wheel was removed and the cu12 reinstall never ran.
Two bugs in older versions caused this. The in-app updater stopped
`update.sh` after 5 minutes, which could land between those two steps. And on
Python 3.12 and later, the RECORD check could not see *missing* library files
(`importlib.metadata` leaves them out of `dist.files`), so later updates never
noticed. Now the updater allows 30 minutes, the check reads RECORD directly,
and the cu12 wheel is downloaded before cu13 is removed. Re-run the update, or
restore it by hand:

```bash
"$V" -m pip install --no-cache-dir --no-deps --force-reinstall nvidia-cudnn-cu12==9.1.0.70
ldconfig && systemctl restart daygle-ai-camera
```

Ruling out the other causes, in the order worth checking:

- `cuInit(0)` returning non-zero, or a device count of 0, means the driver
  layer genuinely is broken - reinstall per sections 1 and 4.
- `dmesg -T | grep -i xid` showing `Xid 79` (fell off the PCIe bus) or `Xid 48`
  (double-bit ECC) is hardware. A driver reinstall will not help; reseat the
  card and check power cabling, or replace it.
- A warning naming a missing `.so` is the plain unmet-dependency case covered by
  section 3, not this one.

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
