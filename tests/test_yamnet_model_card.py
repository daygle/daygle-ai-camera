"""Structural guards for the Sound Model page (yamnet-tflite.html).

The page is one library card, like Object/Face Models: a status strip, the
single YAMNet model card and ONE notice. The status and the model used to sit
in two cards that could contradict each other (a red "runtime missing" panel
above an amber "will download automatically" card).
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'web' / 'yamnet-tflite.html').read_text(encoding='utf-8')
JS = (ROOT / 'web' / 'yamnet-tflite.js').read_text(encoding='utf-8')
CSS = (ROOT / 'web' / 'styles.css').read_text(encoding='utf-8')


def test_page_is_named_after_its_tab():
    assert '<title>Sound Model - Daygle AI Camera</title>' in HTML
    assert '<h1>Sound Model</h1>' in HTML


def test_one_library_card_holds_status_model_and_actions():
    card = HTML[HTML.index('id="soundModelCard"'):HTML.index('</section>', HTML.index('id="soundModelCard"'))]
    for element_id in (
        'yamnetMessage',
        'soundStatusPanel',
        'refreshSoundStatusBtn',
        'checkYamnetUpdateBtn',
        'yamnetModelCard',
        'yamnetModelInfo',
        'reloadYamnetModelBtn',
    ):
        assert f'id="{element_id}"' in card, element_id
    assert HTML.count('<section class="card') == 1
    for token in ('yamnet-notice', 'yamnet-model-details', 'yamnet-model-hash'):
        assert token in CSS


def test_update_controls_start_hidden_and_use_hidden_state():
    assert 'id="reloadYamnetModelBtn" class="btn-warning model-action-btn" type="button" hidden' in HTML
    assert 'id="checkYamnetUpdateBtn" class="btn-info model-update-check-btn" type="button" hidden' in HTML
    assert 'reloadYamnetModelBtn.hidden = false' in JS
    assert 'reloadYamnetModelBtn.hidden = true' in JS
    # Nothing to compare against until a model is installed.
    assert "checkYamnetUpdateBtn.hidden = model !== 'active' && model !== 'installed'" in JS
    assert 'style="display:none"' not in HTML


def test_status_and_model_render_from_one_snapshot_with_one_notice():
    assert 'const snapshot = {' in JS
    assert 'function overallState()' in JS
    assert 'renderNotice(state.notice)' in JS
    # The no-cameras case is explained, not shown as an error.
    assert 'No camera is listening for sounds yet' in JS


def test_yamnet_model_values_are_escaped_before_inner_html_interpolation():
    assert "const hashText = info.sha256 ? escapeHtml(info.sha256) : '';" in JS
    assert "const installedAt = info.installed_at ? escapeHtml(new Date(info.installed_at)" in JS
    assert 'bodyHtml: escapeHtml(reason)' in JS
    assert 'bodyHtml: escapeHtml(error)' in JS
