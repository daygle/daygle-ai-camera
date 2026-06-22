"""Regression tests for /api/live/motion-history.

The endpoint backs the /live page's "Live motion" chart strip, which feeds
``app.live_monitor.process_live_stream_alerts`` per-camera ring buffers. This
suite seeds the buffer directly so the endpoint can be exercised in isolation
(no live camera ingest required).

Run with::

    pytest tests/test_api_motion_history.py

or from the repo root::

    pytest tests/
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

from tests.test_api import _load_app, _server, _setup_admin, _login, LocalClient  # noqa: E402


def _fresh_state():
    """Return the live ``app.state`` module after ``_load_app`` has re-imported it.

    ``_load_app`` pops every ``app.*`` entry from ``sys.modules`` so the next
    ``importlib.import_module('app.main')`` builds a brand-new module graph.
    A module-top ``import app.state`` in this test file would capture a STALE
    reference whose ``_motion_history`` deque was never populated by the new
    graph; looking up ``sys.modules['app.state']`` after ``_load_app`` returns
    the live module object the running uvicorn worker reads from too.
    """
    return sys.modules["app.state"]


def _seed_motion_history(state, camera_id: str, samples):
    """Wipe + seed the per-camera ring buffer for ``camera_id``.

    ``samples`` is an iterable of ``(ts, confidence)`` floats; entries are
    appended in order so the deque's natural ordering matches insertion order.
    """
    with state._motion_history_lock:
        buf = state._motion_history[camera_id]
        buf.clear()
        for ts, conf in samples:
            buf.append((ts, float(conf)))


def _create_camera(client: LocalClient, csrf: str, camera_id: str = "camera-1") -> None:
    """Put a stub RTSP camera into the runtime config so get_camera_config resolves."""
    status, _headers, _body = client.request(
        f"/api/cameras/{camera_id}",
        method="PUT",
        json_body={
            "backend": "rtsp",
            "width": 640,
            "height": 360,
            "fps": 12,
            "device": "rtsp",
            "flip": "none",
            "stream_url": "rtsp://127.0.0.1:554/stream1",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert status == 200, f"camera PUT returned {status}"


def test_motion_history_returns_recent_samples_ordered_by_timestamp(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        _create_camera(client, csrf, "camera-1")

        state = _fresh_state()
        now = time.time()
        # Five samples in the last few seconds, plus one stale sample from
        # 600s ago to verify window trimming drops it.
        recent = [(now - (4 - idx), 0.10 * (idx + 1)) for idx in range(5)]
        _seed_motion_history(state, "camera-1", recent + [(now - 600, 0.01)])

        status, _headers, payload = client.request(
            "/api/live/motion-history?camera_id=camera-1"
        )
        assert status == 200
        assert payload["camera_id"] == "camera-1"
        assert payload["sample_count"] == 5
        assert len(payload["samples"]) == 5
        assert payload["window_seconds"] == 60
        # Server-side filter trims old samples; only the 5 recent remain,
        # and they round-trip in ascending timestamp order.
        confidences = [s["confidence"] for s in payload["samples"]]
        assert confidences == pytest.approx([0.10, 0.20, 0.30, 0.40, 0.50], abs=1e-9)
        timestamps = [s["ts"] for s in payload["samples"]]
        assert timestamps == sorted(timestamps)
        # Each sample's ts/confidence are floats (not strings / not None).
        for sample in payload["samples"]:
            assert isinstance(sample["ts"], float)
            assert isinstance(sample["confidence"], float)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_motion_history_window_seconds_overrides_default(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        _create_camera(client, csrf, "camera-1")

        state = _fresh_state()
        now = time.time()
        # Three samples within the last 8s and one 30s old. With a 10s window
        # only the three recent ones should be returned.
        _seed_motion_history(
            state,
            "camera-1",
            [
                (now - 7.0, 0.20),
                (now - 5.0, 0.40),
                (now - 2.0, 0.60),
                (now - 30.0, 0.99),
            ],
        )

        status, _headers, payload = client.request(
            "/api/live/motion-history?camera_id=camera-1&window_seconds=10"
        )
        assert status == 200
        assert payload["window_seconds"] == 10
        assert payload["sample_count"] == 3
        assert [round(s["confidence"], 2) for s in payload["samples"]] == [0.20, 0.40, 0.60]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_motion_history_unknown_camera_returns_404(tmp_path, monkeypatch):
    # When ``_state.cameras_config`` is non-empty and the requested id is not
    # present, ``get_camera_config`` raises 404 - this is the standard "no
    # such camera" path. Seed camera-1 via PUT so the runtime list isn't
    # empty when we ask for an unrelated id.
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        _create_camera(client, csrf, 'camera-1')
        status, _headers, _body = client.request(
            '/api/live/motion-history?camera_id=camera-does-not-exist'
        )
        assert status == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_motion_history_empty_buffer_for_known_camera(tmp_path, monkeypatch):
    # When a camera is configured but no frames have been processed yet, the
    # ring buffer is empty and the endpoint returns an empty sample list.
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        csrf = _login(client)
        _create_camera(client, csrf, "camera-1")
        state = _fresh_state()
        _seed_motion_history(state, "camera-1", [])

        status, _headers, payload = client.request(
            "/api/live/motion-history?camera_id=camera-1"
        )
        assert status == 200
        assert payload["camera_id"] == "camera-1"
        assert payload["samples"] == []
        assert payload["sample_count"] == 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)
