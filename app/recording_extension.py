"""Recording-extension cluster extracted from ``app/main.py`` (Phase-30).

This module owns the four helpers that previously lived inline on
``app/main.py``:

* ``extend_active_rtsp_recording(*, camera_id, event_time,
  recording_config=None, detections=None) -> int | None`` — extends
  an in-flight RTSP recording's capture deadline by the configured
  ``extension_step_seconds`` post-event horizon and updates the
  associated recording row (timing, labels, trigger).
* ``recording_track_sidecar_path(file_path: Path) -> Path`` —
  computes the ``.track.json`` sidecar path next to a recording file.
* ``write_recording_detection_track(file_path: Path, track:
  list[dict[str, Any]]) -> None`` — atomic-ish write of the
  sidecar JSON next to ``file_path``.
* ``load_recording_detection_track(file_path: Path) -> list[dict[str,
  Any]] | None`` — reads + parses the sidecar JSON if present,
  returning ``None`` on any missing-file / corrupt / non-detection
  shape.

The two pieces of exclusive state STAY on ``app.main`` (NOT moved
here, reachable via Pool C):

* ``app.main.active_rtsp_recordings`` — ``dict`` keyed by camera_id,
  holding ``{recording_id, capture_deadline_ts,
  max_capture_deadline_ts, start_capture_ts}`` per in-flight session.
* ``app.main.active_rtsp_recordings_lock`` — ``threading.Lock``
  guarding ``active_rtsp_recordings``.

Mirrors the Phase-26 (``live_detection_history`` /
``live_detection_history_lock``), Phase-27
(``_notification_threads_lock`` / ``_notification_threads``), and
Phase-29 (``live_detection_status_lock`` / ``live_detection_status``)
precedent for keeping state on ``app.main``.

**Pool-A rebind (in ``app/main.py``):** all four helpers are re-bound
as ``main.<orig_name>``. No underscore stripping needed — none of the
four are originally underscored.

**Pool-C reach sites used by this module (each resolved lazily via
``import app.main as main``):**

* ``main.active_rtsp_recordings`` + ``main.active_rtsp_recordings_lock``
  — state primitives (exclusive to ``extend_active_rtsp_recording``).
* ``main.database`` — the singleton DB handle, instantiated on
  ``app/main.py``. NOTE this is NOT a module-level export of
  ``app/database.py``; that module only exposes the ``EventDatabase``
  class.
* ``main.recording_service`` — the ``RecordingService`` singleton
  instantiated on ``app/main.py``. Used for ``should_record`` policy
  lookup.
* ``main.effective_recording_config`` — Phase-17 rebind from
  ``app.config_facades`` for the active recording config dict.
* ``main.detection_label_strings`` — Phase-29 rebind from
  ``app.detection_status`` for label dedup + sort.
* ``main.detection_label_confidences`` — Phase-29 rebind from
  ``app.detection_status`` for per-label max-confidence selection.

**Track trio Pool-C reach:** NONE. Pure helper group. Uses only
stdlib (``json``, ``pathlib.Path``, ``typing.Any``) and resolves
``recording_track_sidecar_path`` as a bare name inside the same
module.

**External callers that must keep working via Phase-30 rebind:**

* ``app/api/recordings_router.py`` — ``main.load_recording_detection_track``
  + ``main.recording_track_sidecar_path``.
* ``tests/test_api.py`` — extensive usage of all 4 helpers for
  recording-extension tests.
* ``web/app.js`` — comment reference (no functional dependency).

**Internal main.py body callers (use bare names after rebind):**

* ``extend_active_rtsp_recording(...)`` — called from L1149 (likely
  ``process_live_stream_alerts`` event-recording decision) and L1331
  (another recording-orchestration helper).
* ``recording_track_sidecar_path(...)`` — called from L1500 + the
  track trio internally.
* ``write_recording_detection_track(...)`` — called from L1589 (likely
  the main stream-loop detection track persistence).
* ``load_recording_detection_track(...)`` — called from various
  playback + recording-access sites.

**Generic-label-set duplication (recorded for follow-up):** the
``generic_labels = {'', 'motion', 'alert', 'human', 'object', 'none',
'off', 'continuous'}`` set inside ``extend_active_rtsp_recording`` is
near-duplicate of the ``generic`` set inside
``app.detection_status.detection_label_strings``. Per Phase-29's
reviewer-ship verdict, this is a known cosmetic duplication; defer to a
future shared-constants cleanup.

**Logger acquisition:** owns its own child logger via
``logging.getLogger('daygle.ai')`` (matches Phase-25/26/27/28/29
precedent; same logging tree).

**Default-arg safety:** no function-default expression evaluates
``main.<name>`` at module-load time. All defaults are scalars
(``None`` / ``[]``). Safe to import while main.py is partially loaded.

**Skipped helpers (NOT moved):** ``schedule_live_camera_backoff``
remains in ``main.py`` for future Phase-31 (related to Phase-27
event_debounce), as do the rest of the detection-status neighbors now
that Phase-29 has shipped.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import app.main as main

logger = logging.getLogger('daygle.ai')


def extend_active_rtsp_recording(
    *,
    camera_id: str,
    event_time: str,
    recording_config: dict[str, Any] | None = None,
    detections: list[dict[str, Any]] | None = None,
) -> int | None:
    try:
        event_dt = datetime.fromisoformat(str(event_time))
    except ValueError:
        event_dt = datetime.now(timezone.utc)
    if event_dt.tzinfo is None:
        event_dt = event_dt.replace(tzinfo=timezone.utc)
    config = recording_config or main.effective_recording_config()
    extension_step_seconds = max(
        0,
        int(config.get('extension_step_seconds', config.get('post_event_seconds', 10))),
    )
    extend_until = event_dt.timestamp() + extension_step_seconds
    with main.active_rtsp_recordings_lock:
        session = main.active_rtsp_recordings.get(camera_id)
        if not session:
            return None
        current_deadline = float(session.get('capture_deadline_ts') or 0)
        max_deadline = float(session.get('max_capture_deadline_ts') or current_deadline)
        new_deadline = min(max_deadline, max(current_deadline, extend_until))
        if new_deadline <= current_deadline:
            return int(session.get('recording_id'))
        session['capture_deadline_ts'] = new_deadline
        start_ts = float(session.get('start_capture_ts') or new_deadline)
        ended_at = datetime.fromtimestamp(new_deadline, tz=timezone.utc).isoformat()
        duration_seconds = max(1.0, new_deadline - start_ts)
        recording_id = int(session.get('recording_id'))
    main.database.update_recording_timing(
        recording_id, ended_at=ended_at, duration_seconds=duration_seconds,
    )
    if detections:
        should_record, trigger_type, trigger_label = main.recording_service.should_record(
            detections, config,
        )
        new_labels = main.detection_label_strings(detections)
        if new_labels:
            main.database.add_recording_labels(
                recording_id,
                new_labels,
                source='extension',
                confidences=main.detection_label_confidences(detections),
            )
        if should_record and trigger_label:
            current_recording = main.database.get_recording(recording_id) or {}
            current_label = str(current_recording.get('trigger_label') or '').strip().lower()
            current_type = str(current_recording.get('trigger_type') or '').strip().lower()
            generic_labels = {
                '', 'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous',
            }
            candidate_label = str(trigger_label).strip().lower()
            if (
                candidate_label not in generic_labels
                and (current_label in generic_labels or current_type in {'motion', 'human'})
            ):
                main.database.update_recording_trigger(
                    recording_id, trigger_type=trigger_type, trigger_label=candidate_label,
                )
    return recording_id


def recording_track_sidecar_path(file_path: Path) -> Path:
    return file_path.with_name(f'{file_path.stem}.track.json')


def write_recording_detection_track(
    file_path: Path,
    track: list[dict[str, Any]],
) -> None:
    recording_track_sidecar_path(file_path).write_text(
        json.dumps(track),
        encoding='utf-8',
    )


def load_recording_detection_track(
    file_path: Path,
) -> list[dict[str, Any]] | None:
    sidecar = recording_track_sidecar_path(file_path)
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    if not any((isinstance(sample, dict) and sample.get('detections') for sample in data)):
        return None
    return data
