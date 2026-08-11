"""Regression guards for live Vision confidence rendering."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_live_header_does_not_repeat_detection_items():
    source = (ROOT / 'web' / 'live.js').read_text(encoding='utf-8')

    assert "liveEls.detectionSubtitle.textContent = 'What the AI is currently seeing and hearing on the live feed.';" in source
    assert 'parts.push(`Seeing:' not in source
    assert 'parts.push(`Hearing:' not in source


def test_live_vision_uses_the_same_confidence_row_as_hearing():
    source = (ROOT / 'web' / 'live.js').read_text(encoding='utf-8')

    # Both lanes must render through the shared row builder, and Vision must
    # retain backend confidence hints when a status update only has labels.
    assert "liveEls.visionBody.innerHTML = objChips.map((c) => detectionRowHtml(c.label, c.confidence" in source
    assert "liveEls.hearingBody.innerHTML = soundChips.map((c) => detectionRowHtml(c.label, c.confidence" in source
    assert 'const confidenceHints = payload.detection_confidences || {};' in source
    assert "objectReason?.code === 'below_threshold'" in source
    assert 'below alert threshold' in source
    assert 'const hasPct = conf != null;' in source
