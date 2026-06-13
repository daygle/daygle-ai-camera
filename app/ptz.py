from __future__ import annotations

import socket
import logging

logger = logging.getLogger('daygle.ai')

# PelcoD command byte 2 values — bits are ORed together for diagonal moves.
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

VALID_COMMANDS = frozenset(_PELCOD_COMMANDS)


def _pelcod_packet(address: int, command_byte: int, pan_speed: int, tilt_speed: int) -> bytes:
    """Build a 7-byte PelcoD packet."""
    addr = address & 0xFF
    cmd1 = 0x00
    cmd2 = command_byte & 0xFF
    spd1 = pan_speed & 0x3F
    spd2 = tilt_speed & 0x3F
    checksum = (addr + cmd1 + cmd2 + spd1 + spd2) & 0xFF
    return bytes([0xFF, addr, cmd1, cmd2, spd1, spd2, checksum])


def send_ptz_command(host: str, port: int, address: int, command: str, speed: int = 8) -> None:
    """Send a PelcoD PTZ command over TCP to a P6S-style camera.

    The camera accepts raw PelcoD packets on its Command Port (default 6060).
    A timeout of 2 s is used so a bad host doesn't stall a request thread.
    """
    cmd_byte = _PELCOD_COMMANDS.get(command)
    if cmd_byte is None:
        raise ValueError(f"Unknown PTZ command: {command!r}. Valid: {sorted(VALID_COMMANDS)}")

    speed = max(0, min(63, int(speed)))
    packet = _pelcod_packet(int(address), cmd_byte, speed, speed)

    logger.debug("PTZ %s → %s:%d addr=%d pkt=%s", command, host, port, address, packet.hex())

    with socket.create_connection((host, port), timeout=2.0) as sock:
        sock.sendall(packet)
