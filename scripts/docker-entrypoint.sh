#!/bin/sh
# Daygle AI Camera container entrypoint.
#
# Why this exists: the app's storage defaults are paths RELATIVE to the app
# directory (data/... -> /app/data inside the container). /app is root-owned
# and effectively read-only at runtime, so state written there is lost on
# container replacement -- and the writable /data volume would go unused.
#
# On FIRST boot (no /data/config.yaml yet) this script seeds the bootstrap
# config with ABSOLUTE storage paths pointing at /data, so every recording,
# snapshot, event, the SQLite database, and the bootstrap config itself live
# on the volume and survive container replacement. The seeded file is a
# normal config: operators can edit /data/config.yaml (or replace it with a
# bind mount) and the entrypoint leaves an existing file untouched.
#
# The app reads the config via DAYGLE_CONFIG=/data/config.yaml (set in the
# Dockerfile) and app/settings.py falls back to its built-in defaults for
# anything the seed file omits.

set -eu

CONFIG_FILE="${DAYGLE_CONFIG:-/data/config.yaml}"
EXAMPLE_CONFIG="/app/config.example.yaml"

if [ ! -f "${CONFIG_FILE}" ]; then
    echo "[entrypoint] Seeding bootstrap config at ${CONFIG_FILE}"
    export DAYGLE_CONFIG="${CONFIG_FILE}"
    /opt/venv/bin/python - "${EXAMPLE_CONFIG}" "${CONFIG_FILE}" <<'PY'
"""Seed the container bootstrap config (first boot only).

The storage section is always written with absolute /data paths: a relative
path inherited from the example config would keep state inside the ephemeral
container layer (/app/data) instead of the mounted volume.
"""
import pathlib
import sys

import yaml

source, destination = (pathlib.Path(arg) for arg in sys.argv[1:3])

config = {}
if source.is_file():
    loaded = yaml.safe_load(source.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        config = loaded

server = config.get("server")
if not isinstance(server, dict):
    server = {}
    config["server"] = server
server.setdefault("host", "0.0.0.0")

storage = config.get("storage")
if not isinstance(storage, dict):
    storage = {}
    config["storage"] = storage
storage_paths = {
    "data_dir": "/data",
    "database": "/data/daygle_ai_camera.sqlite3",
    "snapshots_dir": "/data/snapshots",
    "events_dir": "/data/events",
    "recordings_dir": "/data/recordings",
}
storage.update(storage_paths)

destination.parent.mkdir(parents=True, exist_ok=True)
for path in storage_paths.values():
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)

destination.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
PY
else
    echo "[entrypoint] Using existing config at ${CONFIG_FILE}"
fi

# The app also writes its rotating file log under <app>/data/logs (see
# app/main.py _configure_file_logging). That directory was made writable at
# image build time; file logs live in the container layer, so treat
# `docker logs`/compose logs as the primary log sink.
exec "$@"
