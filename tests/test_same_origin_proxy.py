"""Regression tests for ``app.middleware._is_same_origin``.

Covers the reverse-proxy scheme/host reconciliation (F1): a TLS-terminating
reverse proxy forwards the INTERNAL ``http://host:8080`` connection to uvicorn
while the browser's ``Origin`` is the EXTERNAL ``https://host``. The same-origin
guard must honour ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` from a *trusted*
proxy so mutating requests are not hard-403'd, while still rejecting the same
headers from an *untrusted* peer (anti-spoofing).

These construct a minimal stub request so the test imports only ``app.middleware``
(no ML runtime deps) and does not require the full FastAPI app to boot.
"""

from types import SimpleNamespace

from starlette.datastructures import URL, Headers

from app.middleware import _is_same_origin


def _request(headers: dict, url: str, client_host: str):
    return SimpleNamespace(
        headers=Headers(headers),
        url=URL(url),
        client=SimpleNamespace(host=client_host),
    )


def test_direct_same_origin_matches():
    req = _request(
        {'Origin': 'http://cam.local:8080'},
        'http://cam.local:8080/api/settings/system',
        '203.0.113.9',
    )
    ok, reason = _is_same_origin(req)
    assert ok, reason


def test_trusted_proxy_forwarded_proto_reconciles_https():
    # Browser Origin is external https; uvicorn saw internal http://:8080.
    # Loopback proxy (default-trusted) forwards the real scheme/host.
    req = _request(
        {
            'Origin': 'https://cam.example.com',
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'cam.example.com',
        },
        'http://cam.example.com:8080/api/settings/system',
        '127.0.0.1',
    )
    ok, reason = _is_same_origin(req)
    assert ok, reason


def test_untrusted_peer_cannot_spoof_forwarded_headers():
    # Same forwarded headers, but the direct peer is NOT a trusted proxy, so
    # they must be ignored and the http/https scheme mismatch rejects.
    req = _request(
        {
            'Origin': 'https://cam.example.com',
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'cam.example.com',
        },
        'http://cam.example.com:8080/api/settings/system',
        '203.0.113.9',
    )
    ok, reason = _is_same_origin(req)
    assert not ok
    assert 'does not match' in reason


def test_cross_origin_host_still_rejected_from_trusted_proxy():
    # A trusted proxy does not make an unrelated attacker origin match.
    req = _request(
        {
            'Origin': 'https://evil.example',
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'cam.example.com',
        },
        'http://cam.example.com:8080/api/settings/system',
        '127.0.0.1',
    )
    ok, reason = _is_same_origin(req)
    assert not ok
    assert 'does not match' in reason


def test_default_port_normalisation_https():
    # Origin omits the port (443 implied); request served on https default.
    req = _request(
        {
            'Origin': 'https://cam.example.com',
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'cam.example.com:443',
        },
        'http://cam.example.com:8080/api/cameras',
        '127.0.0.1',
    )
    ok, reason = _is_same_origin(req)
    assert ok, reason


def test_missing_origin_and_referer_rejected():
    req = _request({}, 'http://cam.local:8080/api/cameras', '127.0.0.1')
    ok, reason = _is_same_origin(req)
    assert not ok
    assert 'Missing Origin and Referer' in reason
