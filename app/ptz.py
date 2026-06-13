from __future__ import annotations

import base64
import datetime
import hashlib
import logging
import os
import re
import socket
import urllib.error
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

# Profile token cache — avoids a GetProfiles round-trip on every button press.
_profile_token_cache: dict[tuple[str, int], str] = {}


def _wssec_header(username: str, password: str) -> str:
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
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
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read(512) if exc.fp else b''
        raise OSError(f'ONVIF HTTP {exc.code}: {body_bytes.decode(errors="replace")[:120]}') from exc


def _get_profile_token(host: str, http_port: int, username: str, password: str) -> str:
    key = (host, http_port)
    if key in _profile_token_cache:
        return _profile_token_cache[key]
    url = f'http://{host}:{http_port}/onvif/media_service'
    response = _soap(url, '<trt:GetProfiles/>', username, password)
    match = re.search(r'<[^>]*Profiles[^>]+token=["\']([^"\']+)["\']', response)
    if not match:
        match = re.search(r'token=["\']([^"\']+)["\']', response)
    if not match:
        raise OSError('Could not find ONVIF media profile token. Check credentials.')
    token = match.group(1)
    logger.debug('ONVIF profile token for %s:%d → %s', host, http_port, token)
    _profile_token_cache[key] = token
    return token


def send_ptz_command_onvif(
    host: str, http_port: int, command: str, speed: int, username: str, password: str,
) -> None:
    token = _get_profile_token(host, http_port, username, password)
    ptz_url = f'http://{host}:{http_port}/onvif/ptz_service'
    speed_factor = max(0.1, min(1.0, int(speed) / 8.0))

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
            '<tptz:Velocity>'
            f'<tt:PanTilt x="{pan * speed_factor:.3f}" y="{tilt * speed_factor:.3f}"/>'
            f'<tt:Zoom x="{zoom * speed_factor:.3f}"/>'
            '</tptz:Velocity>'
            '</tptz:ContinuousMove>'
        )

    _soap(ptz_url, body, username, password)
    logger.debug('ONVIF PTZ %s → %s:%d', command, host, http_port)


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
) -> None:
    if command not in VALID_COMMANDS:
        raise ValueError(f'Unknown PTZ command: {command!r}')
    if protocol == 'tcp_pelcod':
        send_ptz_command_tcp(host, tcp_port, address, command, max(0, min(63, speed)))
    else:
        send_ptz_command_onvif(host, http_port, command, speed, username, password)
