"""Regression guards for the Events page statistics cards."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_web(name: str) -> str:
    return (ROOT / "web" / name).read_text(encoding="utf-8")


def test_events_replaces_total_card_with_motion_card():
    html = read_web("events.html")
    source = read_web("events.js")

    assert "Motion Events" in html
    assert 'id="statMotionEvents"' in html
    assert "Total Events" not in html
    assert 'getElementById(\'statMotionEvents\')' in source
    assert "if (kind === 'motion') motion += 1;" in source
    assert "els.statMotionEvents.textContent = String(motion)" in source
    assert "statTotalEvents" not in source
