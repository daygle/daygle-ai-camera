"""Regression guards for concise playback-card status messaging."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBACK_FILES = (
    ROOT / 'web' / 'recordings.js',
    ROOT / 'web' / 'timeline.js',
)


def test_playback_messages_are_popups_not_an_inline_status_line():
    for path in PLAYBACK_FILES:
        source = path.read_text(encoding='utf-8')
        assert 'Playing recording #' not in source
        assert 'clipPlayerStatus' not in source
        assert "window.showToast?.(`Loading recording #" in source
    for page in ('recordings.html', 'timeline.html'):
        assert 'clipPlayerStatus' not in (ROOT / 'web' / page).read_text(encoding='utf-8')


def test_playback_cards_keep_preparation_loading_and_error_feedback():
    for path in PLAYBACK_FILES:
        source = path.read_text(encoding='utf-8')
        assert 'is still being prepared' in source
        assert 'Loading recording #' in source
        assert 'Unable to play recording #' in source
