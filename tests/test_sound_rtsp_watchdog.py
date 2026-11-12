"""RTSP sound capture must not leave an endless redirect loop running."""

from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import app.sound_detector as sound_detector


class RtspSoundWatchdogTests(unittest.TestCase):
    def _run_capture(self, child_code: str, *, stall_seconds: float, stop_on_audio: bool):
        detector = sound_detector.SoundDetector(
            on_detect=lambda *_args: None,
            rules=[], source='rtsp', rtsp_url='rtsp://camera/stream', sample_duration_seconds=0.01,
        )
        children = []
        received = threading.Event()
        original_popen = subprocess.Popen

        def start_child(_command, **kwargs):
            child = original_popen([sys.executable, '-u', '-c', child_code], **kwargs)
            children.append(child)
            # Stop retries as soon as the test child exits.
            threading.Thread(target=lambda: (child.wait(), detector._stop_event.set()), daemon=True).start()
            return child

        def audio_received(_chunk):
            received.set()
            if stop_on_audio:
                detector._stop_event.set()

        with patch.object(sound_detector.shutil, 'which', return_value='ffmpeg'), \
             patch.object(sound_detector.subprocess, 'Popen', side_effect=start_child), \
             patch.object(sound_detector._yamnet, 'preload'), \
             patch.object(sound_detector, 'RTSP_AUDIO_STALL_SECONDS', stall_seconds), \
             patch.object(detector, '_handle_chunk', side_effect=audio_received):
            worker = threading.Thread(target=detector._run_rtsp, daemon=True)
            worker.start()
            try:
                worker.join(timeout=4)
                self.assertFalse(worker.is_alive(), 'sound capture failed to stop')
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                return received.is_set()
            finally:
                detector._stop_event.set()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=2)
                worker.join(timeout=2)

    def test_stalled_capture_is_killed_and_reaped(self):
        self.assertFalse(self._run_capture('import time; time.sleep(30)', stall_seconds=0.1, stop_on_audio=False))

    def test_audio_is_delivered_before_capture_is_stopped(self):
        code = "import sys, time; sys.stdout.buffer.write(bytes(320)); sys.stdout.buffer.flush(); time.sleep(30)"
        self.assertTrue(self._run_capture(code, stall_seconds=3, stop_on_audio=True))


if __name__ == '__main__':
    unittest.main()
