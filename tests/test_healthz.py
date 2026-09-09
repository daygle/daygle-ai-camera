"""Integration tests for the unauthenticated ``/healthz`` liveness endpoint.

``/healthz`` exists for systemd timers, uptime monitors, and the Docker
HEALTHCHECK: those callers have no session cookie and must never be bounced
to /login. The endpoint is therefore a member of ``PUBLIC_PATHS``
(app/state.py) and serves a minimal liveness payload.

What is pinned here:

1. Membership: ``/healthz`` is in ``PUBLIC_PATHS`` so the authentication
   middleware bypasses it (fail-fast if the allowlist regresses).
2. Unauthenticated reachability: 200 with no admin user existing and no
   session cookie -- the exact state a monitor probes a freshly restarted
   service in.
3. Payload contract: ``status=ok`` plus the ``service`` identifier, and NO
   per-camera / per-model fields -- liveness only, no configuration leak to
   an unauthenticated caller (camera names and detector errors stay behind
   the authenticated ``/api/status``).
4. Authenticated coexistence: the endpoint still answers 200 while a session
   exists, and the navigation middleware does not inject nav.js into the
   JSON response.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.state as state  # noqa: E402
from tests.support import (  # noqa: E402
    LocalClient,
    _load_app,
    _login,
    _server,
    _setup_admin,
)

HEALTHZ_PATH = '/healthz'


def test_healthz_is_in_public_paths():
    """``/healthz`` must stay on the auth bypass allowlist.

    A monitor probing a service whose auth is enabled gets a 303 redirect to
    /login without this membership, which reads as an outage. Pinned as a
    unit-level assertion so the regression is obvious without booting the
    server.
    """
    assert HEALTHZ_PATH in state.PUBLIC_PATHS


def test_healthz_serves_without_session_or_setup(tmp_path, monkeypatch):
    """A brand-new install (no admin user yet) must still answer 200.

    Monitors probe before an operator ever creates the first user; the
    liveness endpoint must not depend on the setup wizard having run.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        status, headers, body = client.request(HEALTHZ_PATH)
        assert status == 200, f"/healthz (no admin, no session) expected 200, got {status}"
        content_type = LocalClient.header(headers, 'Content-Type') or ''
        assert 'application/json' in content_type, (
            '/healthz should be served as application/json'
        )
        assert isinstance(body, dict), '/healthz must return a JSON object'
        assert body['status'] == 'ok'
        assert body['service'] == 'daygle-ai-camera'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_healthz_payload_is_liveness_only(tmp_path, monkeypatch):
    """No camera, stream, model, or detector state leaks on the probe.

    ``/healthz`` is reachable by anything that can reach the port; only the
    authenticated ``/api/status`` exposes camera names, frame counters, and
    detector errors. Fail if a future edit enriches the public payload.
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    try:
        status, _headers, body = LocalClient(base_url).request(HEALTHZ_PATH)
        assert status == 200
        assert isinstance(body, dict)
        forbidden_keys = {
            'camera_id', 'camera_name', 'cameras', 'model_path',
            'model_loaded', 'detector', 'fps', 'resolution', 'uptime_seconds',
        }
        leaked = forbidden_keys & set(body)
        assert not leaked, f'/healthz must not expose runtime state: {sorted(leaked)}'
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_healthz_coexists_with_authenticated_session(tmp_path, monkeypatch):
    """The probe answers 200 after setup/login, and stays nav.js-free.

    The navigation middleware must not inject the dashboard bootstrap script
    into a machine-readable response (mirrors the favicon guard).
    """
    app, _database_path = _load_app(tmp_path, monkeypatch)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        _setup_admin(client)
        _login(client)
        status, _headers, body = client.request(HEALTHZ_PATH)
        assert status == 200, f'/healthz (authenticated) expected 200, got {status}'
        text = body if isinstance(body, str) else str(body)
        assert '<script src="/static/nav.js">' not in text, (
            'nav.js must never be injected into the /healthz JSON response'
        )
        assert isinstance(body, dict) and body['status'] == 'ok'
    finally:
        server.should_exit = True
        thread.join(timeout=5)
