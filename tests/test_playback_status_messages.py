"""Regression guards for concise playback-card status messaging."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBACK_FILES = (
    ROOT / 'web' / 'app.js',
    ROOT / 'web' / 'alerts.js',
    ROOT / 'web' / 'recordings.js',
    ROOT / 'web' / 'timeline.js',
)


def test_playback_cards_clear_status_after_successful_playback():
    for path in PLAYBACK_FILES:
        source = path.read_text(encoding='utf-8')
        assert 'Playing recording #' not in source
        assert "els.clipPlayerStatus.textContent = '';" in source


def test_playback_cards_keep_preparation_loading_and_error_feedback():
    for path in PLAYBACK_FILES:
        source = path.read_text(encoding='utf-8')
        assert 'is still being prepared' in source
        assert 'Loading recording #' in source
        assert 'Unable to play recording #' in source
