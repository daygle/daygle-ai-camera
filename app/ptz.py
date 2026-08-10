from __future__ import annotations

import base64
import datetime
import hashlib
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape as _xml_escape

logger = logging.getLogger('daygle.ai')

VALID_COMMANDS = frozenset({
    'stop', 'up', 'down', 'left', 'right',
    'upleft', 'upright', 'downleft', 'downright',
    'zoom_in', 'zoom_out',
})

# ─── ONVIF PTZ ────────────────────────────────────────────────────────────────

_ONVIF_VELOCITY: dict[str, tuple[float, float, float]] = {
    'up':        ( 0.0,  1.0,  0.0),
    'down':      ( 0.0, -1.0,  0.0),
    'left':      (-1.0,  0.0,  0.0),
    'right':     ( 1.0,  0.0,  0.0),
    'upleft':    (-0.7,  0.7,  0.0),
    'upright':   ( 0.7,  0.7,  0.0),
    'downleft':  (-0.7, -0.7,  0.0),
    'downright': ( 0.7, -0.7,  0.0),
    'zoom_in':   ( 0.0,  0.0,  1.0),
    'zoom_out':  ( 0.0,  0.0, -1.0),
    'stop':      ( 0.0,  0.0,  0.0),
}

# Profile token cache - avoids a GetProfiles round-trip on every button press.
# Entries expire after 5 minutes so stale tokens (camera reboot, firmware update,
# credential rotation) are never used indefinitely.
_PROFILE_TOKEN_TTL = 300.0
_profile_token_cache: dict[tuple[str, int], tuple[str, float]] = {}


def _wssec_header(username: str, password: str) -> str:
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    digest = base64.b64encode(
        # ONVIF UsernameToken PasswordDigest is specified as SHA-1 over
        # nonce + created + password; this is protocol interoperability, not
        # a general-purpose password hash. Keep the finding explicitly scoped.
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()  # lgtm[py/weak-cryptographic-algorithm]
    ).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    return (
        '<s:Header>'
        '<Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        '<UsernameToken>'
        f'<Username>{_xml_escape(username)}</Username>'
        f'<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
        f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</Nonce>'
        f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        '</UsernameToken>'
        '</Security>'
        '</s:Header>'
    )


# Regex used to scrub any ``http(s)://user:pass@host`` substring that may
# leak from a camera's response body or from an exception's stringified
# form. Matches the scheme, the userinfo (anything up to the next ``/`` or
# whitespace), and the trailing ``@``. Replaced with ``\1***@`` so the host
# is preserved for diagnostics while credentials are wiped.
_USERINFO_RE = re.compile(r'(https?://)[^/\s]+@')


def _safe_url_for_error(url: str) -> str:
    """Return a copy of *url* with the userinfo stripped.

    ONVIF camera URLs commonly embed Basic-Auth credentials
    (``http://admin:hunter2@192.168.1.20/onvif/...``). When an
    ``urllib.error`` or socket-level exception bubbles up, Python's
    default stringification includes the URL verbatim, which would leak
    the credentials through any 4xx response body or log line. This
    helper parses the URL, drops ``parsed.username``/``parsed.password``
    from ``netloc`` while keeping the host and port intact, and
    sanitises any userinfo embedded in the original string as a
    belt-and-braces fallback for oddly-formatted URLs.
    """
    sanitized = url
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.username or parsed.password:
            # ``parsed.netloc`` contains ``user:pass@host:port``; ``split('@', 1)[-1]``
            # drops everything before (and including) the first ``@`` so we keep
            # the literal ``host:port`` regardless of IPv6 brackets or ports.
            safe_netloc = parsed.netloc.split('@', 1)[-1]
            sanitized = urllib.parse.urlunparse(parsed._replace(netloc=f'***@{safe_netloc}'))
        # Belt-and-braces: even if the URL has no parsed userinfo, the string
        # form may still contain ``http://user:pass@`` (e.g. via a custom
        # transport's repr). Run the regex scrub on the result so logs are
        # never trusted to flag leaks.
        sanitized = _USERINFO_RE.sub(r'\1***@', sanitized)
    except Exception:
        # If urlparse itself fails (extremely malformed URLs), fall back to a
        # pure-regex scrub instead of leaking the original.
        sanitized = _USERINFO_RE.sub(r'\1***@', url)
    return sanitized


def _sanitize_error_body(body: str) -> str:
    """Strip embedded userinfo from any URL the camera parrots back."""
    return _USERINFO_RE.sub(r'\1***@', body)


def _soap(url: str, body: str, username: str, password: str) -> str:
    header = _wssec_header(username, password) if username else '<s:Header/>'
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope'
        ' xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f'{header}'
        f'<s:Body>{body}</s:Body>'
        '</s:Envelope>'
    )
    req = urllib.request.Request(url, data=envelope.encode('utf-8'), method='POST')
    req.add_header('Content-Type', 'application/soap+xml; charset=utf-8')
    safe_url = _safe_url_for_error(url)  # computed once so every branch can reuse it
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        # Camera returned a 4xx/5xx status; read up to 512 bytes of body for
        # diagnostics, scrub any embedded userinfo URLs, then re-raise with
        # only the sanitized URL in the message.
        body_bytes = exc.read(512) if exc.fp else b''
        scrubbed_body = _sanitize_error_body(body_bytes.decode(errors='replace')[:120])
        raise OSError(f'ONVIF HTTP {exc.code} (url={safe_url}): {scrubbed_body}') from exc
    except urllib.error.URLError as exc:
        # DNS failure, refused connection, TLS error or other transport-level
        # failure with no body. ``str(exc.reason)`` includes the original URL
        # in some Python builds, so we sanitize here too.
        reason = _sanitize_error_body(str(getattr(exc, 'reason', '') or ''))
        raise OSError(f'ONVIF transport error (url={safe_url}): {reason or exc.__class__.__name__}') from exc
    except (OSError, TimeoutError) as exc:
        # socket.timeout surfaces as TimeoutError; generic OSError covers
        # ``Connection reset by peer`` and similar. Strip any userinfo that
        # might leak via ``str(exc)`` (some socket errors include peername
        # but we still defensively scrub).
        raise OSError(f'ONVIF socket error (url={safe_url}): {_sanitize_error_body(str(exc))}') from exc
    except Exception as exc:
        # Last-resort catch: never let an unexpected exception type expose
        # an unsanitised URL or leaked creds embedded in a repr().
        raise OSError(f'ONVIF unexpected error (url={safe_url}): {exc.__class__.__name__}') from exc


def _get_profile_token(host: str, http_port: int, username: str, password: str) -> str:
    key = (host, http_port)
    now = time.monotonic()
    cached = _profile_token_cache.get(key)
    if cached is not None:
        token, cached_at = cached
        if now - cached_at < _PROFILE_TOKEN_TTL:
            return token
    expired = [k for k, (_, t) in _profile_token_cache.items() if now - t >= _PROFILE_TOKEN_TTL]
    for k in expired:
        del _profile_token_cache[k]
    url = f'http://{host}:{http_port}/onvif/media_service'
    response = _soap(url, '<trt:GetProfiles/>', username, password)
    match = re.search(r'<[^>]*Profiles[^>]+token=["\']([^"\']+)["\']', response)
    if not match:
        match = re.search(r'token=["\']([^"\']+)["\']', response)
    if not match:
        raise OSError('Could not find ONVIF media profile token. Check credentials.')
    token = match.group(1)
    logger.debug('ONVIF profile token for %s:%d → %s', host, http_port, token)
    _profile_token_cache[key] = (token, time.monotonic())
    return token


def send_ptz_command_onvif(
    host: str, http_port: int, command: str, speed: int, username: str, password: str,
    timeout_seconds: float = 0.4,
) -> None:
    token = _get_profile_token(host, http_port, username, password)
    ptz_url = f'http://{host}:{http_port}/onvif/ptz_service'
    speed_factor = max(0.1, min(1.0, int(speed) / 8.0))
    # ContinuousMove interprets ``<Timeout>`` as an xsd:duration ("PT{n}S").
    # The camera self-stops after this many seconds even if the explicit
    # /api/.../ptz ``stop`` command is dropped (network jitter, server
    # restart, etc.). Clamp the value defensively so a misbehaving caller
    # can't send an unreasonably long or short timeout to the camera.
    safe_timeout = max(0.05, min(10.0, float(timeout_seconds or 0.4)))
    timeout_iso = f'PT{safe_timeout:.2f}S'

    if command == 'stop':
        body = (
            '<tptz:Stop>'
            f'<tptz:ProfileToken>{token}</tptz:ProfileToken>'
            '<tptz:PanTilt>true</tptz:PanTilt>'
            '<tptz:Zoom>true</tptz:Zoom>'
            '</tptz:Stop>'
        )
    else:
        pan, tilt, zoom = _ONVIF_VELOCITY.get(command, (0.0, 0.0, 0.0))
        body = (
            '<tptz:ContinuousMove>'
            f'<tptz:ProfileToken>{token}</tptz:ProfileToken>'
            f'<tptz:Timeout>{timeout_iso}</tptz:Timeout>'
            '<tptz:Velocity>'
            f'<tt:PanTilt x="{pan * speed_factor:.3f}" y="{tilt * speed_factor:.3f}"/>'
            f'<tt:Zoom x="{zoom * speed_factor:.3f}"/>'
            '</tptz:Velocity>'
            '</tptz:ContinuousMove>'
        )

    _soap(ptz_url, body, username, password)
    logger.debug('ONVIF PTZ %s → %s:%d (timeout=%s)', command, host, http_port, timeout_iso)


# ─── Raw PelcoD over TCP (fallback for cameras without ONVIF) ─────────────────

_PELCOD_COMMANDS: dict[str, int] = {
    'stop':      0x00, 'right':     0x02, 'left':      0x04,
    'up':        0x08, 'down':      0x10, 'upright':   0x0A,
    'upleft':    0x0C, 'downright': 0x12, 'downleft':  0x14,
    'zoom_in':   0x20, 'zoom_out':  0x40,
}


def _pelcod_packet(address: int, command_byte: int, speed: int) -> bytes:
    addr = address & 0xFF
    cmd2 = command_byte & 0xFF
    spd = speed & 0x3F
    checksum = (addr + cmd2 + spd + spd) & 0xFF
    return bytes([0xFF, addr, 0x00, cmd2, spd, spd, checksum])


def send_ptz_command_tcp(host: str, port: int, address: int, command: str, speed: int) -> None:
    packet = _pelcod_packet(address, _PELCOD_COMMANDS[command], speed)
    logger.debug('PTZ TCP %s → %s:%d pkt=%s', command, host, port, packet.hex())
    with socket.create_connection((host, port), timeout=2.0) as sock:
        sock.sendall(packet)


# ─── Dispatcher ───────────────────────────────────────────────────────────────

def send_ptz_command(
    host: str,
    command: str,
    speed: int,
    protocol: str,
    *,
    http_port: int = 80,
    tcp_port: int = 6060,
    address: int = 1,
    username: str = '',
    password: str = '',
    timeout_seconds: float = 0.4,
) -> None:
    if command not in VALID_COMMANDS:
        raise ValueError(f'Unknown PTZ command: {command!r}')
    if protocol == 'tcp_pelcod':
        # PelcoD has no ``Timeout`` concept - the camera pans continuously
        # until the next command. Clients that want a tap-to-step UX must
        # pair a minimum-press-duration gate with their own JS-level Stop
        # fire on release. The config knob is not consulted here.
        send_ptz_command_tcp(host, tcp_port, address, command, max(0, min(63, speed)))
    else:
        send_ptz_command_onvif(
            host, http_port, command, speed, username, password,
            timeout_seconds=timeout_seconds,
        )
