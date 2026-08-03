"""Regression guards for playback-card header action placement."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBACK_PAGES = (
    ROOT / 'web' / 'index.html',
    ROOT / 'web' / 'alerts.html',
    ROOT / 'web' / 'recordings.html',
    ROOT / 'web' / 'timeline.html',
)


def test_overlay_toggle_is_before_download_in_each_playback_header():
    for path in PLAYBACK_PAGES:
        source = path.read_text(encoding='utf-8')
        header_start = source.index('<div class="button-row clip-player-actions">')
        header_end = source.index('</div>', header_start)
        action_row = source[header_start:header_end]
        assert action_row.index('clip-overlay-toggle') < action_row.index('videoModalDownload')
        assert 'clip-overlay-controls' not in source


def test_playback_header_action_styles_remove_legacy_overlay_spacing():
    css = (ROOT / 'web' / 'styles.css').read_text(encoding='utf-8')
    assert '.clip-player-actions' in css
    assert '.clip-player-actions .clip-overlay-toggle' in css
    assert 'margin: 0 0 0 auto;' in css
