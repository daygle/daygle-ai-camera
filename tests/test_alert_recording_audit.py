"""Audit fixes: one alert per rule and label, SMTP batches survive a refused
recipient, extensions record every label, relabel notification wording."""
from __future__ import annotations

import smtplib
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# One alert per rule and label
# ---------------------------------------------------------------------------

class _TwoPeopleDetector:
    backend = 'onnx'
    available = True
    unavailable_reason = None

    def detect_frame(self, image, confidence=None):
        return [
            {'label': 'person', 'confidence': 0.7, 'box': {'x': 0.1, 'y': 0.2, 'width': 0.1, 'height': 0.3}},
            {'label': 'person', 'confidence': 0.9, 'box': {'x': 0.6, 'y': 0.2, 'width': 0.1, 'height': 0.3}},
        ]


def test_two_people_in_one_frame_send_one_alert(tmp_path, monkeypatch):
    np = pytest.importorskip('numpy')
    pytest.importorskip('cv2')
    from tests.test_live_track_ordering import _load_app

    main = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as _lm

    monkeypatch.setattr(main._state, 'detector', _TwoPeopleDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))
    monkeypatch.setattr(_lm, 'filter_detections_by_motion_mode', lambda detections, *a, **k: detections)
    sent: list[list[dict]] = []
    monkeypatch.setattr(_lm, 'submit_alert_notification_with_verification',
                        lambda notifying, *_a, **_k: sent.append(list(notifying)) or True)
    camera = {
        'id': 'camera-gate', 'name': 'Gate',
        'detection': {'object_detection_enabled': True, 'zones': [{
            'id': 'gate', 'name': 'Gate', 'enabled': True, 'monitor_objects': True,
            'x': 0, 'y': 0, 'width': 1, 'height': 1,
            'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5,
                              'alert_schedules': [{'push_enabled': True}]}],
        }]},
        'recording': {'continuous': False},
    }
    event_id = _lm.process_live_stream_alerts(
        np.full((720, 1280, 3), 20, dtype=np.uint8),
        {'width': 1280, 'height': 720, 'timestamp': time.time()}, camera, enforce_interval=False,
    )
    assert event_id is not None
    [[alert]] = sent
    assert alert['label'] == 'person' and alert['confidence'] == pytest.approx(0.9)
    rows = [row for row in main.database.alerts(limit=10) if row.get('event_id') == event_id]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# SMTP: a refused recipient does not abort the batch
# ---------------------------------------------------------------------------

class _RefusingSmtp:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, message):
        if message['To'] == 'bad@example.com':
            raise smtplib.SMTPRecipientsRefused({'bad@example.com': (550, b'no such user')})
        self.sent.append(message['To'])

    def quit(self):
        pass


def test_refused_recipient_does_not_stop_the_rest_of_the_batch(monkeypatch):
    from contextlib import contextmanager

    from app.email_alerts import EmailAlertError, EmailAlertService

    smtp = _RefusingSmtp()

    @contextmanager
    def session(_self):
        yield smtp

    monkeypatch.setattr(EmailAlertService, '_create_smtp_session', session)
    service = EmailAlertService({'enabled': True, 'host': 'mail', 'from_address': 'cam@example.com'})
    with pytest.raises(EmailAlertError, match='bad@example.com'):
        service.send_alert({'label': 'person', 'rule_name': 'Cam / Gate / person', 'confidence': 0.9},
                           event_id=1, recipients=['a@example.com', 'bad@example.com', 'c@example.com'])
    assert smtp.sent == ['a@example.com', 'c@example.com']


# ---------------------------------------------------------------------------
# Recording extensions: every new label, and no writes that change nothing
# ---------------------------------------------------------------------------

class _Db:
    def __init__(self):
        self.label_writes: list[tuple[list[str], dict]] = []
        self.trigger_reads = 0
        self.trigger_updates: list[tuple[str, str]] = []
        self.recording = {'trigger_type': 'alert', 'trigger_label': 'motion'}

    def update_recording_timing(self, *_a, **_k):
        pass

    def update_recording_extensions(self, *_a, **_k):
        pass

    def add_recording_labels(self, _recording_id, labels, *, source, confidences=None):
        self.label_writes.append((list(labels), dict(confidences or {})))
        return len(labels)

    def get_recording(self, _recording_id):
        self.trigger_reads += 1
        return dict(self.recording)

    def update_recording_trigger(self, _recording_id, *, trigger_type, trigger_label):
        self.trigger_updates.append((trigger_type, trigger_label))
        self.recording = {'trigger_type': trigger_type, 'trigger_label': trigger_label}


class _Recorder:
    def should_record(self, detections, _config=None):
        labels = [d['label'] for d in detections if d.get('alert_triggered')]
        return (True, 'alert', labels[0]) if labels else (False, 'none', None)


@pytest.fixture
def extension(monkeypatch):
    import app.recording_extension as ext

    db = _Db()
    monkeypatch.setattr(ext._state, 'database', db)
    monkeypatch.setattr(ext._state, 'recording_service', _Recorder())
    now = time.time()
    session = {'recording_id': 7, 'start_capture_ts': now - 5, 'capture_deadline_ts': now + 30,
               'max_capture_deadline_ts': now + 300, 'extension_runs': [],
               'stored_labels': {'person': 0.8}, 'trigger_specific': False}
    with ext._state.active_rtsp_recordings_lock:
        ext._state.active_rtsp_recordings['cam'] = session

    def extend(detections, *, step):
        from datetime import datetime, timezone
        return ext.extend_active_rtsp_recording(
            camera_id='cam', event_time=datetime.now(timezone.utc).isoformat(),
            recording_config={'extension_step_seconds': step}, detections=detections,
        )

    yield db, extend
    with ext._state.active_rtsp_recordings_lock:
        ext._state.active_rtsp_recordings.pop('cam', None)


def test_extension_records_a_new_label_even_when_the_deadline_holds(extension):
    """A cat seen while the clip's deadline already covers it (extension step
    shorter than what is left) still joins the clip's labels."""
    db, extend = extension
    assert extend([{'label': 'cat', 'confidence': 0.6, 'alert_triggered': True}], step=5) == 7
    assert db.label_writes == [(['cat'], {'cat': 0.6})]


def test_extension_writes_only_what_changes(extension):
    db, extend = extension
    person = {'label': 'person', 'confidence': 0.7, 'alert_triggered': True}
    for _ in range(5):
        extend([person], step=60)
    assert db.label_writes == []  # already stored at 0.8
    extend([{**person, 'confidence': 0.95}], step=60)
    assert db.label_writes == [(['person'], {'person': 0.95})]
    # The motion trigger was named "person" once; later cycles skip the read.
    assert db.trigger_updates == [('alert', 'person')]
    reads = db.trigger_reads
    extend([person], step=60)
    assert db.trigger_reads == reads


# ---------------------------------------------------------------------------
# Relabel notification wording
# ---------------------------------------------------------------------------

def test_relabel_notification_names_the_relabel_without_the_wrong_score():
    from app.alert_formatting import build_alert_content

    content = build_alert_content(
        {'label': 'cat', 'rule_name': 'Cam / Driveway / cat', 'confidence': 0.43,
         'message': 'AI relabel: cat (the detector reported person)', 'ai_relabel_from': 'person'},
        event_id=5, camera_name='Driveway',
    )
    assert content.alert_message == 'Cat Detected (AI relabel of a person detection)'
    assert content.confidence is None and 'Confidence' not in content.plain_text
