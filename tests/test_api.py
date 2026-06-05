from __future__ import annotations

import importlib
import json
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(base_url: str, path: str, method: str = "GET", params: dict[str, str] | None = None):
    if params:
        path = f"{path}?{urlencode(params)}"
    request = Request(f"{base_url}{path}", method=method)
    with urlopen(request, timeout=5) as response:  # noqa: S310 - local test server only
        body = response.read().decode("utf-8")
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.status, json.loads(body)
        return response.status, body


def _load_app(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
server:
  host: 127.0.0.1
  port: 8080
storage:
  data_dir: {tmp_path / 'data'}
  database: {tmp_path / 'data' / 'daygle.sqlite3'}
  snapshots_dir: {tmp_path / 'data' / 'snapshots'}
  events_dir: {tmp_path / 'data' / 'events'}
alerts:
  rules:
    - name: Cat alert
      object: cat
      min_confidence: 0.50
      cooldown_seconds: 0
      enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAYGLE_CONFIG", str(config_path))
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main").app


def test_dashboard_and_api_endpoints(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    try:
        deadline = time.time() + 5
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started

        status_code, root = _request(base_url, "/")
        assert status_code == 200
        assert "AI Camera Dashboard" in root

        status_code, status = _request(base_url, "/api/status")
        assert status_code == 200
        assert status["status"] == "online"

        status_code, created = _request(base_url, "/api/mock/detect", method="POST")
        assert status_code == 200
        assert created["created"] is True
        assert created["event_id"] >= 1
        assert created["detections"]

        status_code, events = _request(base_url, "/api/events")
        assert status_code == 200
        assert len(events) == 1
        assert events[0]["detections"]

        status_code, detail = _request(base_url, f"/api/events/{created['event_id']}")
        assert status_code == 200
        assert detail["id"] == created["event_id"]

        label = created["detections"][0]["label"]
        status_code, search = _request(base_url, "/api/events", params={"label": label})
        assert status_code == 200
        assert search

        status_code, alerts = _request(base_url, "/api/alerts")
        assert status_code == 200
        assert isinstance(alerts, list)

        status_code, stats = _request(base_url, "/api/stats")
        assert status_code == 200
        assert stats["total_events"] == 1

        status_code, runtime_config = _request(base_url, "/api/config")
        assert status_code == 200
        assert runtime_config["camera"]["backend"] == "mock"

        status_code, static_js = _request(base_url, "/static/app.js")
        assert status_code == 200
        assert "refreshAll" in static_js
    finally:
        server.should_exit = True
        thread.join(timeout=5)
