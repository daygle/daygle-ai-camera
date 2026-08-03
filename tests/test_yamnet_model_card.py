"""Structural guards for the YAMNet model card redesign."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'web' / 'yamnet-tflite.html').read_text(encoding='utf-8')
JS = (ROOT / 'web' / 'yamnet-tflite.js').read_text(encoding='utf-8')
CSS = (ROOT / 'web' / 'styles.css').read_text(encoding='utf-8')


def test_yamnet_model_card_uses_modern_layout_hooks():
    for token in (
        'yamnet-model-card',
        'yamnet-model-section-header',
        'yamnet-model-engine-badge',
        'yamnet-model-info',
        'yamnet-model-actions',
    ):
        assert token in HTML
        assert token in CSS


def test_yamnet_update_button_starts_hidden_and_uses_hidden_state():
    assert 'id="reloadYamnetModelBtn" type="button" hidden' in HTML
    assert 'reloadYamnetModelBtn.hidden = false' in JS
    assert 'reloadYamnetModelBtn.hidden = true' in JS
    assert 'style="display:none"' not in HTML


def test_yamnet_model_values_are_escaped_before_inner_html_interpolation():
    assert "const sizeText = info.model_size ? escapeHtml(formatBytes(info.model_size))" in JS
    assert "const hashText = info.sha256 ? escapeHtml(info.sha256)" in JS
    assert "const installedAt = info.installed_at ? escapeHtml(new Date(info.installed_at).toLocaleDateString())" in JS
