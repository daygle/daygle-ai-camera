"""Second-stage PTZ probe: test ONVIF, Jovision JSON, and port 6060 protocol.

Usage:
    python scripts/probe_ptz2.py 192.168.40.101 --user admin --pass YOUR_PASSWORD
"""
import argparse
import base64
import json
import socket
import time
import urllib.request
import urllib.error

# ─── ONVIF ────────────────────────────────────────────────────────────────────

ONVIF_PATHS = [
    '/onvif/device_service',
    '/onvif/service',
    '/onvif/ptz_service',
    '/onvif/media_service',
    '/onvif',
]

ONVIF_PROBE = b'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Body>
    <tds:GetDeviceInformation xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>
  </s:Body>
</s:Envelope>'''


def probe_onvif(host, port, path, auth):
    url = f'http://{host}:{port}{path}'
    req = urllib.request.Request(url, data=ONVIF_PROBE, method='POST')
    req.add_header('Content-Type', 'application/soap+xml; charset=utf-8')
    if auth:
        req.add_header('Authorization', f'Basic {auth}')
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read(200).decode(errors='replace')
            return resp.status, body[:100]
    except urllib.error.HTTPError as e:
        body = e.read(100).decode(errors='replace') if e.fp else ''
        return e.code, body[:80]
    except Exception as e:
        return 0, str(e)[:60]


# ─── Jovision JSON protocol on port 6060 ─────────────────────────────────────

def jovision_login(sock, user, password):
    """Send Jovision login packet, return token or None."""
    login = json.dumps({
        'LoginType': 'Admin',
        'cmd': 'Login',
        'user': user,
        'password': password,
        'AuthorityGroup': 'Admin',
    }).encode()
    # Jovision framing: 4-byte little-endian length prefix
    import struct
    frame = struct.pack('<I', len(login)) + login
    sock.sendall(frame)
    time.sleep(0.3)
    raw = sock.recv(4096)
    try:
        # Strip 4-byte length prefix if present
        payload = raw[4:] if len(raw) > 4 else raw
        resp = json.loads(payload.decode(errors='replace'))
        return resp.get('token') or resp.get('Token') or resp.get('session')
    except Exception:
        return None


def probe_jovision(host, port, user, password):
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            token = jovision_login(sock, user, password)
            if token:
                return True, f'Login OK, token={token}'
            # Try without framing (raw JSON)
    except Exception as e:
        return False, str(e)

    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            login = json.dumps({'cmd': 'Login', 'user': user, 'password': password}).encode() + b'\n'
            sock.sendall(login)
            time.sleep(0.3)
            raw = sock.recv(4096)
            if raw:
                return True, f'Got response: {raw[:80]}'
    except Exception as e:
        return False, str(e)

    return False, 'No response'


# ─── NetSurveillance / Sofia binary protocol ──────────────────────────────────

def probe_netsurveil(host, port):
    """Try the NetSurveillance magic login header."""
    # Common magic for NetSurveillance/Sofia/XMeye protocol
    magic = bytes([
        0xff, 0x00, 0x00, 0x00,  # head
        0x00, 0x00, 0x00, 0x00,  # session id
        0x00, 0x00, 0x00, 0x00,  # sequence
        0x00, 0x00,              # total/current packet
        0xe8, 0x03,              # cmd: 1000 = login
        0x00, 0x00, 0x00, 0x00,  # data len
    ])
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            sock.sendall(magic)
            time.sleep(0.3)
            resp = sock.recv(256)
            if resp:
                return True, f'Got {len(resp)} bytes: {resp[:20].hex()}'
            return False, 'No response'
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('host')
    parser.add_argument('--port', type=int, default=80)
    parser.add_argument('--cmd-port', type=int, default=6060)
    parser.add_argument('--user', default='admin')
    parser.add_argument('--pass', dest='password', default='')
    args = parser.parse_args()

    auth = base64.b64encode(f'{args.user}:{args.password}'.encode()).decode()

    print(f'\n=== ONVIF probe on {args.host}:{args.port} ===')
    for path in ONVIF_PATHS:
        code, body = probe_onvif(args.host, args.port, path, auth)
        marker = '  <-- RESPONDS' if code not in (0, 404) else ''
        print(f'  HTTP {code or "ERR":3}  {path}{marker}')
        if body and code not in (0, 404):
            print(f'         {body[:80]}')

    print(f'\n=== Jovision JSON probe on {args.host}:{args.cmd_port} ===')
    ok, msg = probe_jovision(args.host, args.cmd_port, args.user, args.password)
    print(f'  {"OK" if ok else "FAIL"}: {msg}')

    print(f'\n=== NetSurveillance/Sofia probe on {args.host}:{args.cmd_port} ===')
    ok, msg = probe_netsurveil(args.host, args.cmd_port)
    print(f'  {"OK" if ok else "FAIL"}: {msg}')

    print()


if __name__ == '__main__':
    main()
