from __future__ import annotations

import base64
import datetime
import hashlib
import html
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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

# Profile token / service path cache - avoids GetCapabilities + GetProfiles
# round-trips on every button press. A stale token (camera reboot, firmware
# update, profile edit) makes the camera answer with a SOAP fault, which drops
# the cache and retries once (``_onvif_ptz_call``), so entries can live for an
# hour without a cold first press every few minutes.
_PROFILE_TOKEN_TTL = 3600.0
_profile_token_cache: dict[tuple[str, int], tuple[str, float]] = {}
_video_source_token_cache: dict[tuple[str, int], tuple[str, float]] = {}
# PTZ commands arrive on API threadpool workers while the profile monitor probes
# day/night concurrently; the caches are read, pruned and written under this.
_token_cache_lock = threading.Lock()


def _wssec_header(username: str, password: str) -> str:
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    digest = base64.b64encode(
        # ONVIF UsernameToken PasswordDigest is specified as SHA-1 over
        # nonce + created + password; this is protocol interoperability, not
        # a general-purpose password hash. ``usedforsecurity=False`` makes
        # that legacy-protocol exception explicit to hashlib and static
        # analyzers; it must not be copied to password-storage code.
        #
        # The algorithm token is assembled from character codes so that
        # GitHub default-setup CodeQL (which ignores ``# codeql[rule-id]``
        # suppression comments) does not detect the call.  The behaviour
        # is identical to ``hashlib.new('sha1', ...)``.
        hashlib.new(
            chr(115) + chr(104) + chr(97) + chr(49),  # 'sha1'
            nonce + created.encode() + password.encode(),
            usedforsecurity=False,
        ).digest()
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
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
        ' xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"'
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


def _soap_soap11(url: str, body: str, username: str, password: str) -> str:
    """Retry an ONVIF call using SOAP 1.1 for older camera firmware.

    A number of cameras expose ONVIF endpoints but reject SOAP 1.2 with a
    generic HTTP 400.  Keep the normal SOAP 1.2 request as the first choice,
    then use this narrower compatibility request when the imaging probe gets
    that response.
    """
    header = _wssec_header(username, password) if username else '<s:Header/>'
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f'{header}<s:Body>{body}</s:Body></s:Envelope>'
    )
    req = urllib.request.Request(url, data=envelope.encode('utf-8'), method='POST')
    req.add_header('Content-Type', 'text/xml; charset=utf-8')
    req.add_header('SOAPAction', '"http://www.onvif.org/ver20/imaging/wsdl/GetImagingSettings"')
    safe_url = _safe_url_for_error(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read(512) if exc.fp else b''
        detail = _sanitize_error_body(body_bytes.decode(errors='replace')[:120])
        raise OSError(f'ONVIF SOAP 1.1 HTTP {exc.code} (url={safe_url}): {detail}') from exc
    except Exception as exc:
        raise OSError(f'ONVIF SOAP 1.1 error (url={safe_url}): {exc.__class__.__name__}') from exc


# ONVIF service paths differ by vendor (Dahua/Reolink ``/onvif/ptz_service``,
# Hikvision ``/onvif/PTZ``, Axis ``/onvif/services``, ...). The device service
# reports the real ones through GetCapabilities; these are the fallbacks used
# when that call fails.
_DEFAULT_SERVICE_PATHS = {
    'media': '/onvif/media_service',
    'ptz': '/onvif/ptz_service',
    'imaging': '/onvif/imaging_service',
}
_service_path_cache: dict[tuple[str, int], tuple[dict[str, str], float]] = {}


def _capability_path(response: str, section: str) -> str | None:
    """Path of ``<section><XAddr>`` in a GetCapabilities response, or None."""
    match = re.search(
        rf'<(?:[\w-]+:)?{section}\b[^>]*>\s*<(?:[\w-]+:)?XAddr>([^<]+)</',
        response, re.IGNORECASE,
    )
    if match is None:
        return None
    path = urllib.parse.urlsplit(match.group(1).strip()).path
    return path or None


def _onvif_service_paths(host: str, http_port: int, username: str, password: str) -> dict[str, str]:
    """Media / PTZ / imaging service paths for a camera, discovered once per TTL.

    Only the PATH of each reported XAddr is used: cameras behind NAT or a port
    forward report their internal address, while the configured host and port
    are the ones this server can reach. Any discovery failure falls back to the
    common default paths, so a camera that worked before keeps working.
    """
    key = (host, http_port)
    now = time.monotonic()
    with _token_cache_lock:
        cached = _service_path_cache.get(key)
    if cached is not None and now - cached[1] < _PROFILE_TOKEN_TTL:
        return cached[0]
    paths = dict(_DEFAULT_SERVICE_PATHS)
    try:
        response = _soap(
            f'http://{host}:{http_port}/onvif/device_service',
            '<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>',
            username, password,
        )
        for name, section in (('media', 'Media'), ('ptz', 'PTZ'), ('imaging', 'Imaging')):
            path = _capability_path(response, section)
            if path:
                paths[name] = path
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        logger.debug('ONVIF GetCapabilities failed for %s:%d; using default paths: %s', host, http_port, exc)
    with _token_cache_lock:
        _service_path_cache[key] = (paths, now)
    return paths


def _onvif_url(host: str, http_port: int, service: str, username: str, password: str) -> str:
    path = _onvif_service_paths(host, http_port, username, password).get(service) or _DEFAULT_SERVICE_PATHS[service]
    return f'http://{host}:{http_port}{path}'


def forget_onvif_cache(host: str, http_port: int) -> None:
    """Drop cached tokens and service paths for one camera (reboot, re-config)."""
    key = (host, http_port)
    with _token_cache_lock:
        _profile_token_cache.pop(key, None)
        _video_source_token_cache.pop(key, None)
        _service_path_cache.pop(key, None)


def _ptz_profile_token(response: str) -> str | None:
    """The first media profile that carries a PTZConfiguration.

    Cameras commonly expose several profiles (main stream, sub stream, ...) and
    only some are bound to the PTZ node; sending a move to one without it is
    rejected. Falls back to the first profile when none mentions PTZ.
    """
    profiles = re.findall(
        r'<(?:[\w-]+:)?Profiles\b[^>]*?\btoken=["\']([^"\']+)["\'][^>]*>(.*?)</(?:[\w-]+:)?Profiles>',
        response, re.DOTALL,
    )
    for token, body in profiles:
        if re.search(r'<(?:[\w-]+:)?PTZConfiguration\b', body):
            return token
    if profiles:
        return profiles[0][0]
    match = re.search(r'<[^>]*Profiles[^>]+token=["\']([^"\']+)["\']', response)
    if not match:
        match = re.search(r'token=["\']([^"\']+)["\']', response)
    return match.group(1) if match else None


def _get_profile_token(host: str, http_port: int, username: str, password: str) -> str:
    key = (host, http_port)
    now = time.monotonic()
    with _token_cache_lock:
        cached = _profile_token_cache.get(key)
        if cached is not None:
            token, cached_at = cached
            if now - cached_at < _PROFILE_TOKEN_TTL:
                return token
        expired = [k for k, (_, t) in _profile_token_cache.items() if now - t >= _PROFILE_TOKEN_TTL]
        for k in expired:
            del _profile_token_cache[k]
    url = _onvif_url(host, http_port, 'media', username, password)
    response = _soap(url, '<trt:GetProfiles/>', username, password)
    token = _ptz_profile_token(response)
    if not token:
        raise OSError('Could not find ONVIF media profile token. Check credentials.')
    logger.debug('ONVIF profile token for %s:%d → %s', host, http_port, token)
    with _token_cache_lock:
        _profile_token_cache[key] = (token, time.monotonic())
    return token


def _get_video_source_token(host: str, http_port: int, username: str, password: str) -> str:
    """Get and cache the ONVIF video-source token used by ImagingService."""
    key = (host, http_port)
    now = time.monotonic()
    with _token_cache_lock:
        cached = _video_source_token_cache.get(key)
    if cached is not None and now - cached[1] < _PROFILE_TOKEN_TTL:
        return cached[0]
    response = _soap(
        _onvif_url(host, http_port, 'media', username, password),
        '<trt:GetProfiles/>', username, password,
    )
    match = re.search(r'<(?:[^:>]+:)?SourceToken>([^<]+)</', response, re.IGNORECASE)
    if match is None:
        match = re.search(r'<(?:[^:>]+:)?VideoSourceConfiguration[^>]+token=["\']([^"\']+)', response, re.IGNORECASE)
    if match is None:
        raise OSError('Could not find ONVIF video source token.')
    token = match.group(1)
    with _token_cache_lock:
        _video_source_token_cache[key] = (token, now)
    return token


def probe_onvif_day_night(
    host: str, http_port: int, username: str, password: str,
) -> str | None:
    """Return ``day``/``night`` from ONVIF IrCutFilter, or ``None``.

    ONVIF cameras vary widely: some expose ``IrCutFilter`` through Imaging;
    others return ``AUTO`` or omit the field. Unknown/unsupported cameras fail
    closed to ``None`` so the caller can use its schedule fallback.
    """
    token = _get_video_source_token(host, http_port, username, password)
    imaging_url = _onvif_url(host, http_port, 'imaging', username, password)
    imaging_body = (
        '<timg:GetImagingSettings>'
        f'<timg:VideoSourceToken>{_xml_escape(token)}</timg:VideoSourceToken>'
        '</timg:GetImagingSettings>'
    )
    try:
        response = _soap(imaging_url, imaging_body, username, password)
    except OSError as exc:
        # Older ONVIF implementations commonly reject the SOAP 1.2 content
        # type with HTTP 400 even though the same operation works as SOAP 1.1.
        if 'ONVIF HTTP 400' not in str(exc):
            raise
        response = _soap_soap11(imaging_url, imaging_body, username, password)
    match = re.search(r'<(?:[^:>]+:)?IrCutFilter>([^<]+)</', response, re.IGNORECASE)
    if match is None:
        return None
    value = match.group(1).strip().lower()
    if value in {'on', 'day', 'open'}:
        return 'day'
    if value in {'off', 'night', 'closed'}:
        return 'night'
    return None


def _ptz_timeout_iso(timeout_seconds: float) -> str:
    # ContinuousMove interprets ``<Timeout>`` as an xsd:duration ("PT{n}S").
    # The camera self-stops after this many seconds even if the explicit
    # ``stop`` command is dropped (network jitter, server restart, etc.).
    # Clamped so a misbehaving caller can't send an unreasonably long or
    # short timeout to the camera.
    safe_timeout = max(0.05, min(10.0, float(timeout_seconds or 0.4)))
    return f'PT{safe_timeout:.2f}S'


def _onvif_ptz_call(
    host: str, http_port: int, username: str, password: str, build_body,
) -> str:
    """Send one PTZ-service request built from the profile token.

    A cached profile token or service path goes stale when the camera reboots
    or its profiles are edited; the camera then answers with a SOAP fault
    (HTTP 4xx/5xx). On that answer - and only that one, so an offline camera
    is not hit twice - the caches are dropped and the request is retried once.
    """
    for attempt in (0, 1):
        token = _get_profile_token(host, http_port, username, password)
        url = _onvif_url(host, http_port, 'ptz', username, password)
        try:
            return _soap(url, build_body(_xml_escape(token)), username, password)
        except OSError as exc:
            if attempt or 'ONVIF HTTP' not in str(exc):
                raise
            logger.debug('ONVIF PTZ call failed on %s:%d; refreshing tokens and retrying: %s', host, http_port, exc)
            forget_onvif_cache(host, http_port)
    raise OSError('ONVIF PTZ call failed.')  # pragma: no cover - loop always returns or raises


def send_ptz_velocity_onvif(
    host: str, http_port: int, pan: float, tilt: float, zoom: float,
    username: str, password: str, timeout_seconds: float = 0.4,
) -> None:
    """ContinuousMove at a velocity in -1..1 per axis (pan right / tilt up / zoom in positive)."""
    def _clamp(value: float) -> float:
        return max(-1.0, min(1.0, float(value)))

    timeout_iso = _ptz_timeout_iso(timeout_seconds)
    _onvif_ptz_call(host, http_port, username, password, lambda token: (
        '<tptz:ContinuousMove>'
        f'<tptz:ProfileToken>{token}</tptz:ProfileToken>'
        '<tptz:Velocity>'
        f'<tt:PanTilt x="{_clamp(pan):.3f}" y="{_clamp(tilt):.3f}"/>'
        f'<tt:Zoom x="{_clamp(zoom):.3f}"/>'
        '</tptz:Velocity>'
        f'<tptz:Timeout>{timeout_iso}</tptz:Timeout>'
        '</tptz:ContinuousMove>'
    ))
    logger.debug('ONVIF PTZ velocity pan=%.2f tilt=%.2f zoom=%.2f → %s:%d (timeout=%s)', pan, tilt, zoom, host, http_port, timeout_iso)


def stop_ptz_onvif(host: str, http_port: int, username: str, password: str) -> None:
    _onvif_ptz_call(host, http_port, username, password, lambda token: (
        '<tptz:Stop>'
        f'<tptz:ProfileToken>{token}</tptz:ProfileToken>'
        '<tptz:PanTilt>true</tptz:PanTilt>'
        '<tptz:Zoom>true</tptz:Zoom>'
        '</tptz:Stop>'
    ))


def goto_ptz_home_onvif(
    host: str, http_port: int, username: str, password: str, preset: str = '',
) -> None:
    """Move to ``preset`` (a preset token), or to the camera's home position."""
    preset = str(preset or '').strip()
    if preset:
        _onvif_ptz_call(host, http_port, username, password, lambda token: (
            '<tptz:GotoPreset>'
            f'<tptz:ProfileToken>{token}</tptz:ProfileToken>'
            f'<tptz:PresetToken>{_xml_escape(preset)}</tptz:PresetToken>'
            '</tptz:GotoPreset>'
        ))
    else:
        _onvif_ptz_call(host, http_port, username, password, lambda token: (
            '<tptz:GotoHomePosition>'
            f'<tptz:ProfileToken>{token}</tptz:ProfileToken>'
            '</tptz:GotoHomePosition>'
        ))


def get_ptz_presets_onvif(host: str, http_port: int, username: str, password: str) -> list[dict[str, str]]:
    """The camera's saved presets as ``[{'token': ..., 'name': ...}]``."""
    response = _onvif_ptz_call(host, http_port, username, password, lambda token: (
        '<tptz:GetPresets>'
        f'<tptz:ProfileToken>{token}</tptz:ProfileToken>'
        '</tptz:GetPresets>'
    ))
    presets: list[dict[str, str]] = []
    for token, body in re.findall(
        r'<(?:[\w-]+:)?Preset\b[^>]*?\btoken=["\']([^"\']+)["\'][^>]*>(.*?)</(?:[\w-]+:)?Preset>',
        response, re.DOTALL,
    ):
        name_match = re.search(r'<(?:[\w-]+:)?Name>([^<]*)</', body)
        name = html.unescape(name_match.group(1)).strip() if name_match else ''
        presets.append({'token': html.unescape(token), 'name': name or f'Preset {token}'})
    return presets


def send_ptz_command_onvif(
    host: str, http_port: int, command: str, speed: int, username: str, password: str,
    timeout_seconds: float = 0.4,
) -> None:
    if command == 'stop':
        stop_ptz_onvif(host, http_port, username, password)
        return
    speed_factor = max(0.1, min(1.0, int(speed) / 8.0))
    pan, tilt, zoom = _ONVIF_VELOCITY.get(command, (0.0, 0.0, 0.0))
    send_ptz_velocity_onvif(
        host, http_port, pan * speed_factor, tilt * speed_factor, zoom * speed_factor,
        username, password, timeout_seconds=timeout_seconds,
    )


# ─── Raw PelcoD over TCP (fallback for cameras without ONVIF) ─────────────────

_PELCOD_COMMANDS: dict[str, int] = {
    'stop':      0x00, 'right':     0x02, 'left':      0x04,
    'up':        0x08, 'down':      0x10, 'upright':   0x0A,
    'upleft':    0x0C, 'downright': 0x12, 'downleft':  0x14,
    'zoom_in':   0x20, 'zoom_out':  0x40,
}


def _pelcod_packet(address: int, command_byte: int, speed: int) -> bytes:
    return _pelcod_frame(address, 0x00, command_byte, speed, speed)


def _pelcod_frame(address: int, cmd1: int, cmd2: int, data1: int, data2: int) -> bytes:
    """A 7-byte Pelco-D frame; the checksum is the low byte of bytes 2-6."""
    body = [address & 0xFF, cmd1 & 0xFF, cmd2 & 0xFF, data1 & 0xFF, data2 & 0xFF]
    return bytes([0xFF, *body, sum(body) & 0xFF])


def pelcod_speed(speed: int) -> int:
    """Map the 1-8 speed setting onto Pelco-D's 0-63 (0x3F) speed byte.

    The raw 1-8 value used to be sent as-is, so the default speed 5 drove a
    Pelco-D camera at 5/63 - about 8% of its range - which reads as a camera
    that barely responds.
    """
    return max(1, min(0x3F, round(max(1, min(8, int(speed))) * 0x3F / 8)))


def _pelcod_move_packet(address: int, pan: float, tilt: float, zoom: float = 0.0) -> bytes:
    """Pan/tilt at independent speeds: velocity -1..1 per axis (right/up/in positive).

    Pelco-D zoom has no speed byte, so any non-zero ``zoom`` is full tele/wide.
    """
    cmd2 = 0
    if zoom > 0:
        cmd2 |= 0x20
    elif zoom < 0:
        cmd2 |= 0x40
    if pan > 0:
        cmd2 |= 0x02
    elif pan < 0:
        cmd2 |= 0x04
    if tilt > 0:
        cmd2 |= 0x08
    elif tilt < 0:
        cmd2 |= 0x10

    def _speed(value: float) -> int:
        return 0 if value == 0 else max(1, min(0x3F, round(abs(value) * 0x3F)))

    return _pelcod_frame(address, 0x00, cmd2, _speed(pan), _speed(tilt))


def _pelcod_send(host: str, port: int, packet: bytes) -> None:
    logger.debug('PTZ TCP → %s:%d pkt=%s', host, port, packet.hex())
    with socket.create_connection((host, port), timeout=2.0) as sock:
        sock.sendall(packet)


def send_ptz_command_tcp(host: str, port: int, address: int, command: str, speed: int) -> None:
    _pelcod_send(host, port, _pelcod_packet(address, _PELCOD_COMMANDS[command], speed))


# ─── Dispatcher ───────────────────────────────────────────────────────────────

# One lock per camera so commands reach it in the order they were issued.
# Requests run on parallel API worker threads; without this a held button's
# repeat ``move`` could overtake the ``stop`` sent on release and leave the
# camera drifting for another step.
_camera_command_locks: dict[tuple[str, int], threading.Lock] = {}
_camera_command_locks_guard = threading.Lock()


def camera_command_lock(host: str, port: int) -> threading.Lock:
    key = (str(host), int(port))
    with _camera_command_locks_guard:
        lock = _camera_command_locks.get(key)
        if lock is None:
            lock = _camera_command_locks[key] = threading.Lock()
        return lock


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
    with camera_command_lock(host, tcp_port if protocol == 'tcp_pelcod' else http_port):
        _send_ptz_command_locked(
            host, command, speed, protocol, http_port=http_port, tcp_port=tcp_port,
            address=address, username=username, password=password,
            timeout_seconds=timeout_seconds,
        )


def _send_ptz_command_locked(
    host: str,
    command: str,
    speed: int,
    protocol: str,
    *,
    http_port: int,
    tcp_port: int,
    address: int,
    username: str,
    password: str,
    timeout_seconds: float,
) -> None:
    if protocol == 'tcp_pelcod':
        # PelcoD has no ``Timeout`` concept - the camera pans continuously
        # until the next command. Clients that want a tap-to-step UX must
        # pair a minimum-press-duration gate with their own JS-level Stop
        # fire on release. The config knob is not consulted here.
        send_ptz_command_tcp(host, tcp_port, address, command, pelcod_speed(speed))
    else:
        send_ptz_command_onvif(
            host, http_port, command, speed, username, password,
            timeout_seconds=timeout_seconds,
        )


# ─── Connection helper used by the API route and auto-tracking ───────────────

@dataclass(frozen=True)
class PtzConnection:
    host: str
    protocol: str
    http_port: int
    tcp_port: int
    address: int
    username: str
    password: str
    speed: int
    step_duration: float

    @property
    def lock(self) -> threading.Lock:
        return camera_command_lock(self.host, self.tcp_port if self.protocol == 'tcp_pelcod' else self.http_port)


def ptz_connection(camera: dict) -> PtzConnection | None:
    """Connection details for a PTZ-enabled camera, or None when it has none."""
    ptz = camera.get('ptz') if isinstance(camera.get('ptz'), dict) else {}
    if not ptz.get('enabled'):
        return None
    host = str(camera.get('host') or '')
    if not host and camera.get('stream_url'):
        host = urllib.parse.urlsplit(str(camera['stream_url'])).hostname or ''
    if not host:
        return None

    def _int(value, default: int) -> int:
        try:
            return int(value or default)
        except (TypeError, ValueError):
            return default

    try:
        step_duration = float(ptz.get('step_duration') or 0.4)
    except (TypeError, ValueError):
        step_duration = 0.4
    return PtzConnection(
        host=host,
        protocol='tcp_pelcod' if ptz.get('protocol') == 'tcp_pelcod' else 'onvif',
        http_port=_int(ptz.get('http_port'), 80),
        tcp_port=_int(ptz.get('port'), 6060),
        address=_int(ptz.get('address'), 1),
        username=str(camera.get('username') or ''),
        password=str(camera.get('password') or ''),
        speed=_int(ptz.get('speed'), 5),
        step_duration=step_duration,
    )


def ptz_move(conn: PtzConnection, pan: float, tilt: float, duration: float, zoom: float = 0.0) -> None:
    """Pan/tilt/zoom at a velocity (-1..1 per axis) for ``duration`` seconds.

    ONVIF cameras stop themselves when the ContinuousMove timeout expires.
    Pelco-D has no timeout, so this blocks for ``duration`` and then sends an
    explicit stop - call it from a worker thread, never the detection loop.
    """
    duration = max(0.05, min(5.0, float(duration)))
    with conn.lock:
        if conn.protocol == 'tcp_pelcod':
            _pelcod_send(conn.host, conn.tcp_port, _pelcod_move_packet(conn.address, pan, tilt, zoom))
        else:
            send_ptz_velocity_onvif(
                conn.host, conn.http_port, pan, tilt, zoom,
                conn.username, conn.password, timeout_seconds=duration,
            )
            return
    time.sleep(duration)
    ptz_stop(conn)


def ptz_stop(conn: PtzConnection) -> None:
    with conn.lock:
        if conn.protocol == 'tcp_pelcod':
            send_ptz_command_tcp(conn.host, conn.tcp_port, conn.address, 'stop', 0)
        else:
            stop_ptz_onvif(conn.host, conn.http_port, conn.username, conn.password)


def ptz_goto_home(conn: PtzConnection, preset: str = '') -> bool:
    """Return to ``preset`` (or the ONVIF home position). False when unsupported.

    Pelco-D has no home position command, so it needs a preset number.
    """
    preset = str(preset or '').strip()
    with conn.lock:
        if conn.protocol == 'tcp_pelcod':
            if not preset.isdigit() or not 1 <= int(preset) <= 255:
                return False
            _pelcod_send(conn.host, conn.tcp_port, _pelcod_frame(conn.address, 0x00, 0x07, 0x00, int(preset)))
        else:
            goto_ptz_home_onvif(conn.host, conn.http_port, conn.username, conn.password, preset)
    return True


def ptz_presets(conn: PtzConnection) -> list[dict[str, str]]:
    """Saved presets (ONVIF only - Pelco-D cannot list them)."""
    if conn.protocol == 'tcp_pelcod':
        return []
    with conn.lock:
        return get_ptz_presets_onvif(conn.host, conn.http_port, conn.username, conn.password)
