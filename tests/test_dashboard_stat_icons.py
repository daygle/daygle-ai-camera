"""Regression guards for dashboard stat-card icon styling."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_motion_stat_icon_uses_shared_dashboard_icon_style():
    html = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    css = (ROOT / 'web' / 'styles.css').read_text(encoding='utf-8')

    assert 'class="stat-card-icon stat-card-icon-motion"' not in html
    assert '.stat-card-icon-motion' not in css
    assert html.count('class="stat-card-icon"') >= 6
