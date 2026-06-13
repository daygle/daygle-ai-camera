"""Probe a camera's HTTP interface to find the working PTZ endpoint.

Usage:
    python scripts/probe_ptz.py 192.168.40.101 --user admin --pass admin
"""
import argparse
import base64
import urllib.request
import urllib.parse
import urllib.error

# (path, params) pairs to probe
CANDIDATES = [
    # HiSilicon hi3510 — /web prefix variants first (those returned 502 in initial probe)
    ('/web/cgi-bin/hi3510/ptzctrl.cgi', {'-step': '0', '-act': 'stop', '-speed': '3'}),
    ('/web/cgi-bin/ptzctrl.cgi',        {'-step': '0', '-act': 'stop', '-speed': '3'}),
    ('/cgi-bin/hi3510/ptzctrl.cgi',     {'-step': '0', '-act': 'stop', '-speed': '3'}),
    ('/cgi-bin/ptzctrl.cgi',            {'-step': '0', '-act': 'stop', '-speed': '3'}),
    # Foscam / decoder_control (command=1 = stop-up, used as general stop)
    ('/decoder_control.cgi',            {'command': '1', 'onestep': '0'}),
    ('/cgi-bin/decoder_control.cgi',    {'command': '1', 'onestep': '0'}),
    # Generic ptz.cgi variants
    ('/ptz.cgi',                        {'-step': '0', '-act': 'stop', '-speed': '3'}),
    ('/web/ptz.cgi',                    {'-step': '0', '-act': 'stop', '-speed': '3'}),
    ('/ptz.cgi',                        {'action': 'stop'}),
    ('/web/ptz.cgi',                    {'action': 'stop'}),
    # Dahua-style
    ('/cgi-bin/ptz.cgi',                {'action': 'stop', 'channel': '0', 'code': 'Stop', 'arg1': '0', 'arg2': '0', 'arg3': '0'}),
    # Other
    ('/cgi-bin/cmd.cgi',                {'cmd': 'ptzctrl', 'act': 'stop', 'speed': '3'}),
    ('/goform/usercmd',                 {'cmd': 'ptz', 'act': 'stop'}),
    ('/api/ptz',                        {'action': 'stop'}),
]


def probe(host: str, port: int, path: str, params: dict, auth: str) -> tuple[int, str]:
    url = f'http://{host}:{port}{path}?{urllib.parse.urlencode(params)}'
    req = urllib.request.Request(url)
    if auth:
        req.add_header('Authorization', f'Basic {auth}')
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read(300).decode(errors='replace').strip()
            return resp.status, body[:120]
    except urllib.error.HTTPError as e:
        try:
            body = e.read(300).decode(errors='replace').strip()[:120]
        except Exception:
            body = ''
        return e.code, body
    except Exception as e:
        return 0, str(e)


def main():
    parser = argparse.ArgumentParser(description='Probe camera PTZ endpoints')
    parser.add_argument('host', help='Camera IP address')
    parser.add_argument('--port', type=int, default=80, help='HTTP port (default 80)')
    parser.add_argument('--user', default='admin', help='Username')
    parser.add_argument('--pass', dest='password', default='', help='Password')
    args = parser.parse_args()

    auth = base64.b64encode(f'{args.user}:{args.password}'.encode()).decode() if args.user else ''

    print(f'\nProbing http://{args.host}:{args.port} (user={args.user})\n')
    found = []
    for path, params in CANDIDATES:
        code, body = probe(args.host, args.port, path, params, auth)
        qs = urllib.parse.urlencode(params)
        if code == 200:
            marker = '  <-- SUCCESS'
            found.append((path, params, body))
        elif code == 0:
            marker = '  (timeout/refused)'
        elif code == 404:
            marker = ''
        else:
            marker = f'  <-- RESPONDS ({code})'
        print(f'  HTTP {code or "ERR":3}  {path}?{qs}{marker}')
        if body and code not in (0, 404):
            print(f'         body: {body}')

    print()
    if found:
        print('=== Working endpoints ===')
        for path, params, body in found:
            print(f'  {path}  params={params}')
            if body:
                print(f'  response: {body}')
    else:
        print('No 200 OK found.')
        print('If all responding paths return 4xx, credentials are likely wrong.')
        print('If all return 5xx, the parameter format may not match this firmware.')
    print()


if __name__ == '__main__':
    main()
