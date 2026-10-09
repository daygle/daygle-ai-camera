"""ONVIF discovery, profile selection, stale-token retry and Pelco-D framing (app/ptz.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.ptz as ptz  # noqa: E402

CAPABILITIES = """
<tds:Capabilities>
  <tt:Imaging><tt:XAddr>http://10.0.0.9/onvif/Imaging</tt:XAddr></tt:Imaging>
  <tt:Media><tt:XAddr>http://10.0.0.9/onvif/Media</tt:XAddr></tt:Media>
  <tt:PTZ><tt:XAddr>http://10.0.0.9:8000/onvif/PTZ</tt:XAddr></tt:PTZ>
</tds:Capabilities>
"""

PROFILES = """
<trt:Profiles token="sub" fixed="true"><tt:Name>Sub</tt:Name><tt:VideoEncoderConfiguration/></trt:Profiles>
<trt:Profiles token="main" fixed="true"><tt:Name>Main</tt:Name><tt:PTZConfiguration token="ptz0"/></trt:Profiles>
"""


@pytest.fixture(autouse=True)
def _clear_caches():
    for cache in (ptz._profile_token_cache, ptz._video_source_token_cache, ptz._service_path_cache):
        cache.clear()
    yield
    for cache in (ptz._profile_token_cache, ptz._video_source_token_cache, ptz._service_path_cache):
        cache.clear()


class _FakeCamera:
    """Answers SOAP requests by operation and records (url, body)."""

    def __init__(self, capabilities=CAPABILITIES, profiles=PROFILES):
        self.capabilities = capabilities
        self.profiles = profiles
        self.calls: list[tuple[str, str]] = []
        self.fail_ptz_once = False

    def __call__(self, url, body, username, password):
        self.calls.append((url, body))
        if 'GetCapabilities' in body:
            if self.capabilities is None:
                raise OSError('ONVIF HTTP 400 (url=x): not supported')
            return self.capabilities
        if 'GetProfiles' in body:
            return self.profiles
        if self.fail_ptz_once:
            self.fail_ptz_once = False
            raise OSError('ONVIF HTTP 500 (url=x): No such profile')
        if 'GetPresets' in body:
            return (
                '<tptz:Preset token="1"><tt:Name>Driveway</tt:Name></tptz:Preset>'
                '<tptz:Preset token="2"><tt:Name>Gate &amp; Path</tt:Name></tptz:Preset>'
            )
        return '<ok/>'


def test_discovered_paths_use_configured_host_and_port(monkeypatch):
    camera = _FakeCamera()
    monkeypatch.setattr(ptz, '_soap', camera)
    ptz.send_ptz_command_onvif('192.168.1.50', 80, 'left', 8, 'admin', 'pw')
    urls = [url for url, _body in camera.calls]
    assert urls[0] == 'http://192.168.1.50:80/onvif/device_service'
    assert 'http://192.168.1.50:80/onvif/Media' in urls  # path from XAddr, host from config
    assert urls[-1] == 'http://192.168.1.50:80/onvif/PTZ'


def test_discovery_failure_falls_back_to_default_paths(monkeypatch):
    camera = _FakeCamera(capabilities=None)
    monkeypatch.setattr(ptz, '_soap', camera)
    ptz.send_ptz_command_onvif('cam', 80, 'stop', 5, 'admin', 'pw')
    assert camera.calls[-1][0] == 'http://cam:80/onvif/ptz_service'


def test_profile_with_ptz_configuration_is_preferred():
    assert ptz._ptz_profile_token(PROFILES) == 'main'
    assert ptz._ptz_profile_token('<trt:Profiles token="only"><x/></trt:Profiles>') == 'only'


def test_caches_avoid_repeat_lookups(monkeypatch):
    camera = _FakeCamera()
    monkeypatch.setattr(ptz, '_soap', camera)
    ptz.send_ptz_command_onvif('cam', 80, 'left', 5, 'admin', 'pw')
    ptz.send_ptz_command_onvif('cam', 80, 'stop', 5, 'admin', 'pw')
    bodies = [body for _url, body in camera.calls]
    assert sum('GetCapabilities' in b for b in bodies) == 1
    assert sum('GetProfiles' in b for b in bodies) == 1


def test_soap_fault_refreshes_tokens_and_retries_once(monkeypatch):
    camera = _FakeCamera()
    monkeypatch.setattr(ptz, '_soap', camera)
    ptz.send_ptz_command_onvif('cam', 80, 'left', 5, 'admin', 'pw')
    camera.fail_ptz_once = True
    ptz.send_ptz_command_onvif('cam', 80, 'left', 5, 'admin', 'pw')
    bodies = [body for _url, body in camera.calls]
    assert sum('GetProfiles' in b for b in bodies) == 2  # cache dropped and re-read
    assert bodies[-1].startswith('<tptz:ContinuousMove>')


def test_transport_error_is_not_retried(monkeypatch):
    calls = []

    def offline(url, body, username, password):
        calls.append(body)
        if 'GetProfiles' in body:
            return PROFILES
        if 'GetCapabilities' in body:
            return CAPABILITIES
        raise OSError('ONVIF transport error (url=x): timed out')

    monkeypatch.setattr(ptz, '_soap', offline)
    with pytest.raises(OSError):
        ptz.send_ptz_command_onvif('cam', 80, 'left', 5, 'admin', 'pw')
    assert sum('ContinuousMove' in b for b in calls) == 1


def test_velocity_move_is_clamped_and_carries_timeout(monkeypatch):
    camera = _FakeCamera()
    monkeypatch.setattr(ptz, '_soap', camera)
    ptz.send_ptz_velocity_onvif('cam', 80, 3.0, -0.25, 0.5, 'admin', 'pw', timeout_seconds=0.4)
    body = camera.calls[-1][1]
    assert 'x="1.000" y="-0.250"' in body
    assert '<tt:Zoom x="0.500"/>' in body
    assert '<tptz:Timeout>PT0.40S</tptz:Timeout>' in body


def test_home_preset_and_presets(monkeypatch):
    camera = _FakeCamera()
    monkeypatch.setattr(ptz, '_soap', camera)
    ptz.goto_ptz_home_onvif('cam', 80, 'admin', 'pw')
    assert '<tptz:GotoHomePosition>' in camera.calls[-1][1]
    ptz.goto_ptz_home_onvif('cam', 80, 'admin', 'pw', preset='2')
    assert '<tptz:PresetToken>2</tptz:PresetToken>' in camera.calls[-1][1]
    assert ptz.get_ptz_presets_onvif('cam', 80, 'admin', 'pw') == [
        {'token': '1', 'name': 'Driveway'},
        {'token': '2', 'name': 'Gate & Path'},
    ]


def test_pelcod_speed_uses_the_full_range():
    assert ptz.pelcod_speed(8) == 0x3F
    assert ptz.pelcod_speed(5) == 39  # was sent as 5 (8% speed) before
    assert ptz.pelcod_speed(1) == 8


def test_pelcod_frames_and_checksums():
    # Legacy direction packet unchanged.
    assert ptz._pelcod_packet(1, 0x02, 0x20) == bytes([0xFF, 1, 0, 0x02, 0x20, 0x20, 0x43])
    move = ptz._pelcod_move_packet(1, pan=-1.0, tilt=0.5, zoom=1.0)
    assert move[3] == 0x04 | 0x08 | 0x20  # left + up + zoom in
    assert move[4] == 0x3F and move[5] == 32
    assert move[6] == sum(move[1:6]) & 0xFF


def test_ptz_connection_reads_camera_config():
    assert ptz.ptz_connection({'ptz': {'enabled': False}, 'host': 'cam'}) is None
    assert ptz.ptz_connection({'ptz': {'enabled': True}}) is None  # no host
    conn = ptz.ptz_connection({
        'stream_url': 'rtsp://user:pw@10.1.1.5:554/live', 'username': 'u', 'password': 'p',
        'ptz': {'enabled': True, 'protocol': 'tcp_pelcod', 'port': 6061, 'speed': 7},
    })
    assert conn.host == '10.1.1.5' and conn.protocol == 'tcp_pelcod'
    assert conn.tcp_port == 6061 and conn.speed == 7


def test_pelcod_move_stops_after_duration(monkeypatch):
    sent: list[bytes] = []
    monkeypatch.setattr(ptz, '_pelcod_send', lambda host, port, packet: sent.append(packet))
    monkeypatch.setattr(ptz.time, 'sleep', lambda _s: None)
    conn = ptz.ptz_connection({'host': 'cam', 'ptz': {'enabled': True, 'protocol': 'tcp_pelcod'}})
    ptz.ptz_move(conn, 0.5, 0.0, 0.4)
    assert len(sent) == 2 and sent[1][3] == 0x00  # move, then stop
    assert ptz.ptz_goto_home(conn, '') is False  # Pelco-D needs a preset number
    assert ptz.ptz_goto_home(conn, '3') is True and sent[-1][3] == 0x07 and sent[-1][5] == 3
