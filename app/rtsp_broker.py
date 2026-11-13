"""Loopback RTSP broker that constrains where FFmpeg/OpenCV connect.

FFmpeg's RTSP demuxer follows 3xx redirects internally (``Location:``
header in ``ff_rtsp_connect``) and offers no option to disable them:
``-max_redirects`` is an HTTP-protocol option and does not apply to RTSP,
and the CVE-2026-107698 fix only restricts redirect *schemes* to the
protocol whitelist -- a redirect to another RTSP host/port is still
followed. Handing the configured camera URL straight to ffmpeg therefore
lets a malicious or compromised camera server pivot the capture process
to an arbitrary internal RTSP endpoint.

Instead, live capture never sees the camera URL. It sees a loopback URL
served by :class:`RtspBroker`, which:

- re-resolves the camera hostname on each new connection (falling back
  to the last-known-good addresses on transient DNS failure, so a
  DHCP-driven address change is picked up on reconnect) and only ever
  connects to those resolved addresses,
- proxies the RTSP control connection (TCP interleaving, the transport
  the app forces for every capture path),
- inspects upstream RTSP responses and refuses to forward a 3xx redirect
  whose ``Location`` points at a different scheme, host, or port than the
  configured camera -- the connection is dropped instead, so ffmpeg
  fails to open the stream rather than following the redirect,
- rewrites a same-authority redirect's ``Location`` back to the
  broker's own loopback address, so legitimate path-only redirects
  (``/stream`` -> ``/live.sdp``) still work through the broker.

For ``rtsps`` the broker terminates TLS upstream (SNI/cert verification
as usual); the ffmpeg<->broker leg is plaintext on loopback only.
Non-RTSP inputs are unaffected: HTTP capture already passes
``-max_redirects 0``, which *is* effective for the HTTP protocol.

Everything here is standard-library only and thread-based; there is no
new dependency and no persistent state.
"""
from __future__ import annotations

import logging
import re
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger('daygle.ai.rtsp_broker')

_RTSP_SCHEMES = frozenset({'rtsp', 'rtsps'})
_DEFAULT_PORTS = {'rtsp': 554, 'rtsps': 322}
_CONNECT_TIMEOUT_SECONDS = 10.0
_RECV_BYTES = 16384
_CONTENT_LENGTH_RE = re.compile(rb'(?im)^Content-Length:[ \t]*(\d+)')
_LOCATION_RE = re.compile(rb'(?im)^Location:[ \t]*(.+?)[ \t]*\r?$')
_STATUS_RE = re.compile(rb'^RTSP/\d(?:\.\d)?[ \t]+(\d{3})')


class RtspBrokerError(RuntimeError):
    """The broker could not be started; capture must fail closed."""


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return _DEFAULT_PORTS.get(scheme.lower())


def _location_authority_matches(
    scheme: str,
    hostname: str,
    port: int | None,
    location: str,
) -> bool:
    """True if a redirect ``Location`` stays on the configured authority.

    Relative locations (path/query only) are same-authority by definition.
    Absolute locations must keep the same scheme, hostname, and effective
    port. Anything unparseable fails closed.
    """
    parsed = urlsplit(location)
    if not parsed.scheme and not parsed.netloc:
        return True
    if parsed.scheme.lower() != scheme.lower():
        return False
    location_host = parsed.hostname
    if not location_host or location_host.lower() != hostname.lower():
        return False
    return _effective_port(parsed.scheme, parsed.port) == _effective_port(scheme, port)


def _rewrite_location(location: str, broker_authority: str, original_userinfo: str) -> str:
    """Point an absolute same-authority redirect back at the broker.

    ffmpeg follows the redirect by reconnecting to the ``Location`` URL;
    rewriting its authority to the broker's loopback address routes that
    second connection through the broker (and thus to the same pinned
    upstream) instead of letting ffmpeg connect out directly.
    """
    parsed = urlsplit(location)
    if not parsed.netloc:
        return location
    userinfo = ''
    if '@' in parsed.netloc:
        userinfo = parsed.netloc.rpartition('@')[0] + '@'
    elif original_userinfo:
        userinfo = original_userinfo + '@'
    return urlunsplit((parsed.scheme, f'{userinfo}{broker_authority}', parsed.path or '/', parsed.query, ''))


def _split_userinfo(netloc: str) -> str:
    return netloc.rpartition('@')[0] if '@' in netloc else ''


class RtspBroker:
    """A loopback TCP proxy enforcing the configured RTSP endpoint.

    One broker serves every sequential connection of a capture (the
    initial handshake plus any same-authority redirect reconnects),
    re-resolving the upstream addresses on each new connection for its
    whole lifetime.
    """

    def __init__(self, stream_url: str) -> None:
        parsed = urlsplit(stream_url)
        scheme = parsed.scheme.lower()
        if scheme not in _RTSP_SCHEMES:
            raise RtspBrokerError(f'RTSP broker only handles rtsp/rtsps URLs, got scheme {scheme!r}.')
        hostname = parsed.hostname
        if not hostname:
            raise RtspBrokerError('RTSP stream URL has no hostname.')
        self._scheme = scheme
        self._hostname = hostname
        self._port = _effective_port(scheme, parsed.port) or 0
        self._userinfo = _split_userinfo(parsed.netloc)
        self._resolved: list[tuple[int, tuple]] = []
        self._resolve_lock = threading.Lock()
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self._closed = False
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.bind(('127.0.0.1', 0))
            self._listener.listen(5)
        except OSError as exc:
            self._listener.close()
            raise RtspBrokerError(f'Cannot bind loopback RTSP broker: {exc}') from exc
        self._listener.settimeout(0.5)
        self._broker_port = int(self._listener.getsockname()[1])
        self._authority = f'127.0.0.1:{self._broker_port}'
        authority = f'{self._userinfo}@{self._authority}' if self._userinfo else self._authority
        self.url = urlunsplit(('rtsp', authority, parsed.path or '/', parsed.query, ''))
        self._accept_thread: threading.Thread | None = None

    def start(self) -> RtspBroker:
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name='rtsp-broker-accept', daemon=True,
        )
        self._accept_thread.start()
        return self

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        try:
            self._listener.close()
        except OSError:
            pass
        if self._accept_thread is not None:
            # Suppress AttributeError/RuntimeError: tests that stub
            # threading.Thread install fakes without join().
            with suppress(AttributeError, RuntimeError):
                self._accept_thread.join(timeout=2)

    def __enter__(self) -> RtspBroker:
        if self._accept_thread is None:
            self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(sock)

    def _ensure_resolved(self) -> list[tuple[int, tuple]]:
        """Resolve the camera host for this connection.

        Re-resolved on every new client connection so a DHCP-driven
        address change is picked up when capture reconnects. A transient
        DNS failure falls back to the last-known-good addresses instead
        of breaking an otherwise healthy reconnect. This does not weaken
        redirect enforcement, which compares the redirect ``Location``
        against the configured hostname, not against these addresses.
        """
        with self._resolve_lock:
            try:
                infos = socket.getaddrinfo(self._hostname, self._port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            except OSError as exc:
                if self._resolved:
                    return self._resolved
                raise RtspBrokerError(f'Cannot resolve camera host {self._hostname!r}: {exc}') from exc
            seen: set[tuple] = set()
            resolved: list[tuple[int, tuple]] = []
            for family, _type, _proto, _canon, sockaddr in infos:
                if sockaddr not in seen:
                    seen.add(sockaddr)
                    resolved.append((family, sockaddr))
            if resolved:
                self._resolved = resolved
            elif not self._resolved:
                raise RtspBrokerError(f'Cannot resolve camera host {self._hostname!r}.')
            return self._resolved

    def _connect_upstream(self) -> socket.socket:
        last_error: OSError | None = None
        for family, sockaddr in self._ensure_resolved():
            raw: socket.socket | None = None
            try:
                raw = socket.socket(family, socket.SOCK_STREAM)
                raw.settimeout(_CONNECT_TIMEOUT_SECONDS)
                raw.connect(sockaddr)
                if self._scheme == 'rtsps':
                    context = ssl.create_default_context()
                    context.minimum_version = ssl.TLSVersion.TLSv1_2
                    raw = context.wrap_socket(raw, server_hostname=self._hostname)
                raw.settimeout(None)
                return raw
            except (OSError, ssl.SSLError) as exc:
                last_error = exc
                if raw is not None:
                    try:
                        raw.close()
                    except OSError:
                        pass
        raise RtspBrokerError(
            f'Cannot connect to camera {self._hostname}:{self._port}: {last_error}',
        )

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                client, _addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._track(client)
            threading.Thread(
                target=self._handle_client, args=(client,),
                name='rtsp-broker-client', daemon=True,
            ).start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        forward_thread: threading.Thread | None = None
        try:
            upstream = self._connect_upstream()
            self._track(upstream)
            forward_thread = threading.Thread(
                target=self._forward_client_to_upstream, args=(client, upstream),
                name='rtsp-broker-forward', daemon=True,
            )
            forward_thread.start()
            self._forward_upstream_to_client(upstream, client)
        except (OSError, RtspBrokerError) as exc:
            logger.warning('RTSP broker connection to %s:%s failed: %s', self._hostname, self._port, exc)
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        sock.close()
                    except OSError:
                        pass
                    self._untrack(sock)
            if forward_thread is not None and getattr(forward_thread, 'ident', None) is not None:
                with suppress(AttributeError, RuntimeError):
                    forward_thread.join(timeout=2)

    @staticmethod
    def _forward_client_to_upstream(client: socket.socket, upstream: socket.socket) -> None:
        try:
            while True:
                data = client.recv(_RECV_BYTES)
                if not data:
                    break
                upstream.sendall(data)
        except OSError:
            pass
        finally:
            try:
                upstream.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _forward_upstream_to_client(self, upstream: socket.socket, client: socket.socket) -> None:
        """Forward upstream bytes, enforcing the redirect policy.

        RTSP responses are framed explicitly; TCP-interleaved RTP packets
        (``$`` + channel + length) are forwarded verbatim. A 3xx response
        with a cross-authority ``Location`` is dropped (connection
        closed) so the client cannot be redirected off the configured
        camera; a same-authority one is rewritten to the broker address.
        """
        buffer = bytearray()
        while not self._closed:
            chunk = upstream.recv(_RECV_BYTES)
            if not chunk:
                return
            buffer.extend(chunk)
            while buffer:
                if buffer[:1] == b'$':
                    if len(buffer) < 4:
                        break
                    total = 4 + int.from_bytes(buffer[2:4], 'big')
                    if len(buffer) < total:
                        break
                    client.sendall(bytes(buffer[:total]))
                    del buffer[:total]
                    continue
                if buffer[:5].upper() == b'RTSP/':
                    header_end = buffer.find(b'\r\n\r\n')
                    if header_end == -1:
                        break
                    header = bytes(buffer[:header_end + 4])
                    length_match = _CONTENT_LENGTH_RE.search(header)
                    body_length = int(length_match.group(1)) if length_match else 0
                    total = header_end + 4 + body_length
                    if len(buffer) < total:
                        break
                    message = bytes(buffer[:total])
                    del buffer[:total]
                    message = self._apply_redirect_policy(message)
                    if message is None:
                        return
                    client.sendall(message)
                    continue
                # Unexpected framing: forward as-is rather than stall the
                # stream. Redirect enforcement covers every parseable RTSP
                # response above; non-RTSP bytes cannot drive a redirect.
                client.sendall(bytes(buffer))
                buffer.clear()
                break

    def _apply_redirect_policy(self, message: bytes) -> bytes | None:
        """Return the response to forward, or ``None`` to drop the connection."""
        status_match = _STATUS_RE.match(message)
        if status_match is None:
            return message
        status = int(status_match.group(1))
        if not 300 <= status < 400:
            return message
        header_end = message.find(b'\r\n\r\n')
        header = message[:header_end] if header_end != -1 else message
        location_match = _LOCATION_RE.search(header)
        if location_match is None:
            return message
        location = location_match.group(1).decode('utf-8', errors='replace')
        try:
            matches = _location_authority_matches(self._scheme, self._hostname, self._port, location)
        except ValueError:
            # urlsplit/.port raise on unparseable Locations (invalid port,
            # malformed IPv6). Fail closed like any other non-match.
            logger.warning('Blocked RTSP redirect with unparseable Location from %s:%s', self._hostname, self._port)
            return None
        if not matches:
            parsed = urlsplit(location)
            logger.warning(
                'Blocked RTSP redirect from %s:%s to %s://%s:%s',
                self._hostname, self._port,
                parsed.scheme or '(relative)', parsed.hostname or '(none)',
                parsed.port or '(default)',
            )
            return None
        rewritten = _rewrite_location(location, self._authority, self._userinfo)
        if rewritten == location:
            return message
        return _LOCATION_RE.sub(
            b'Location: ' + rewritten.encode('utf-8'), message, count=1,
        )


def start_camera_broker(stream_url: str) -> RtspBroker | None:
    """Start a broker for RTSP/RTSPS URLs; ``None`` for other schemes.

    Raises :class:`RtspBrokerError` on failure -- callers must not fall
    back to the direct URL, or the redirect protection is silently lost.
    """
    if urlsplit(stream_url).scheme.lower() not in _RTSP_SCHEMES:
        return None
    return RtspBroker(stream_url).start()


@contextmanager
def camera_input_url(stream_url: str) -> Iterator[str]:
    """Yield the URL capture tools should open for ``stream_url``.

    RTSP/RTSPS URLs are replaced with the broker's loopback URL for the
    duration of the block; other schemes pass through unchanged.
    """
    broker = start_camera_broker(stream_url)
    if broker is None:
        yield stream_url
        return
    with broker:
        yield broker.url
