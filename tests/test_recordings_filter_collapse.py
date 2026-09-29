"""Regression guards for the collapsible recordings filter panel."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'web' / 'recordings.html'
SCRIPT = ROOT / 'web' / 'recordings.js'
STYLES = ROOT / 'web' / 'styles.css'


def test_toggle_button_controls_the_filter_form():
    html = PAGE.read_text(encoding='utf-8')
    assert 'id="recordingsFilterToggle"' in html
    assert 'aria-controls="recordingsFilterForm"' in html
    # Starts collapsed: the attribute and the form's hidden state must agree,
    # or the panel flashes open before the script runs.
    assert 'id="recordingsFilterToggle" class="secondary recordings-filter-toggle" type="button" aria-expanded="false"' in html
    form = html[html.index('<form id="recordingsFilterForm"'):]
    assert form[:form.index('>')].endswith('hidden')


def test_filter_panel_toggles_and_persists():
    js = SCRIPT.read_text(encoding='utf-8')
    assert 'els.filterForm.hidden = !open;' in js
    assert "setAttribute('aria-expanded', String(open))" in js
    assert 'RECORDINGS_FILTER_PANEL_KEY' in js
    # Guarded storage access, like every other localStorage read on this page.
    assert 'try { localStorage.setItem(RECORDINGS_FILTER_PANEL_KEY' in js


def test_active_filter_count_reaches_the_collapsed_button():
    js = SCRIPT.read_text(encoding='utf-8')
    assert 'updateFilterPanelBadge(activeFilters.length)' in js
    assert "classList.toggle('is-filtered', count > 0)" in js
    assert 'els.filterBadge.hidden = !count;' in js


def test_deep_linked_filters_reopen_the_panel():
    js = SCRIPT.read_text(encoding='utf-8')
    assert 'params.get(\'label\')' in js
    assert "setFilterPanelOpen(deepLinked || saved === '1', { persist: false })" in js


def test_caret_flips_with_the_disclosure_state():
    css = STYLES.read_text(encoding='utf-8')
    assert '.recordings-filter-toggle[aria-expanded="true"] .recordings-filter-caret' in css
    assert 'transform: rotate(180deg);' in css
