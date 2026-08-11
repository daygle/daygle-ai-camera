"""Regression guards for consistent playback overlay timing."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBACK_SCRIPTS = (
    ROOT / 'web' / 'app.js',
    ROOT / 'web' / 'recordings.js',
    ROOT / 'web' / 'timeline.js',
)


def test_playback_fallback_overlays_are_trigger_time_gated_everywhere():
    for path in PLAYBACK_SCRIPTS:
        source = path.read_text(encoding='utf-8')
        assert 'function detectionAnchorSeconds(recording)' in source
        assert 'function shouldRenderOverlayForTime(recording, playerTimeSeconds)' in source
        assert 'if (!shouldRenderOverlayForTime(activeRecording, playerTime)) return;' in source


def test_motion_history_keeps_all_firing_zones_for_playback():
    source = (ROOT / 'app' / 'live_monitor.py').read_text(encoding='utf-8')
    assert "for motion in motion_detections" in source
    assert "'motion_event': True" in source
