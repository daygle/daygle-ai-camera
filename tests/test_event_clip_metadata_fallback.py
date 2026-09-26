"""Regression tests for the event-clip metadata fallback JSON crash.

Detection dicts carry internal, tuple-keyed memo caches
(``_zone_match_memo`` / ``_rule_match_memo``) stamped on by the live pipeline.
When neither clip encoder is available (no ffmpeg, no OpenCV),\n``RecordingService.write_event_clip`` falls back to writing a ``.meta.json``
sidecar -- and ``json.dumps`` rejects a tuple dict-key, so the fallback crashed
with ``TypeError`` instead of persisting anything: exactly the path that runs
when the clip could not be encoded.

These tests pin the contract that the callers sanitise detections through
``detection_status.json_safe_detections`` (drop every ``_``-prefixed internal
key) before handing them to the clip writer, so the metadata sidecar is always
written and is always parseable JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.detection_status import json_safe_detections
from app.recordings import RecordingService


def _memo_bearing_detections() -> list[dict]:
    """A detection row as the live pipeline hands it around mid-cycle."""
    return [{
        'label': 'person',
        'confidence': 0.91,
        'box': {'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.5},
        'alert_triggered': True,
        'alert_matched': True,
        '_zone_match_memo': {('full-frame', 0.1, 0.1, 0.3, 0.5): True},
        '_rule_match_memo': {(123456, 'alert', 'person', 0.91, 0.1, 0.1, 0.3, 0.5): ['Front Door']},
    }]


def test_json_safe_detections_strips_internal_fields_without_mutating():
    detections = _memo_bearing_detections()
    cleaned = json_safe_detections(detections)
    assert cleaned[0]['label'] == 'person'
    assert cleaned[0]['box'] == detections[0]['box']
    assert '_zone_match_memo' not in cleaned[0]
    assert '_rule_match_memo' not in cleaned[0]
    # The caller's live dicts are left untouched (shallow copies only).
    assert '_zone_match_memo' in detections[0]
    # The result must survive strict JSON serialisation.
    json.dumps(cleaned)


def test_event_clip_metadata_fallback_is_json_safe_with_memo_fields(tmp_path, monkeypatch):
    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'rec')},
        'recording': {},
    })

    def _no_ffmpeg(*_args, **_kwargs):
        raise RuntimeError('ffmpeg is not installed.')

    def _no_opencv(*_args, **_kwargs):
        raise RuntimeError("No module named 'cv2'")

    monkeypatch.setattr(service, '_write_ffmpeg_placeholder_clip', _no_ffmpeg)
    monkeypatch.setattr(service, '_write_opencv_clip', _no_opencv)

    metadata = service.event_recording_metadata(
        7,
        '2026-09-26T09:00:00+00:00',
        'rtsp',
        _memo_bearing_detections(),
    )

    assert metadata is not None
    meta_path = Path(str(metadata['file_path']) + '.meta.json')
    assert meta_path.exists()
    payload = json.loads(meta_path.read_text(encoding='utf-8'))
    assert payload['event_id'] == 7
    assert payload['trigger_type'] == 'alert'
    assert payload['trigger_label'] == 'person'
    row = payload['detections'][0]
    assert row['label'] == 'person'
    assert '_zone_match_memo' not in row
    assert '_rule_match_memo' not in row
    # No clip file is left behind in place of the failed encode.
    assert not Path(metadata['file_path']).exists()
