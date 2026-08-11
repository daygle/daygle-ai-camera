"""Regression guards for dashboard mixed motion/object detection pills."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_mixed_events_use_motion_pill_for_motion_labels():
    source = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')

    assert "String(label).toLowerCase() === 'motion'" in source
    assert ' ? motionPill(conf)' in source
    assert ': detectionPill(label, conf, isSound)' in source
