"""Regression guards for Cameras page summary cards."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_web(name: str) -> str:
    return (ROOT / "web" / name).read_text(encoding="utf-8")


def test_cameras_removes_ptz_summary_card_but_keeps_ptz_configuration():
    html = read_web("cameras.html")
    source = read_web("cameras.js")

    assert "PTZ Enabled" not in html
    assert "statPtzCameras" not in html
    assert "ptz_enabled" in source
    assert "PTZ Control" in source
    assert "statPtzCameras" not in source
