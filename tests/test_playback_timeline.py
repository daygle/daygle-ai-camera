"""Regression guards for playback timeline event visibility."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBACK_SCRIPTS = (
    ROOT / 'web' / 'app.js',
    ROOT / 'web' / 'recordings.js',
    ROOT / 'web' / 'timeline.js',
)


def test_single_sample_event_keeps_a_visible_green_segment():
    """A one-sample motion track must not render Event as 0.0s/zero width."""
    for path in PLAYBACK_SCRIPTS:
        source = path.read_text(encoding='utf-8')
        assert 'const minimumEventSpan = Math.min(1, duration);' in source
        assert 'first + minimumEventSpan' in source
