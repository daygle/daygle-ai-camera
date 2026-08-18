"""Regression guards for mixed motion/object rendering across activity pages."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_web(name: str) -> str:
    return (ROOT / "web" / name).read_text(encoding="utf-8")


def test_events_and_snapshots_keep_motion_pill_with_object_pills():
    for name in ("events.js", "snapshots.js"):
        source = read_web(name)
        assert "const motionBadge = motionDetections.length ? motionPill" in source
        assert "objectDetections.map((d) => detectionPill" in source
        assert "return `${motionBadge}${objectDetections.map" in source


def test_recordings_keep_motion_pill_on_mixed_object_clips():
    source = read_web("recordings.js")
    html = read_web("recordings.html")
    assert "window.daygleUi.recordingHasMotion(recording)" in source
    assert "const motionBadge = !isSound && hasRecordingMotion(recording)" in source
    assert "const objectBadges = detections.map((d) => detectionPill" in source
    assert "motionConfidenceFor(recording) !== null" in source
    assert "/static/utils.js?v=recordings-motion-fallback-1" in html
    assert "/static/recordings.js?v=recordings-motion-fallback-1" in html


def test_shared_ui_registry_preserves_nav_helpers_when_utils_loads_after_nav():
    utils_source = read_web("utils.js")
    nav_source = read_web("nav.js")
    assert "...(window.daygleUi || {})," in utils_source
    assert "window.daygleUi = Object.assign(window.daygleUi || {}," in nav_source


def test_timeline_has_a_separate_motion_only_card_and_partition():
    html = read_web("timeline.html")
    source = read_web("timeline.js")
    assert 'data-timeline-card="motion"' in html
    assert "{ kind: 'motion'" in source
    assert "const motionRecordings = recordings.filter((r) => !isSoundRecording(r) && isMotionOnlyRecording(r));" in source
    assert "card.kind === 'motion'" in source
    assert "add(motionChips, '__motion__'" in source
    assert "const chips = card.kind === 'motion' ? motionChips" in source


def test_timeline_object_card_excludes_motion_only_clips():
    source = read_web("timeline.js")
    # The Objects bucket excludes sound, motion-only AND continuous-only clips so
    # each kind renders on its own dedicated card without being duplicated.
    assert "const objectRecordings = recordings.filter((r) => !isSoundRecording(r) && !isMotionOnlyRecording(r) && !isContinuousOnlyRecording(r));" in source
    assert "if (normalized === '__object__') return !isSoundRecording(recording) && !isMotionOnlyRecording(recording) && !isContinuousOnlyRecording(recording);" in source


def test_timeline_has_a_separate_continuous_card_and_partition():
    html = read_web("timeline.html")
    source = read_web("timeline.js")
    assert 'data-timeline-card="continuous"' in html
    assert "{ kind: 'continuous'" in source
    assert "const continuousRecordings = recordings.filter((r) => isContinuousOnlyRecording(r));" in source
    assert "card.kind === 'continuous' ? continuousRecordings" in source
    assert "add(continuousChips, '__continuous__'" in source
    assert "card.kind === 'continuous' ? continuousChips" in source
