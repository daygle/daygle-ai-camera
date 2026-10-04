"""Regression guards for playback timeline event visibility.

Rendering behaviour is covered by tests/test_clip_timeline.test.js; these
guards pin the shape of the fix in the shared script.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# The clip timeline is shared by the recordings and timeline pages.
CLIP_TIMELINE = ROOT / 'web' / 'clip_timeline.js'


def test_single_sample_event_stays_visible_without_an_invented_span():
    """A one-sample track must stay visible on the bar (a hairline), but must
    not be inflated into a fake duration such as "Event 1.0s"."""
    source = CLIP_TIMELINE.read_text(encoding='utf-8')
    assert 'minimumEventSpan' not in source
    assert 'clip-seg-event-single' in source
    assert "'Event: single detection'" in source
    styles = (ROOT / 'web' / 'styles.css').read_text(encoding='utf-8')
    assert '.clip-seg-event-single' in styles


def test_both_playback_pages_load_the_shared_clip_timeline_before_their_script():
    for page, script in (('recordings.html', 'recordings.js'), ('timeline.html', 'timeline.js')):
        html = (ROOT / 'web' / page).read_text(encoding='utf-8')
        shared = html.index('/static/clip_timeline.js')
        assert shared < html.index(f'/static/{script}'), page
