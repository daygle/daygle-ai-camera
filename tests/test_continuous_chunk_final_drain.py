"""Regression tests for Bug 3: the continuous chunk worker must drain
``.segment_list.txt`` once after ``process.wait`` returns so the chunk
ffmpeg finalised during SIGTERM-driven graceful shutdown reaches
``on_chunk_complete``.

Previously the inner polling loop exited the instant ``stop_event``
fired, so any chunk finalised during ffmpeg's SIGTERM teardown was
orphaned on disk with no ``recordings`` row claiming it - the callback
never fired, the database never learned about it, and the file sat in
the chunks directory as a dead artifact.
"""

from __future__ import annotations

import threading
import time


def test_continuous_chunk_worker_drains_final_chunk_after_graceful_shutdown(
    tmp_path, monkeypatch
):
    """Bug 3 regression: ``on_chunk_complete`` fires for the chunk
    ffmpeg wrote during SIGTERM teardown, even though the inner polling
    loop bailed out the moment ``stop_event`` fired.

    The mock Popen simulates ffmpeg finalising the open chunk at
    ``terminate()`` time (the entry appears in ``.segment_list.txt``
    once the SIGTERM handler runs) and then exiting cleanly so
    ``process.wait`` returns 0. The worker is expected to drain the
    list file once more after that ``wait`` returns and invoke the
    callback for the new entry.
    """
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService(
        {
            "storage": {"recordings_dir": str(tmp_path / "rec")},
            "recording": {},
        }
    )
    monkeypatch.setattr(recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    camera_key = RecordingService._camera_key("cam")
    chunks_dir = service.recordings_dir / f"continuous-{camera_key}"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    # Pre-create the final chunk on disk so the "exists + size > 0"
    # check inside ``_drain_chunk_list`` accepts it. The worker reaches
    # this check after ``terminate()`` writes the list-file entry.
    final_chunk_name = f"continuous_{camera_key}_20260101T000000.mp4"
    final_chunk = chunks_dir / final_chunk_name
    final_chunk.write_bytes(b"ts-bytes")

    list_file = chunks_dir / ".segment_list.txt"

    class FakeProc:
        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> int | None:
            # Always running during the inner loop; the loop bails out via
            # stop_event.is_set(), not via poll(). Returning None keeps
            # the inner loop iterating until stop_event fires.
            return None

        def terminate(self) -> None:
            # Real ffmpeg on SIGTERM flushes the open segment and appends
            # it to ``-segment_list`` before exiting. Simulate the
            # post-terminate list-file state so the post-wait drain sees
            # and announces the entry.
            self.terminated = True
            list_file.write_text(
                f"{final_chunk_name}\n",
                encoding="utf-8",
            )

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - mock
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(recordings_module.subprocess, "Popen", lambda cmd, **_kw: FakeProc())

    stop_event = threading.Event()
    captured: list[tuple[str, str]] = []
    on_chunk_complete = lambda key, path: captured.append((key, str(path)))

    worker_thread = threading.Thread(
        target=service._run_continuous_chunk_worker,
        args=(
            camera_key,
            "rtsp://example/stream",
            chunks_dir,
            60,
            on_chunk_complete,
            stop_event,
        ),
        daemon=True,
        name=f"continuous-recorder-test-{camera_key}",
    )
    worker_thread.start()

    # Warm-up: wait long enough for the worker thread to reach the outer
    # ``while not stop_event.is_set():`` loop and call ``Popen``. Without
    # it, a scheduler that preempts the worker between the function entry
    # and the outer-while condition check could see ``stop_event`` already
    # set on its first iteration and exit without entering the inner
    # try/finally at all - which would let this test pass on an empty run
    # instead of exercising the post-process.wait drain this regression
    # exists to cover.
    time.sleep(0.2)
    stop_event.set()
    worker_thread.join(timeout=10)

    assert not worker_thread.is_alive(), "worker thread should have exited within 10s of stop_event"

    assert captured == [(camera_key, str(final_chunk))], (
        f"final chunk callback should fire after process.wait during graceful "
        f"shutdown; captured={captured}, list_file_on_disk={list_file.read_text()!r}"
    )
    # And the list-file state should now reflect the consumed entry
    # (``seen_count`` would have advanced past it).
    assert list_file.read_text(encoding="utf-8").strip() == final_chunk_name
