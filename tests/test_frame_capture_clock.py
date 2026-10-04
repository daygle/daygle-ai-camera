"""Detection frames are stamped on the clip clock, not at decode completion.

The ingest's decoded ``latest.jpg`` lands after decoder pipelining, so its mtime
trails the moment the frame was captured; playback overlays built from those
stamps drew every box behind the object. ``FrameCaptureClock`` maps each
frame's stream time onto the prebuffer segments' wall clock instead.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import app.recordings as recordings_module
from app.frame_capture_clock import (
    SEGMENT_LIST_NAME,
    FrameCaptureClock,
    detection_frame_output_args,
    segment_list_args,
    stream_frame_stats_supported,
)
from app.recordings import RecordingService


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _clock_with_segment(tmp_path, *, segment_end_stream_t, segment_mtime):
    camera_dir = tmp_path / 'cam'
    camera_dir.mkdir()
    segment = camera_dir / 'segment-1.mp4'
    segment.write_bytes(b'x')
    os.utime(segment, (segment_mtime, segment_mtime))
    (camera_dir / SEGMENT_LIST_NAME).write_text(
        f'segment-0.mp4,0.000000,4.000000\nsegment-1.mp4,4.000000,{segment_end_stream_t:.6f}\n',
        encoding='utf-8',
    )
    clock = FrameCaptureClock()
    read_fd, write_fd = os.pipe()
    clock.attach('cam', read_fd, camera_dir)
    return clock, write_fd


def test_failed_probe_is_not_cached(monkeypatch):
    import app.frame_capture_clock as clock_module

    results = iter([
        subprocess.CompletedProcess([], 1, stdout='', stderr='boom'),
        subprocess.CompletedProcess([], 0, stdout='  -stats_mux_pre_fmt  format', stderr=''),
    ])
    monkeypatch.setattr(clock_module.subprocess, 'run', lambda *_a, **_k: next(results))
    monkeypatch.setattr(clock_module, '_support_cache', {})
    # A non-zero exit says nothing about the build: unsupported now, re-probed later.
    assert stream_frame_stats_supported('/opt/ffmpeg-probe-test') is False
    assert stream_frame_stats_supported('/opt/ffmpeg-probe-test') is True


def test_frame_output_args_keep_fps_filter_without_stats():
    assert detection_frame_output_args(6, None) == ['-vf', 'fps=6']


def test_frame_output_args_preserve_frame_times_and_report_them():
    args = detection_frame_output_args(6, 9)
    assert args[0] == '-vf' and args[1].startswith("select='isnan(prev_selected_t)")
    # fps=N would snap frames onto a synthetic grid and report the slot time.
    assert not any(arg.startswith('fps=') for arg in args)
    assert args[args.index('-fps_mode:v') + 1] == 'passthrough'
    assert args[args.index('-stats_mux_pre') + 1] == 'pipe:9'
    assert args[args.index('-stats_mux_pre_fmt') + 1] == '{t}'


def test_segment_list_is_capped_csv(tmp_path):
    stale = tmp_path / SEGMENT_LIST_NAME
    stale.write_text('segment-old.mp4,0.000000,500.000000\n', encoding='utf-8')
    args = segment_list_args(tmp_path)
    # A previous process's list (a different stream timeline) is discarded.
    assert not stale.exists()
    assert args[args.index('-segment_list') + 1] == str(tmp_path / SEGMENT_LIST_NAME)
    assert args[args.index('-segment_list_type') + 1] == 'csv'
    assert int(args[args.index('-segment_list_size') + 1]) > 0


def _send(clock, write_fd, stream_t):
    """Write one stats line and return the wall time the reader received it."""
    before = len(clock._rings['cam'])
    os.write(write_fd, f'{stream_t:.6f}\n'.encode())
    assert _wait_for(lambda: len(clock._rings['cam']) > before)
    return clock._rings['cam'][-1][1]


def test_stamp_learns_decode_lag_from_an_unambiguous_pair(tmp_path):
    now = time.time()
    # Newest closed segment ended at stream 8.0s, written at wall `now - 1.0`.
    clock, write_fd = _clock_with_segment(tmp_path, segment_end_stream_t=8.0, segment_mtime=now - 1.0)
    arrived = _send(clock, write_fd, 8.4)
    jpeg_mtime = arrived + 0.01  # JPEG lands just after its stats line
    # Stream 8.4s maps to wall (now - 1.0) + 0.4; the stamp is that capture time.
    assert clock.stamp('cam', jpeg_mtime) == pytest.approx(now - 0.6, abs=1e-3)
    assert clock.decode_lag('cam') == pytest.approx(jpeg_mtime - (now - 0.6), abs=1e-3)
    os.close(write_fd)


def test_stamp_ignores_a_pair_delayed_by_the_reader(tmp_path):
    # Review case: the reader wakes late, so the JPEG's own line arrives well
    # after the JPEG. Pairing by arrival would pick the previous frame's line;
    # the ambiguous frame must not shift the learned lag.
    now = time.time()
    clock, write_fd = _clock_with_segment(tmp_path, segment_end_stream_t=8.0, segment_mtime=now - 1.0)
    arrived = _send(clock, write_fd, 8.4)
    clock.stamp('cam', arrived + 0.01)
    learned = clock.decode_lag('cam')
    time.sleep(0.2)
    late_jpeg_mtime = time.time()
    time.sleep(0.15)  # reader delayed past the JPEG write
    _send(clock, write_fd, 8.6)
    stamped = clock.stamp('cam', late_jpeg_mtime)
    assert clock.decode_lag('cam') == pytest.approx(learned)
    assert stamped == pytest.approx(late_jpeg_mtime - learned)
    os.close(write_fd)


def test_stamp_rejects_implausible_lag(tmp_path):
    now = time.time()
    # A stale list (e.g. RTSP timestamps reset) would put the frame far in the past.
    clock, write_fd = _clock_with_segment(tmp_path, segment_end_stream_t=500.0, segment_mtime=now - 1.0)
    arrived = _send(clock, write_fd, 8.4)
    assert clock.stamp('cam', arrived + 0.01) == pytest.approx(arrived + 0.01)
    assert clock.decode_lag('cam') is None
    os.close(write_fd)


def test_stamp_without_stats_is_the_mtime(tmp_path):
    mtime = time.time()
    assert FrameCaptureClock().stamp('cam', mtime) == mtime


def test_stamp_never_goes_backwards_when_the_lag_is_learned(tmp_path):
    # Review case: frames stamped with the mtime before the first segment
    # closes, then the learned (earlier) time - history must stay ordered.
    now = time.time()
    clock, write_fd = _clock_with_segment(tmp_path, segment_end_stream_t=8.0, segment_mtime=now - 1.0)
    early = clock.stamp('cam', now)  # nothing learned yet: the mtime itself
    arrived = _send(clock, write_fd, 8.4)
    later = clock.stamp('cam', arrived + 0.01)  # learned: ~(now - 0.6), earlier
    assert early == now
    assert later >= early
    os.close(write_fd)


def test_latest_frame_falls_back_to_mtime_without_clock(tmp_path):
    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    frame = service.frames_dir / service._camera_key('cam') / 'latest.jpg'
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b'\xff\xd8jpeg')
    data, captured = service.latest_frame_jpeg('cam')
    assert data == b'\xff\xd8jpeg'
    assert captured == pytest.approx(frame.stat().st_mtime)


def test_ingest_wires_stats_pipe_and_segment_list_when_supported(tmp_path, monkeypatch):
    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(recordings_module, 'stream_frame_stats_supported', lambda _ffmpeg: True)
    stop = threading.Event()
    launches = []

    class _Proc:
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):  # noqa: ARG002
            return self.returncode

    def popen(cmd, stderr=None, pass_fds=(), **_kw):  # noqa: ARG001
        launches.append((list(cmd), tuple(pass_fds)))
        if '-i' in cmd:
            stop.set()
        return _Proc()

    monkeypatch.setattr(recordings_module.subprocess, 'Popen', popen)
    service._run_prebuffer_worker('cam', 'rtsp://example/stream', {
        'stop_event': stop, 'stream_url': 'rtsp://example/stream', 'buffer_seconds': 20, 'camera_id': 'cam',
    })
    # Other Popen calls are the decoder capability probes (-hwaccels, -L).
    (cmd, pass_fds), = [launch for launch in launches if '-i' in launch[0]]
    stats_target = cmd[cmd.index('-stats_mux_pre') + 1]
    assert stats_target == f'pipe:{pass_fds[0]}'
    # The list belongs to the prebuffer segment output (before its pattern).
    pattern_index = next(i for i, arg in enumerate(cmd) if arg.endswith('segment-%Y%m%dT%H%M%S.mp4'))
    assert cmd.index('-segment_list') < pattern_index
    assert not any(arg.startswith('fps=') for arg in cmd)


# ---------------------------------------------------------------------------
# End to end against a real ffmpeg: the same command shape as the ingest.
# ---------------------------------------------------------------------------

_FFMPEG = shutil.which('ffmpeg')
_FFPROBE = shutil.which('ffprobe')
_SOURCE_FPS = 15
_BITS = 9


def _frame_ids(args):
    raw = subprocess.run(
        [_FFMPEG, '-v', 'error', *args, '-vf', f'crop={_BITS * 40}:40:0:0,scale={_BITS}:1:flags=area',
         '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
        capture_output=True, check=True,
    ).stdout
    return [sum(1 << b for b in range(_BITS) if raw[i + b] > 128) for i in range(0, len(raw) - _BITS + 1, _BITS)]


@pytest.mark.skipif(
    not (_FFMPEG and _FFPROBE and stream_frame_stats_supported(_FFMPEG)),
    reason='needs ffmpeg 6.1+ with ffprobe',
)
def test_real_ffmpeg_frames_land_on_the_segment_clock(tmp_path):
    # Source with each frame's index burned in as a row of binary boxes.
    boxes = ''.join(
        f",drawbox=x={b * 40}:y=0:w=40:h=40:color=white:t=fill:enable='mod(floor(n/pow(2\\,{b}))\\,2)'"
        for b in range(_BITS)
    )
    source = tmp_path / 'src.mp4'
    subprocess.run(
        [_FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=size=640x360:rate={_SOURCE_FPS}', '-t', '14',
         '-vf', f'drawbox=x=0:y=0:w={_BITS * 40}:h=40:color=black:t=fill{boxes}',
         '-c:v', 'libx264', '-preset', 'veryfast', '-bf', '0', '-g', str(_SOURCE_FPS), '-pix_fmt', 'yuv420p',
         str(source)],
        check=True,
    )
    camera_dir = tmp_path / 'seg'
    camera_dir.mkdir()
    latest = tmp_path / 'latest.jpg'
    read_fd, write_fd = os.pipe()
    command = [
        _FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-re', '-i', str(source),
        '-map', '0:v:0', '-c:v', 'copy', '-an', '-f', 'segment', '-segment_time', '2', '-segment_format', 'mp4',
        '-segment_format_options', 'movflags=+frag_keyframe+empty_moov+default_base_moof',
        *segment_list_args(camera_dir), str(camera_dir / 'segment-%03d.mp4'),
        '-map', '0:v:0', *detection_frame_output_args(4, write_fd), '-q:v', '2',
        '-update', '1', '-atomic_writing', '1', '-f', 'image2', '-y', str(latest),
    ]
    process = subprocess.Popen(command, pass_fds=(write_fd,))
    os.close(write_fd)
    clock = FrameCaptureClock()
    clock.attach('cam', read_fd, camera_dir)
    captures = []  # (jpeg path copy, clock stamp, mtime)
    last_mtime = None
    while process.poll() is None:
        try:
            with open(latest, 'rb') as handle:
                mtime = os.fstat(handle.fileno()).st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    stamped = clock.stamp('cam', mtime)
                    lag = clock.decode_lag('cam')
                    # Skip the few frames held at the last mtime stamp while
                    # the switch to learned stamps catches up (no going back).
                    if lag is not None and stamped == pytest.approx(mtime - lag, abs=1e-6):
                        copy = tmp_path / f'cap-{len(captures):04d}.jpg'
                        copy.write_bytes(handle.read())
                        captures.append((copy, stamped, mtime))
        except OSError:
            pass
        time.sleep(0.003)
    assert len(captures) >= 10
    # Ground truth on the app's own clip clock: segment mtime - probed duration
    # + the frame's offset inside that segment.
    segments = []
    for path in sorted(glob.glob(str(camera_dir / 'segment-*.mp4')))[:-1]:
        duration = float(subprocess.run(
            [_FFPROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', path],
            capture_output=True, text=True, check=True,
        ).stdout)
        ids = _frame_ids(['-i', path])
        segments.append((Path(path).stat().st_mtime, duration, ids[0], ids[-1]))
    errors, decode_lags = [], []
    for copy, stamped, mtime in captures:
        (frame_id,) = _frame_ids(['-i', str(copy)])
        for segment_mtime, duration, first, last in segments:
            if first <= frame_id <= last:
                truth = segment_mtime - duration + (frame_id - first) / _SOURCE_FPS
                errors.append(stamped - truth)
                decode_lags.append(mtime - truth)
                break
    assert len(errors) >= 8
    errors.sort()
    # Within a couple of source frames of the truth, every sample.
    assert abs(errors[len(errors) // 2]) < 0.05, errors
    assert max(abs(e) for e in errors) < 0.15, errors
