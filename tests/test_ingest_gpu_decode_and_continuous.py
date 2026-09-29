"""Shared camera ingest: GPU (NVDEC) decoding and continuous recording.

* ``video_decode`` (auto / gpu / cpu) puts ``-hwaccel cuda`` on the ingest's
  single input when the GPU is used; a camera whose GPU decode fails falls
  back to the CPU without leaving it without detection frames.
* Continuous recording rides the same ffmpeg as a fourth output (one RTSP
  connection per camera); the ingest drains its chunk list. The dedicated
  recorder remains only for a camera without a running ingest.
"""
from __future__ import annotations

import importlib
import threading
from pathlib import Path

import pytest

import app.recordings as recordings_module
from app import video_decode

RecordingService = recordings_module.RecordingService


# ---------------------------------------------------------------------------
# app.video_decode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('setting', 'ffmpeg_cuda', 'nvidia_gpu', 'expected'), [
    ('auto', True, True, 'gpu'),
    ('auto', True, False, 'cpu'),     # no NVIDIA card
    ('auto', False, True, 'cpu'),     # ffmpeg without CUDA decode
    ('gpu', True, False, 'gpu'),      # forced, ffmpeg supports it
    ('gpu', False, True, 'cpu'),      # forced but impossible: CPU rather than no frames
    ('cpu', True, True, 'cpu'),
    ('nonsense', True, True, 'gpu'),  # unknown values act as auto
])
def test_resolve_video_decode(monkeypatch, setting, ffmpeg_cuda, nvidia_gpu, expected):
    monkeypatch.setattr(video_decode, 'probe_gpu_decode',
                        lambda **_kw: {'ffmpeg_cuda': ffmpeg_cuda, 'nvidia_gpu': nvidia_gpu})
    assert video_decode.resolve_video_decode(setting) == expected


def test_probe_reads_ffmpeg_hwaccels_and_nvidia_smi(monkeypatch):
    outputs = {
        'ffmpeg': 'Hardware acceleration methods:\nvdpau\ncuda\nvaapi\n',
        'nvidia-smi': 'GPU 0: Tesla P4 (UUID: GPU-1)\n',
    }
    monkeypatch.setattr(video_decode.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(video_decode, '_run', lambda cmd, timeout=5.0: outputs[cmd[0].rsplit('/', 1)[-1]])
    assert video_decode.probe_gpu_decode(refresh=True) == {'ffmpeg_cuda': True, 'nvidia_gpu': True}
    outputs['ffmpeg'] = 'Hardware acceleration methods:\nvdpau\n'
    assert video_decode.probe_gpu_decode(refresh=True)['ffmpeg_cuda'] is False
    video_decode.probe_gpu_decode(refresh=True)  # leave a fresh cache for other tests


def test_hwaccel_args_and_error_detection():
    assert video_decode.hwaccel_input_args('gpu') == ['-hwaccel', 'cuda']
    assert video_decode.hwaccel_input_args('cpu') == []
    assert video_decode.is_gpu_decode_error('[AVHWDeviceContext] Cannot load libcuda.so.1\nDevice creation failed: -1.')
    assert not video_decode.is_gpu_decode_error('rtsp://cam: Connection refused')


def test_setting_is_validated_and_defaults_to_auto():
    validators = importlib.import_module('app.payload_validators')
    facades = importlib.import_module('app.config_facades')
    assert facades.DEFAULT_LIVE_CONFIG['video_decode'] == 'auto'
    assert video_decode.normalize_video_decode('GPU') == 'gpu'
    assert video_decode.normalize_video_decode(None) == 'auto'
    assert validators.normalize_video_decode('bogus') == 'auto'


# ---------------------------------------------------------------------------
# Ingest worker harness
# ---------------------------------------------------------------------------

class _Proc:
    """A fake ffmpeg: runs until stopped, or exits at once with ``code``."""

    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):  # noqa: ARG002
        return self.returncode


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.time, 'sleep', lambda _s: None)
    commands: list[list[str]] = []
    diagnostics: list[str] = []
    monkeypatch.setattr(service, '_emit_diagnostic', lambda _cam, kind, *_a, **_k: diagnostics.append(kind))

    def run(decode='cpu', behaviour=None, iterations=1):
        """Run the worker until ``iterations`` ffmpeg launches; return commands."""
        monkeypatch.setattr(recordings_module, 'resolve_video_decode', lambda _setting: decode)
        stop = threading.Event()

        def popen(cmd, stderr=None, **_kw):
            commands.append(list(cmd))
            if len(commands) >= iterations:
                stop.set()
            if behaviour:
                return behaviour(cmd, stderr)
            return _Proc(code=0)

        monkeypatch.setattr(recordings_module.subprocess, 'Popen', popen)
        service._run_prebuffer_worker('cam', 'rtsp://example/stream', {
            'stop_event': stop, 'stream_url': 'rtsp://example/stream', 'buffer_seconds': 20, 'camera_id': 'cam',
        })
        return commands

    return service, run, diagnostics


def _input_options(cmd):
    return cmd[:cmd.index('-i')]


# ---------------------------------------------------------------------------
# GPU decode on the ingest
# ---------------------------------------------------------------------------

def test_gpu_decode_adds_hwaccel_to_the_single_input(ingest):
    service, run, _diag = ingest
    (cmd,) = run(decode='gpu')
    assert cmd.count('-i') == 1
    assert _input_options(cmd)[-2:] == ['-hwaccel', 'cuda']
    # Frames are copied back to system memory (no -hwaccel_output_format), so
    # the fps/JPEG output works unchanged.
    assert '-hwaccel_output_format' not in cmd
    assert service.ingest_decode_status()['cam']['decode'] == 'gpu'


def test_cpu_decode_has_no_hwaccel(ingest):
    service, run, _diag = ingest
    (cmd,) = run(decode='cpu')
    assert '-hwaccel' not in cmd
    assert service.ingest_decode_status()['cam']['decode'] == 'cpu'


def test_failed_gpu_decode_falls_back_to_cpu(ingest):
    service, run, diagnostics = ingest

    def behaviour(cmd, stderr):
        if '-hwaccel' in cmd:
            stderr.write('[AVHWDeviceContext @ 0x1] Cannot load libcuda.so.1\nDevice creation failed: -1.\n')
            stderr.flush()
            return _Proc(code=1)
        return _Proc(code=0)

    first, second = run(decode='gpu', behaviour=behaviour, iterations=2)
    assert '-hwaccel' in first and '-hwaccel' not in second
    assert 'ingest_gpu_decode_fallback' in diagnostics
    status = service.ingest_decode_status()['cam']
    assert status['decode'] == 'cpu' and status['gpu_fallback'] is True


def test_a_camera_outage_does_not_disable_gpu_decode(ingest):
    _service, run, diagnostics = ingest

    def behaviour(cmd, stderr):
        stderr.write('rtsp://example/stream: Connection refused\n')
        stderr.flush()
        return _Proc(code=1)

    first, second = run(decode='gpu', behaviour=behaviour, iterations=2)
    assert '-hwaccel' in first and '-hwaccel' in second
    assert 'ingest_gpu_decode_fallback' not in diagnostics


# ---------------------------------------------------------------------------
# Continuous recording on the shared ingest
# ---------------------------------------------------------------------------

def _register(service, chunk_seconds=3600, callback=None):
    chunks_dir = service.recordings_dir / 'continuous-cam'
    service._shared_continuous['cam'] = {
        'chunks_dir': chunks_dir, 'chunk_seconds': chunk_seconds, 'on_chunk_complete': callback,
    }
    return chunks_dir


def test_continuous_is_a_fourth_output_of_the_same_connection(ingest):
    service, run, _diag = ingest
    chunks_dir = _register(service, chunk_seconds=900)
    (cmd,) = run()
    assert cmd.count('-i') == 1, 'still one RTSP connection'
    pattern = str(chunks_dir / 'continuous_cam_%Y%m%dT%H%M%S.mp4')
    assert cmd[-1] == pattern
    output = cmd[cmd.index('-segment_list') - 20:]
    assert cmd[cmd.index('-segment_list') + 1] == str(chunks_dir / '.segment_list.txt')
    assert ['-c:v', 'copy'] == output[output.index('-c:v'):output.index('-c:v') + 2]
    assert '900' in output


def test_without_continuous_there_is_no_fourth_output(ingest):
    _service, run, _diag = ingest
    (cmd,) = run()
    assert '-segment_list' not in cmd


def test_ingest_drains_finished_chunks_to_the_callback(ingest, monkeypatch):
    service, run, _diag = ingest
    completed: list[Path] = []
    chunks_dir = _register(service, callback=lambda _key, path: completed.append(path))

    def behaviour(cmd, _stderr):
        # ffmpeg recreates the list at start; a finished chunk lands in it.
        chunk = chunks_dir / 'continuous_cam_20260929T100000.mp4'
        chunk.write_bytes(b'mp4')
        (chunks_dir / '.segment_list.txt').write_text(chunk.name + '\n')
        return _Proc(code=0)

    run(behaviour=behaviour)
    assert [p.name for p in completed] == ['continuous_cam_20260929T100000.mp4']


def test_switching_continuous_on_reconnects_the_ingest_once(ingest, monkeypatch):
    service, run, diagnostics = ingest
    procs: list[_Proc] = []

    def behaviour(cmd, _stderr):
        proc = _Proc()  # a healthy, long-running ffmpeg
        procs.append(proc)
        if len(procs) == 1:
            _register(service)  # continuous switched on while it runs
        return proc

    first, second = run(behaviour=behaviour, iterations=2)
    assert '-segment_list' not in first and '-segment_list' in second
    assert procs[0].returncode == -15, 'the running ffmpeg is stopped gracefully'
    assert 'ingest_restart' not in diagnostics, 'a planned reconnect is not reported as a failure'


def test_start_continuous_attaches_to_a_live_ingest(tmp_path, monkeypatch):
    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    started: list[str] = []
    monkeypatch.setattr(service, '_ensure_continuous_chunk_worker', lambda key, *_a: started.append(key))
    alive = threading.Event()
    thread = threading.Thread(target=alive.wait, daemon=True)
    thread.start()
    try:
        service._prebuffer_workers['cam'] = {'thread': thread, 'stream_url': 'rtsp://example/stream'}
        assert service.start_continuous_chunk_recording(stream_url='rtsp://example/stream', camera_id='cam',
                                                         recording_config={'chunk_duration_seconds': 600})
        assert started == [], 'no second RTSP connection'
        assert service._shared_continuous['cam']['chunk_seconds'] == 600
        # A different stream URL (or no ingest) uses the dedicated recorder.
        assert service.start_continuous_chunk_recording(stream_url='rtsp://other/stream', camera_id='cam2')
        assert started == ['cam2']
        service.stop_continuous_chunk_recording('cam')
        assert 'cam' not in service._shared_continuous
    finally:
        alive.set()
        thread.join(timeout=2)
