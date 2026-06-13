from __future__ import annotations

import socket
import urllib.request
import urllib.parse
import urllib.error
import logging

logger = logging.getLogger('daygle.ai')

VALID_COMMANDS = frozenset({
    'stop', 'up', 'down', 'left', 'right',
    'upleft', 'upright', 'downleft', 'downright',
    'zoom_in', 'zoom_out',
})

# ─── HiSilicon hi3510 CGI (-act style) ───────────────────────────────────────

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

# Paths tried in order — only those that returned non-404 in probing are listed first.
_HI3510_PATHS = [
    '/web/cgi-bin/hi3510/ptzctrl.cgi',
    '/web/cgi-bin/ptzctrl.cgi',
    '/cgi-bin/hi3510/ptzctrl.cgi',
    '/cgi-bin/ptzctrl.cgi',
    '/ptz.cgi',
    '/web/ptz.cgi',
]

# ─── Foscam / decoder_control style (numeric command codes) ──────────────────

# Each direction has a start code and a paired stop code.
_FOSCAM_CMDS = {
    'up':        (0,  1),
    'down':      (2,  3),
    'left':      (4,  5),
    'right':     (6,  7),
    'upleft':    (91, 93),   # diagonal codes vary by firmware; fallback below
    'upright':   (90, 92),
    'downleft':  (93, 93),
    'downright': (92, 92),
    'zoom_in':   (16, 17),
    'zoom_out':  (18, 19),
    'stop':      (1,  1),    # stop-up is treated as general stop by most firmware
}

_FOSCAM_PATHS = [
    '/decoder_control.cgi',
    '/cgi-bin/decoder_control.cgi',
]


def _make_request(url: str, auth: str) -> urllib.request.Request:
    req = urllib.request.Request(url)
    if auth:
        req.add_header('Authorization', f'Basic {auth}')
    return req


def _basic_auth(username: str, password: str) -> str:
    import base64
    if not username:
        return ''
    return base64.b64encode(f'{username}:{password}'.encode()).decode()


def _try_url(req: urllib.request.Request) -> int:
    """Return HTTP status code, 0 on connection error."""
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _is_success(code: int) -> bool:
    # Many P6S / HiSilicon cameras return 502 "CGI was not CGI/1.1 compliant"
    # even when the PTZ command executed successfully — the scripts run but
    # don't emit proper HTTP headers, so the web server wraps the result in 502.
    return code in (200, 201, 204, 302, 502)


def send_ptz_command_cgi(
    host: str,
    http_port: int,
    command: str,
    speed: int,
    username: str = '',
    password: str = '',
) -> None:
    """Try HiSilicon hi3510 CGI paths, then Foscam decoder_control paths."""
    auth = _basic_auth(username, password)
    speed = max(1, min(8, int(speed)))
    act = _CGI_ACTS.get(command, 'stop')

    # --- HiSilicon hi3510 style ---
    params = urllib.parse.urlencode({'-step': 0, '-act': act, '-speed': speed})
    for path in _HI3510_PATHS:
        url = f'http://{host}:{http_port}{path}?{params}'
        req = _make_request(url, auth)
        code = _try_url(req)
        logger.debug("PTZ hi3510 %s → %s  HTTP %s", command, url, code)
        if _is_success(code):
            return

    # --- Foscam / decoder_control style ---
    start_code, _ = _FOSCAM_CMDS.get(command, (1, 1))
    foscam_params = urllib.parse.urlencode({'command': start_code, 'onestep': 0})
    for path in _FOSCAM_PATHS:
        url = f'http://{host}:{http_port}{path}?{foscam_params}'
        req = _make_request(url, auth)
        code = _try_url(req)
        logger.debug("PTZ foscam %s → %s  HTTP %s", command, url, code)
        if _is_success(code):
            return

    raise OSError(
        f"No PTZ endpoint responded for {host}:{http_port}. "
        f"Check camera IP and credentials."
    )


# ─── Raw PelcoD over TCP (fallback protocol) ─────────────────────────────────

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
    cmd_byte = _PELCOD_COMMANDS.get(command)
    if cmd_byte is None:
        raise ValueError(f"Unknown PTZ command: {command!r}")
    speed = max(0, min(63, int(speed)))
    packet = _pelcod_packet(int(address), cmd_byte, speed, speed)
    logger.debug("PTZ TCP %s → %s:%d pkt=%s", command, host, port, packet.hex())
    with socket.create_connection((host, port), timeout=2.0) as sock:
        sock.sendall(packet)


# ─── Dispatcher ──────────────────────────────────────────────────────────────

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
