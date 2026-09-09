# Daygle AI Camera - container image (CPU default, optional Pascal/Tesla-P4 GPU).
#
# Build (CPU):
#     docker build -t daygle-ai-camera .
#
# Build (GPU, CUDA 12.4-era wheels pinned for Pascal sm_61 -- see
# docs/tesla-p4-gpu-setup.md):
#     docker build --build-arg ORT_VARIANT=gpu -t daygle-ai-camera:gpu .
#
# Dependency handling mirrors the Debian installer exactly:
#   * scripts/install_python_deps.sh is invoked FROM /app/scripts so its
#     APP_DIR resolution (dirname of the script) finds the committed
#     requirements.cpu.lock.txt and installs it with --require-hashes. The
#     lock is compiled for --python-version 3.11 on linux, so the base image
#     is pinned to 3.11-slim to keep every wheel hash valid.
#   * The variant is selected explicitly via the ORT_VARIANT build arg (never
#     "auto") so the build host's GPU cannot flip the outcome. GPU builds
#     additionally install the pinned CUDA 12.4 / cuDNN 9.x userspace wheels
#     from requirements-gpu-pascal.txt and register their lib dirs with
#     ldconfig, mirroring docs/tesla-p4-gpu-setup.md.
#
# Runtime layout: /app holds the (root-owned) application, /data (a volume)
# holds all mutable state. On first boot /app/scripts/docker-entrypoint.sh
# seeds /data/config.yaml with absolute storage paths, because the app's
# built-in defaults are relative to the app directory, which is read-only
# inside the container.

FROM python:3.11-slim

ARG ORT_VARIANT=cpu
# Fail the build on an unknown variant rather than silently falling back.
# "auto" is deliberately forbidden here: resolution must not depend on the
# build host's GPU.
RUN case "${ORT_VARIANT}" in cpu|gpu) ;; *) \
      echo "ERROR: ORT_VARIANT must be 'cpu' or 'gpu' (got '${ORT_VARIANT}')" >&2; exit 1 ;; esac

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg is required for RTSP capture and audio muxing; libgl1/libglib2.0-0 are
# the OpenCV runtime libraries; tini reaps the ffmpeg workers the app spawns so
# container stop does not leave zombie processes; curl is used by the
# HEALTHCHECK probe against the unauthenticated /healthz endpoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer (cached across source-only rebuilds). The installer script
# is copied to /app/scripts (not /tmp) so its APP_DIR == /app and it finds the
# committed lock. GPU builds then layer the pinned Pascal CUDA userspace wheels
# and register their lib dirs with the dynamic loader.
# requirements files go to /app (so APP_DIR == /app finds the lock next to
# them); the installer script itself must live at /app/scripts/ for that same
# APP_DIR derivation (dirname of the script + ..).
COPY requirements.txt requirements-gpu-pascal.txt requirements.cpu.lock.txt /app/
COPY scripts/install_python_deps.sh /app/scripts/
RUN python -m venv /opt/venv \
    && DAYGLE_ONNXRUNTIME_VARIANT="${ORT_VARIANT}" \
        /app/scripts/install_python_deps.sh /opt/venv/bin/python /app/requirements.txt \
    && if [ "${ORT_VARIANT}" = "gpu" ]; then \
        /opt/venv/bin/pip install --no-cache-dir -r /app/requirements-gpu-pascal.txt \
        && SP="$(/opt/venv/bin/python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")" \
        && printf '%s\n' "${SP}"/nvidia/*/lib > /etc/ld.so.conf.d/daygle-cuda.conf \
        && ldconfig; \
    fi \
    && rm -f /app/requirements.txt /app/requirements-gpu-pascal.txt /app/requirements.cpu.lock.txt /app/scripts/install_python_deps.sh

ENV PATH="/opt/venv/bin:${PATH}"
# The variant env var was only for the build-time install; do not leak it into
# runtime (nothing reads it there, but stale env invites confusion).
ENV DAYGLE_ONNXRUNTIME_VARIANT=

# Application source (web dashboard, app/, models label files, entrypoint).
COPY app/ /app/app/
COPY web/ /app/web/
COPY models/ /app/models/
COPY config.example.yaml /app/config.example.yaml
COPY scripts/docker-entrypoint.sh /app/scripts/docker-entrypoint.sh

# Runtime state lives on a volume so recordings/snapshots/models survive
# container replacement; the bootstrap config is seeded into the volume so
# persisted dashboard settings and this file stay together.
ENV DAYGLE_CONFIG=/data/config.yaml
# /app/models is writable by the runtime user so the first-start model
# download lands there; mount a named volume at /app/models to persist it
# across container replacement (see docker-compose.yml). /app/data exists
# writable because app/main.py creates its rotating file log under
# <app>/data/logs at startup; container-layer file logs are best-effort and
# `docker logs` remains the primary sink.
RUN mkdir -p /data /app/data \
    && useradd --system --home /data --shell /usr/sbin/nologin daygle \
    && chown -R daygle:daygle /data /app/data /app/models \
    && chmod 0755 /app/scripts/docker-entrypoint.sh
# /app/models is writable by the runtime user so the first-start model
# download lands there; mount a named volume at /app/models to persist it
# across container replacement (see docker-compose.yml).
VOLUME ["/data"]
USER daygle

EXPOSE 8080

# Liveness probe against the unauthenticated /healthz endpoint (see
# docs/operations.md). Assumes the default server.port; override this
# HEALTHCHECK if you change the port in a custom /data/config.yaml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# tini is PID 1 (signal handling + zombie reaping for ffmpeg workers); the
# entrypoint seeds /data/config.yaml on first boot and then execs the server,
# so signals still reach the app process.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/docker-entrypoint.sh"]
CMD ["python", "-m", "app.server"]
