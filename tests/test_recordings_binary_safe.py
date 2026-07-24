"""Regression test for the Windows os.open text-mode truncation fix.

Locks down `RecordingService.latest_frame_jpeg` so it cannot regress to
``os.open(str(path), os.O_RDONLY)`` on Windows - where MSVCRT's _open
defaults to text mode and reads STOP at the first 0x1A (Ctrl-Z) byte
interpreted as EOF. JPEG scan-data bytes routinely include 0x1A in the
first few hundred bytes, so without O_BINARY the text-mode read silently
truncated the stream and cv2.imdecode returned None, breaking
read_ingest_frame (the live detection / snapshot read path) on every
Windows deployment.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.recordings import RecordingService  # noqa: E402  (import after sys.path bootstrap)


def test_latest_frame_jpeg_reads_binary_safely(tmp_path):
    """Synthesized payload with an early 0x1A forces any text-mode os.open
    read to truncate. After the O_BINARY fix, the round-tripped bytes must
    match the bytes that were written.

    POSIX opens in binary mode by default so this test passes naturally
    there; on Windows the test fails loudly without the fix.
    """
    service = RecordingService(
        {'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}}
    )
    key = RecordingService._camera_key('cam1')
    frames_dir = service.frames_dir / key
    frames_dir.mkdir(parents=True, exist_ok=True)
    # 0x1A at byte 5 is the truncation trigger. Anything past it is filler
    # so the file is large enough to look like a real JPEG buffer.
    payload = b'\xff\xd8\xff\xe0\x1aJUNK_HIT_AT_BYTE_5\xff\xd9' + b'\x00' * 600
    # Self-check: if a future edit shortens the payload below the typical
    # truncation point, the test would silently stop exercising the Windows
    # bug. Lock the trigger byte in place so that can't happen unnoticed.
    assert b'\x1a' in payload[:600], 'payload no longer triggers Windows text-mode truncation'

    (frames_dir / 'latest.jpg').write_bytes(payload)

    got = service.latest_frame_jpeg('cam1')

    assert got is not None, 'O_BINARY missing: read returned None (text-mode EOF)'
    returned_bytes, _mtime = got
    assert returned_bytes == payload, (
        f'O_BINARY missing: text-mode read truncated at 0x1A '
        f'(got {len(returned_bytes)} of {len(payload)} bytes)'
    )
