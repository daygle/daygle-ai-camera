#!/bin/sh
# Load the NVIDIA kernel modules CUDA needs before the service starts.
#
# CUDA needs nvidia_uvm and /dev/nvidia-uvm. They are normally loaded on
# demand by the first CUDA program, but the service runs with
# ProtectKernelModules=yes and cannot load them itself. At boot, if nothing
# else (Ollama, nvidia-smi) has loaded them before the detector starts, ONNX
# Runtime reports "no CUDA-capable device is detected" and runs on the CPU
# until the service is restarted.
#
# systemd runs this with full privileges (the "+" prefix on ExecStartPre), so
# the hardening still applies to the service itself. It never fails the
# start: a machine without an NVIDIA GPU simply has nothing to load.

if command -v nvidia-modprobe >/dev/null 2>&1; then
  # Loads nvidia + nvidia_uvm and creates /dev/nvidia0 and /dev/nvidia-uvm.
  nvidia-modprobe -u -c=0 >/dev/null 2>&1 || true
fi
modprobe -q nvidia_uvm >/dev/null 2>&1 || exit 0

# udev creates the device nodes asynchronously; give it a few seconds.
i=0
while [ ! -e /dev/nvidia-uvm ] && [ "$i" -lt 20 ]; do
  sleep 0.25
  i=$((i + 1))
done
exit 0
