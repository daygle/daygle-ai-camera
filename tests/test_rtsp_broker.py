"""Tests for the loopback RTSP redirect broker (app.rtsp_broker).

A fake upstream RTSP server speaks just enough RTSP to answer one
request with a canned response; a raw socket plays the role of
ffmpeg/ffprobe on the broker side. No ffmpeg binary or real camera is
needed.
"""
from __future__ import annotations

import socket
import threading

import pytest

from app.rtsp_broker import (
    RtspBroker,
    RtspBrokerError,
    camera_input_url,
    start_camera_broker,
)

_DESCRIBE = b'DESCRIBE rtsp://127.0.0.1/stream RTSP/1.0\r\nCSeq: 1\r\n\r\n'


class FakeRtspServer:
    """One-shot upstream: reads one request, sends the canned reply."""

    def __init__(self, reply: bytes) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen(5)
        self._listener.settimeout(0.5)
        self.port = int(self._listener.getsockname()[1])
        # Replies may embed b'{port}' for same-authority redirect targets.
        self._reply = reply.replace(b'{port}', str(self.port).encode())
        self._closed = False
        self.requests: list[bytes] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                data = b''
                while b'\r\n\r\n' not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.requests.append(data)
                if self._reply:
                    conn.sendall(self._reply)

    def close(self) -> None:
        self._closed = True
        try:
            self._listener.close()
        except OSError:
            pass


def _exchange(broker_url: str, request: bytes = _DESCRIBE) -> bytes:
    """Send one request through the broker and read until it closes."""
    from urllib.parse import urlsplit

    parsed = urlsplit(broker_url)
    with socket.create_connection(('127.0.0.1', parsed.port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(request)
        received = b''
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
        except (ConnectionResetError, BrokenPipeError):
            # A dropped connection may surface as a reset (e.g. Windows)
            # rather than a clean EOF; either way no (more) data arrived.
            pass
    return received


@pytest.fixture
def upstream_factory():
    servers: list[FakeRtspServer] = []

    def make(reply: bytes) -> FakeRtspServer:
        server = FakeRtspServer(reply)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_broker_forwards_normal_response_and_interleaved_packets(upstream_factory):
    body = b'v=0\r\nm=video 0 RTP/AVP 96\r\n'
    reply = (
        b'RTSP/1.0 200 OK\r\n'
        b'CSeq: 1\r\n'
        b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
    )
    interleaved = b'$' + bytes([0]) + (4).to_bytes(2, 'big') + b'abcd'
    upstream = upstream_factory(reply + interleaved)
    with RtspBroker(f'rtsp://127.0.0.1:{upstream.port}/stream') as broker:
        assert broker.url.startswith('rtsp://127.0.0.1:')
        assert broker.url.endswith('/stream')
        assert _exchange(broker.url) == reply + interleaved
    assert upstream.requests, 'broker never connected upstream'


def test_broker_blocks_redirect_to_different_host(upstream_factory):
    upstream = upstream_factory(
        b'RTSP/1.0 302 Found\r\nCSeq: 1\r\n'
        b'Location: rtsp://169.254.9.9:554/evil\r\n'
        b'Content-Length: 0\r\n\r\n'
    )
    with RtspBroker(f'rtsp://127.0.0.1:{upstream.port}/stream') as broker:
        # The redirect response itself must not reach the client: the
        # broker drops the connection so ffmpeg fails instead of
        # following the redirect to an internal host.
        assert _exchange(broker.url) == b''


def test_broker_blocks_redirect_to_different_port(upstream_factory):
    upstream = upstream_factory(
        b'RTSP/1.0 302 Found\r\nCSeq: 1\r\n'
        b'Location: rtsp://127.0.0.1:8554/stream\r\n'
        b'Content-Length: 0\r\n\r\n'
    )
    with RtspBroker(f'rtsp://127.0.0.1:{upstream.port}/stream') as broker:
        assert _exchange(broker.url) == b''


def test_broker_blocks_redirect_to_different_scheme(upstream_factory):
    upstream = upstream_factory(
        b'RTSP/1.0 302 Found\r\nCSeq: 1\r\n'
        b'Location: http://127.0.0.1/stream\r\n'
        b'Content-Length: 0\r\n\r\n'
    )
    with RtspBroker(f'rtsp://127.0.0.1:{upstream.port}/stream') as broker:
        assert _exchange(broker.url) == b''


def test_broker_rewrites_same_authority_redirect_to_itself(upstream_factory):
    upstream = upstream_factory(
        b'RTSP/1.0 302 Found\r\nCSeq: 1\r\n'
        b'Location: rtsp://127.0.0.1:{port}/live.sdp\r\n'
        b'Content-Length: 0\r\n\r\n'
    )
    with RtspBroker(f'rtsp://127.0.0.1:{upstream.port}/stream') as broker:
        received = _exchange(broker.url)
        from urllib.parse import urlsplit

        broker_port = urlsplit(broker.url).port
        assert b'302' in received
        # The client must reconnect to the broker, not the camera host.
        assert (f'Location: rtsp://127.0.0.1:{broker_port}/live.sdp').encode() in received


def test_broker_forwards_relative_redirect_unchanged(upstream_factory):
    upstream = upstream_factory(
        b'RTSP/1.0 302 Found\r\nCSeq: 1\r\n'
        b'Location: /live.sdp\r\n'
        b'Content-Length: 0\r\n\r\n'
    )
    with RtspBroker(f'rtsp://127.0.0.1:{upstream.port}/stream') as broker:
        received = _exchange(broker.url)
        assert b'Location: /live.sdp' in received


def test_camera_input_url_brokers_rtsp_and_passes_http_through(upstream_factory):
    upstream = upstream_factory(b'RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n')
    with camera_input_url(f'rtsp://user:secret@127.0.0.1:{upstream.port}/stream') as url:
        assert url.startswith('rtsp://user:secret@127.0.0.1:')
        assert url.endswith('/stream')
        assert _exchange(url) != b''
    with camera_input_url('http://camera.invalid/mjpeg') as url:
        assert url == 'http://camera.invalid/mjpeg'


def test_broker_fails_closed_when_upstream_unresolvable():
    broker = start_camera_broker('rtsp://nonexistent.invalid:554/stream')
    assert broker is not None
    try:
        with broker:
            assert _exchange(broker.url) == b''
    finally:
        broker.close()


def test_broker_rejects_non_rtsp_url():
    with pytest.raises(RtspBrokerError):
        RtspBroker('file:///etc/passwd')


def test_broker_reresolves_per_connection_and_falls_back_on_dns_failure(monkeypatch):
    """A DHCP address change is picked up on reconnect, but a transient DNS
    failure must not break a reconnect that last-known-good addresses can
    still serve."""
    import socket as socket_module

    broker = RtspBroker('rtsp://camera.invalid:554/stream')
    try:
        calls = []

        def fake_getaddrinfo(host, port, *_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                return [(socket_module.AF_INET, socket_module.SOCK_STREAM, 0, '', ('192.0.2.10', port))]
            if len(calls) == 2:
                return [(socket_module.AF_INET, socket_module.SOCK_STREAM, 0, '', ('192.0.2.20', port))]
            raise OSError('temporary DNS failure')

        monkeypatch.setattr(socket_module, 'getaddrinfo', fake_getaddrinfo)
        first = broker._ensure_resolved()
        second = broker._ensure_resolved()
        fallback = broker._ensure_resolved()
        assert first[0][1][0] == '192.0.2.10'
        assert second[0][1][0] == '192.0.2.20'
        assert fallback == second
    finally:
        broker.close()
