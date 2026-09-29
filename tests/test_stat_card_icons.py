"""Regression guards for stat-card icon styling (System and Events pages)."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_stat_cards_use_the_shared_icon_style():
    css = (ROOT / 'web' / 'styles.css').read_text(encoding='utf-8')
    assert '.stat-card-icon-motion' not in css
    for page, minimum in (('system.html', 5), ('events.html', 3)):
        html = (ROOT / 'web' / page).read_text(encoding='utf-8')
        assert 'class="stat-card-icon stat-card-icon-motion"' not in html
        assert html.count('class="stat-card-icon"') >= minimum, page
