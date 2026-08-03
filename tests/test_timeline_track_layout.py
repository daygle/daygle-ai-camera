"""Regression guards for the compact timeline track layout."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / 'web' / 'styles.css').read_text(encoding='utf-8')
JS = (ROOT / 'web' / 'timeline.js').read_text(encoding='utf-8')


def test_timeline_track_and_rows_use_one_row_minimum_height():
    assert ".timeline-track {\n  position: relative;\n  min-height: 46px;" in CSS
    assert ".timeline-rows {\n  position: relative;\n  min-height: 46px;" in CSS
    assert "card.rows.style.height = `${Math.max(46, rowCount * TIMELINE_ROW_HEIGHT)}px`;" in JS


def test_timeline_empty_state_matches_compact_track_height():
    assert ".timeline-empty {\n  display: grid;\n  place-items: center;\n  min-height: 46px;" in CSS
