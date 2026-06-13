from __future__ import annotations

import socket
import urllib.request
import urllib.parse
import logging

logger = logging.getLogger('daygle.ai')

VALID_COMMANDS = frozenset({
    'stop', 'up', 'down', 'left', 'right',
    'upleft', 'upright', 'downleft', 'downright',
    'zoom_in', 'zoom_out',
})

# ─── HTTP CGI (HiSilicon / P6S) ───────────────────────────────────────────────

_CGI_ACTS = {
    'stop':      'stop',
    'up':        'up',
    'down':      'down',
    'left':      'left',
    'right':     'right',
    'upleft':    'leftup',
    'upright':   'rightup',
    'downleft':  'leftdown',
    'downright': 'rightdown',
    'zoom_in':   'zoomin',
    'zoom_out':  'zoomout',
}

# Ordered list of CGI paths to try. Many HiSilicon-based cameras expose the
# same endpoint at multiple paths depending on firmware version.
_CGI_PATHS = [
    '/cgi-bin/hi3510/ptzctrl.cgi',
    '/web/cgi-bin/hi3510/ptzctrl.cgi',
    '/cgi-bin/ptzctrl.cgi',
]


def send_ptz_command_cgi(
    host: str,
    http_port: int,
    command: str,
    speed: int,
    username: str = '',
    password: str = '',
) -> None:
    """Send PTZ via HTTP CGI (HiSilicon / P6S firmware)."""
    act = _CGI_ACTS.get(command)
    if act is None:
        raise ValueError(f"Unknown PTZ command: {command!r}")

    speed = max(1, min(8, int(speed)))
    params = urllib.parse.urlencode({'-step': 0, '-act': act, '-speed': speed})

    last_exc: Exception | None = None
    for path in _CGI_PATHS:
        url = f'http://{host}:{http_port}{path}?{params}'
        req = urllib.request.Request(url)
        if username:
            import base64
            creds = base64.b64encode(f'{username}:{password}'.encode()).decode()
            req.add_header('Authorization', f'Basic {creds}')
        try:
            logger.debug("PTZ CGI %s → %s", command, url)
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                resp.read()
            return
        except Exception as exc:
            last_exc = exc
            logger.debug("PTZ CGI path %s failed: %s", path, exc)

    raise OSError(f"All CGI paths failed for {host}:{http_port} — last error: {last_exc}")


# ─── Raw PelcoD over TCP (fallback) ───────────────────────────────────────────

_PELCOD_COMMANDS: dict[str, int] = {
    'stop':      0x00,
    'right':     0x02,
    'left':      0x04,
    'up':        0x08,
    'down':      0x10,
    'upright':   0x0A,
    'upleft':    0x0C,
    'downright': 0x12,
    'downleft':  0x14,
    'zoom_in':   0x20,
    'zoom_out':  0x40,
}


def _pelcod_packet(address: int, command_byte: int, pan_speed: int, tilt_speed: int) -> bytes:
    addr = address & 0xFF
    cmd2 = command_byte & 0xFF
    spd1 = pan_speed & 0x3F
    spd2 = tilt_speed & 0x3F
    checksum = (addr + 0x00 + cmd2 + spd1 + spd2) & 0xFF
    return bytes([0xFF, addr, 0x00, cmd2, spd1, spd2, checksum])


def send_ptz_command_tcp(host: str, port: int, address: int, command: str, speed: int = 8) -> None:
    """Send a PelcoD PTZ command over TCP."""
    cmd_byte = _PELCOD_COMMANDS.get(command)
    if cmd_byte is None:
        raise ValueError(f"Unknown PTZ command: {command!r}")
    speed = max(0, min(63, int(speed)))
    packet = _pelcod_packet(int(address), cmd_byte, speed, speed)
    logger.debug("PTZ TCP %s → %s:%d pkt=%s", command, host, port, packet.hex())
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
        raise ValueError(f"Unknown PTZ command: {command!r}")
    if protocol == 'tcp_pelcod':
        send_ptz_command_tcp(host, tcp_port, address, command, speed)
    else:
        send_ptz_command_cgi(host, http_port, command, speed, username, password)
