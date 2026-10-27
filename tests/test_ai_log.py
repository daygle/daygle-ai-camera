"""The dedicated AI activity log (app/diagnostics.log_ai_diagnostic).

Per-event AI activity - verification verdicts, descriptions and tag checks,
relabels, face-review captures - is written to the ``ai_diagnostics`` table
and surfaced on the AI Log page, instead of the noisy application logger
(the same move the PTZ auto-track log made earlier). AI model startup and
engineering diagnostics (detector load, backend selection, INT8 quantization)
land here too.
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.database import EventDatabase


def _db(tmp_path):
    return EventDatabase(str(tmp_path / 'ai.sqlite3'))


# ── Storage layer ─────────────────────────────────────────────────────────

def test_roundtrip_and_filters(tmp_path):
    db = _db(tmp_path)
    db.add_ai_diagnostic(
        created_at='2026-10-27T01:00:00+00:00', event_type='ai_verification_filtered',
        message='filtered cat', camera_id='cam', camera_name='Front',
        details={'event_id': 1, 'labels': ['cat']},
    )
    db.add_ai_diagnostic(
        created_at='2026-10-27T02:00:00+00:00', event_type='ai_relabel',
        message='cat -> dog', camera_id='cam',
    )
    db.add_ai_diagnostic(
        created_at='2026-10-27T03:00:00+00:00', event_type='ai_search_fallback',
        message='keywords',
    )
    assert db.count_ai_diagnostics() == 3
    assert db.count_ai_diagnostics(camera_id='cam') == 2
    assert db.count_ai_diagnostics(event_type='ai_relabel') == 1
    assert db.count_ai_diagnostics(severity='warning') == 0
    assert db.count_ai_diagnostics(created_after='2026-10-27T02:00:00+00:00') == 2

    newest = db.list_ai_diagnostics(limit=1)[0]
    assert newest['event_type'] == 'ai_search_fallback'
    assert newest['camera_id'] is None
    assert newest['details'] == {}

    filtered = db.list_ai_diagnostics(event_type='ai_verification_filtered')[0]
    assert filtered['camera_name'] == 'Front'
    assert filtered['details'] == {'event_id': 1, 'labels': ['cat']}


def test_search_filters_message_and_details(tmp_path):
    """Free-text search matches the message OR the details JSON blob.

    Wildcards typed by the operator must be literals (escaped), not SQL
    LIKE wildcards, and matching is case-insensitive for ASCII.
    """
    db = _db(tmp_path)
    db.add_ai_diagnostic(
        created_at='2026-10-27T01:00:00+00:00', event_type='ai_model_install',
        message='Downloaded arcface.onnx (512 bytes) from https://example.invalid.',
        details={'step': 'download', 'file': 'arcface.onnx', 'sha256': 'abc123feed'},
    )
    db.add_ai_diagnostic(
        created_at='2026-10-27T02:00:00+00:00', event_type='ai_relabel',
        message='cat -> dog', details={}
    )

    # Matched in the message text...
    assert db.count_ai_diagnostics(search='arcface.onnx') == 1
    # ...and in the details JSON (model hashes are searchable).
    assert db.count_ai_diagnostics(search='abc123') == 1
    # Case-insensitive for ASCII.
    assert db.count_ai_diagnostics(search='ABC123FEED') == 1
    # A literal '%'/_ typed by the operator matches nothing, not everything.
    assert db.count_ai_diagnostics(search='%') == 0
    assert db.count_ai_diagnostics(search='_') == 0
    assert db.count_ai_diagnostics() == 2

    hits = db.list_ai_diagnostics(search='abc123')
    assert [hit['event_type'] for hit in hits] == ['ai_model_install']
    # Details stay structured after a search hit.
    assert hits[0]['details']['sha256'] == 'abc123feed'


def test_ai_log_router_passes_search_through():
    """The /api/ai-log GET accepts a q param and feeds it to both the list
    and the count query (mirrors the repo's static pinning style)."""
    import inspect

    from app.api import ai_log_router

    source = inspect.getsource(ai_log_router)
    assert 'q: str | None' in source
    assert 'search=(q.strip() or None) if q else None' in source
    assert source.count('search=') >= 2  # list + count


def test_row_cap_and_clear_and_purge(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(type(db), 'AI_DIAGNOSTICS_MAX_ROWS', 3)
    for index in range(5):
        db.add_ai_diagnostic(
            created_at=f'2026-10-27T00:00:0{index}+00:00', event_type='ai_relabel', message=f'm{index}',
        )
    assert [row['message'] for row in db.list_ai_diagnostics(limit=10)] == ['m4', 'm3', 'm2']

    # The purge honours the canonical UTC cutoff: of the capped newest three
    # rows (m2..m4), m2 and m3 are older than the bound.
    assert db.purge_ai_diagnostics_older_than('2026-10-27T00:00:04+00:00') == 2
    assert db.count_ai_diagnostics() == 1
    assert db.list_ai_diagnostics()[0]['message'] == 'm4'
    assert db.delete_all_ai_diagnostics() == 1
    assert db.count_ai_diagnostics() == 0


def test_helper_is_best_effort(monkeypatch):
    """A database failure must never break the calling AI pool path."""
    import app.diagnostics as dg
    import app.state as state

    class _Boom:
        def add_ai_diagnostic(self, **kw):
            raise RuntimeError('disk full')

    monkeypatch.setattr(state, 'database', _Boom())
    monkeypatch.setattr(state, 'cameras_config', [])
    dg.log_ai_diagnostic('ai_relabel', 'cat -> dog', camera_id='cam')  # must not raise


def test_helper_resolves_the_camera_name(monkeypatch):
    import app.diagnostics as dg
    import app.state as state

    stored = {}

    class _Db:
        def add_ai_diagnostic(self, **kw):
            stored.update(kw)

    monkeypatch.setattr(state, 'database', _Db())
    monkeypatch.setattr(state, 'cameras_config', [{'id': 'cam', 'name': 'Front Door'}])
    dg.log_ai_diagnostic('ai_relabel', 'cat -> dog', camera_id='cam')
    assert stored['camera_id'] == 'cam'
    assert stored['camera_name'] == 'Front Door'


# ── Converted call sites reach the AI log, not the app logger ─────────────

def _recorder(events):
    def helper(event_type, message='', *, severity='info', details=None, camera_id=None, camera_name=None):
        events.append({
            'event_type': event_type, 'message': message, 'severity': severity,
            'details': details, 'camera_id': camera_id, 'camera_name': camera_name,
        })
    return helper


def test_relabel_stores_reach_the_ai_log(caplog, monkeypatch, tmp_path):
    import app.state as state
    import app.ai_relabel as relabel
    from tests.support import add_event

    db = _db(tmp_path)
    event_id = add_event(db, created_at='2026-10-27T01:00:00+00:00', source='rtsp',
                         snapshot_path=None, detections=[], metadata={'camera_id': 'cam'})
    event = db.get_event(event_id)

    events = []
    monkeypatch.setattr(state, 'database', db)
    monkeypatch.setattr(relabel, 'log_ai_diagnostic', _recorder(events))
    with caplog.at_level('WARNING'):
        relabel._store(
            [{'rule_name': 'Cats', 'label': 'dog', 'confidence': 0.7, 'message': 'AI relabel: dog',
              'ai_relabel_from': 'cat'}],
            event, event_id,
        )
    assert [entry['event_type'] for entry in events] == ['ai_relabel']
    assert all(entry['camera_id'] == 'cam' for entry in events)
    assert 'cat -> dog (Cats)' in events[0]['message']
    assert not [record for record in caplog.records if record.levelname == 'INFO']


def test_unknown_face_skip_reaches_the_ai_log(caplog, monkeypatch, tmp_path):
    import app.state as state
    import app.face_identity as face_identity

    events = []
    monkeypatch.setattr(state, 'database', _db(tmp_path))
    monkeypatch.setattr(face_identity, 'log_ai_diagnostic', _recorder(events))

    class _Service:
        model_id = 'test-face-model'

        def embed_face(self, crop):
            return None

    with caplog.at_level('INFO'):
        face_identity._store_unknown_face('cam', 42, {'box': {}}, object(), _Service())
    assert [entry['event_type'] for entry in events] == ['face_capture_skipped']
    assert events[0]['camera_id'] == 'cam'
    assert not [record for record in caplog.records if record.levelname == 'INFO']


def detector_backend_test_recorder(monkeypatch, statuses):
    """Caplog-style recorder for ``log_detector_initialization`` conversions."""
    import app.ai_settings as ai_settings

    events = []
    monkeypatch.setattr(ai_settings, 'ai_status_payload', lambda: statuses)
    monkeypatch.setattr(
        ai_settings, '_state',
        type('S', (), {'detector': SimpleNamespace(active_providers=['CPUExecutionProvider'])})(),
    )
    monkeypatch.setattr(
        ai_settings, 'log_ai_diagnostic',
        lambda event_type, message='', *, severity='info', details=None,
        camera_id=None, camera_name=None: events.append({
            'event_type': event_type, 'message': message, 'severity': severity,
            'details': details or {},
        }),
    )
    return events


def test_detector_backend_summary_reaches_the_ai_log(caplog, monkeypatch):
    import app.ai_settings as ai_settings

    status = {
        'active_backend': 'onnx', 'configured_backend': 'onnx', 'model_loaded': True,
        'inference_available': True, 'active_precision': 'fp16', 'precision': 'FP16',
        'model_path': '/models/yolo11n.onnx', 'labels_path': '/models/labels.txt', 'error': None,
    }
    events = detector_backend_test_recorder(monkeypatch, status)
    with caplog.at_level('INFO'):
        ai_settings.log_detector_initialization('reload')
    assert [entry['event_type'] for entry in events] == ['ai_detector_backend']
    assert events[0]['severity'] == 'info'
    assert 'AI detector reload:' in events[0]['message']
    assert 'active_backend=onnx' in events[0]['message']
    assert events[0]['details'] == {
        'context': 'reload', 'active_backend': 'onnx', 'configured_backend': 'onnx',
        'model_loaded': True, 'inference_available': True, 'active_precision': 'fp16',
        'requested_precision': 'fp16', 'providers': 'CPUExecutionProvider',
        'model_path': '/models/yolo11n.onnx', 'labels_path': '/models/labels.txt', 'error': '<none>',
    }
    assert not [record for record in caplog.records if record.levelname == 'INFO']


def test_detector_backend_failure_summary_is_a_warning(caplog, monkeypatch):
    import app.ai_settings as ai_settings

    status = {
        'active_backend': 'unknown', 'configured_backend': 'onnx', 'model_loaded': False,
        'inference_available': False, 'active_precision': None, 'precision': None,
        'model_path': None, 'labels_path': None, 'error': 'model file missing',
    }
    events = detector_backend_test_recorder(monkeypatch, status)
    with caplog.at_level('WARNING', logger='daygle.ai'):
        ai_settings.log_detector_initialization()  # context defaults to 'startup'
    assert [entry['event_type'] for entry in events] == ['ai_detector_backend']
    assert events[0]['severity'] == 'warning' and events[0]['details']['context'] == 'startup'
    assert 'model file missing' in events[0]['message']


# ── Model install lifecycle (download, integrity, export) ─────────────────

def _mm_recorder(monkeypatch):
    """Swap model_management's AI-log writer for a capture list."""
    import app.model_management as mm

    events = []

    def _record(event_type, message='', *, severity='info', details=None,
                camera_id=None, camera_name=None):
        events.append({
            'event_type': event_type, 'message': message, 'severity': severity,
            'details': details or {}, 'camera_id': camera_id, 'camera_name': camera_name,
        })

    monkeypatch.setattr(mm, 'log_ai_diagnostic', _record)
    return mm, events


def test_download_weights_success_reaches_the_ai_log(tmp_path, monkeypatch, caplog):
    import hashlib

    mm, events = _mm_recorder(monkeypatch)
    payload = b'fake weights'

    def _fake_urlopen(_request, timeout):
        chunks = iter([payload])

        class _Stream:
            def read(self, n):
                return next(chunks, b'')

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return _Stream()

    monkeypatch.setattr(mm.urllib.request, 'urlopen', _fake_urlopen)
    destination = tmp_path / 'weights.pt'
    with caplog.at_level('INFO'):
        mm._download_weights(
            'https://example.invalid/weights.pt', destination,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
    assert destination.read_bytes() == payload
    assert [entry['event_type'] for entry in events] == ['ai_model_install']
    assert events[0]['severity'] == 'info'
    assert events[0]['details']['step'] == 'download'
    assert events[0]['details']['size_bytes'] == len(payload)
    assert events[0]['details']['sha256'] == hashlib.sha256(payload).hexdigest()
    assert not caplog.records
    # The temp file must not survive the download.
    assert list(tmp_path.glob('weights.download-*')) == []


def test_download_weights_failure_logged_as_an_error(tmp_path, monkeypatch, caplog):
    import urllib.error

    mm, events = _mm_recorder(monkeypatch)

    def _boom(_request, timeout):
        raise urllib.error.URLError('no route to host')

    monkeypatch.setattr(mm.urllib.request, 'urlopen', _boom)
    with caplog.at_level('ERROR'), pytest.raises(urllib.error.URLError):
        mm._download_weights(
            'https://example.invalid/weights.pt', tmp_path / 'weights.pt',
            expected_sha256='0' * 64,
        )
    assert [entry['severity'] for entry in events] == ['error']
    assert events[0]['details']['step'] == 'download'
    assert 'no route to host' in events[0]['message']
    # The failure must not leave a half-written cache entry behind.
    assert not (tmp_path / 'weights.pt').exists()
    assert list(tmp_path.glob('weights.download-*')) == []


def test_cached_weights_integrity_failure_reaches_the_ai_log(tmp_path, monkeypatch, caplog):
    mm, events = _mm_recorder(monkeypatch)
    import subprocess
    import sys

    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)

    catalog = dict(mm.YOLO_MODELS)
    catalog['facetest'] = {
        'pt': 'facetest.pt', 'onnx': 'facetest.onnx', 'label': 'Face Test',
        'approx_mb': 6, 'input_size': 640, 'labels': 'models/face.names',
        'keypoint_count': 5, 'weights_url': 'https://example.invalid/facetest.pt',
        'weights_sha256': '0' * 64, 'description': 'synthetic face entry',
    }
    monkeypatch.setattr(mm, 'YOLO_MODELS', catalog)

    # A cached copy whose bytes no longer match the pinned hash...
    cached = models_dir / 'facetest.pt'
    cached.write_bytes(b'stale weights')
    monkeypatch.setattr(mm, '_sha256_file', lambda _path: 'deadbeef')

    def fake_download(_url, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'fresh weights')

    monkeypatch.setattr(mm, '_download_weights', fake_download)

    destination = models_dir / 'facetest.onnx'

    def fake_run(command, cwd, capture_output, text, timeout, check):  # noqa: ANN001
        destination.write_bytes(b'fake onnx')
        return subprocess.CompletedProcess(command, 0, stdout='exported', stderr='')

    monkeypatch.setattr(mm.subprocess, 'run', fake_run)

    with caplog.at_level('WARNING'):
        mm.export_yolo_onnx('facetest', destination)
    integrity = [entry for entry in events if entry['details'].get('step') == 'integrity']
    assert len(integrity) == 1
    assert integrity[0]['severity'] == 'warning'
    assert 'integrity check' in integrity[0]['message']
    assert integrity[0]['details']['file'] == 'facetest.pt'
    assert not [record for record in caplog.records if record.levelname == 'WARNING']


def test_export_success_and_failure_reach_the_ai_log(tmp_path, monkeypatch):
    import subprocess

    mm, events = _mm_recorder(monkeypatch)
    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)
    model_name = next(iter(mm.YOLO_MODELS))
    destination = models_dir / mm.YOLO_MODELS[model_name]['onnx']

    def failing_run(command, cwd, capture_output, text, timeout, check):  # noqa: ANN001
        return subprocess.CompletedProcess(command, 1, stdout='', stderr='boom')

    monkeypatch.setattr(mm.subprocess, 'run', failing_run)
    with pytest.raises(RuntimeError):
        mm.export_yolo_onnx(model_name, destination)
    assert [entry['severity'] for entry in events] == ['error']
    assert events[0]['details']['step'] == 'export'
    assert 'boom' in events[0]['message']
    assert not destination.exists()

    events.clear()

    def ok_run(command, cwd, capture_output, text, timeout, check):  # noqa: ANN001
        destination.write_bytes(b'fake onnx')
        return subprocess.CompletedProcess(command, 0, stdout='exported', stderr='')

    monkeypatch.setattr(mm.subprocess, 'run', ok_run)
    mm.export_yolo_onnx(model_name, destination)
    assert [entry['event_type'] for entry in events] == ['ai_model_install']
    assert events[0]['severity'] == 'info'
    assert events[0]['details']['step'] == 'export'
    assert events[0]['details']['model'] == model_name
    assert events[0]['details']['size_bytes'] == len(b'fake onnx')


def _delete_sandbox(tmp_path, monkeypatch):
    """A models dir with an installed 'yolo26n' file, safe to delete.

    Mirrors the sandbox in tests/test_quantization.py::
    test_delete_model_removes_int8_cache_sibling.
    """
    mm, events = _mm_recorder(monkeypatch)
    models_dir = tmp_path / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    onnx = models_dir / 'yolo26n.onnx'
    onnx.write_bytes(b'fake onnx')
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, 'MODELS_DIR', models_dir)
    # A different active model, so the delete isn't blocked by the active guard.
    monkeypatch.setattr(mm, 'effective_ai_config', lambda: {'model_path': 'models/yolo11n.onnx'})
    return mm, events, onnx


def test_delete_model_success_reaches_the_ai_log(tmp_path, monkeypatch):
    mm, events, onnx = _delete_sandbox(tmp_path, monkeypatch)

    result = mm.delete_model('yolo26n')

    assert result['ok'] is True
    assert [entry['event_type'] for entry in events] == ['ai_model_install']
    assert events[0]['severity'] == 'info'
    assert events[0]['details']['step'] == 'delete'
    assert events[0]['details']['model'] == 'yolo26n'
    assert events[0]['details']['file'] == 'models/yolo26n.onnx'
    assert events[0]['details']['int8_cache_removed'] is False
    assert 'Deleted' in events[0]['message']
    assert not onnx.exists()


def test_delete_model_int8_cache_removal_is_recorded(tmp_path, monkeypatch):
    from app.quantization import _int8_cache_metadata_path, int8_cache_path

    mm, events, onnx = _delete_sandbox(tmp_path, monkeypatch)
    int8_cache_path(onnx).write_bytes(b'fake int8')
    _int8_cache_metadata_path(onnx).write_text('stale-source-hash', encoding='ascii')

    mm.delete_model('yolo26n')

    assert events[0]['details']['int8_cache_removed'] is True


def test_delete_model_failure_reaches_the_ai_log(tmp_path, monkeypatch):
    from fastapi import HTTPException

    mm, events, onnx = _delete_sandbox(tmp_path, monkeypatch)
    import app.quantization as quant

    def _locked(_onnx_path):
        raise RuntimeError('int8 cache is locked by another process')

    monkeypatch.setattr(quant, 'int8_cache_path', _locked)
    with pytest.raises(HTTPException) as excinfo:
        mm.delete_model('yolo26n')
    assert excinfo.value.status_code == 503
    assert [entry['severity'] for entry in events] == ['error']
    assert events[0]['details']['step'] == 'delete'
    assert 'int8 cache is locked' in events[0]['message']
    # A failed delete must leave the model in place.
    assert onnx.exists()


def test_delete_model_validation_rejections_write_no_records(tmp_path, monkeypatch):
    from fastapi import HTTPException

    mm, events, onnx = _delete_sandbox(tmp_path, monkeypatch)

    # Operator-input rejections (unknown model / not installed) are surfaced
    # by the API response; no AI-log record is written for them.
    with pytest.raises(HTTPException):
        mm.delete_model('nosuchmodel')
    assert events == []
    with pytest.raises(HTTPException):
        mm.delete_model('yolo11n')  # in the catalog but not installed in the sandbox
    assert events == []
    assert onnx.exists()


def test_registry_read_failure_reaches_the_ai_log(tmp_path, monkeypatch, caplog):
    mm, events = _mm_recorder(monkeypatch)
    # Point the registry at the sandbox, never at the repo's real
    # models/installed.json.
    monkeypatch.setattr(mm, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(mm, '_export_info_cache', {})
    registry = mm._installed_models_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text('not json at all', encoding='utf-8')
    with caplog.at_level('WARNING'):
        assert mm._read_installed_models() == {}
    assert [entry['event_type'] for entry in events] == ['ai_model_install']
    assert events[0]['severity'] == 'warning'
    assert events[0]['details']['step'] == 'registry'
    assert not [record for record in caplog.records if record.levelname == 'WARNING']


# ── Side models: face embedder load + YAMNet TFLite readiness ─────────────

def test_face_matcher_refresh_failures_reach_the_ai_log(tmp_path, monkeypatch, caplog):
    import app.face_recognition_service as frs
    from app.database import EventDatabase

    events = _replacer(monkeypatch, frs)
    # An enabled-less config keeps the constructor from touching a model.
    svc = frs.FaceRecognitionService({'enabled': False}, EventDatabase(str(tmp_path / 'f.sqlite3')))

    class _BoomDb:
        def load_face_embeddings(self, _model_id):
            raise RuntimeError('embeddings table corrupted')

    monkeypatch.setattr(svc, '_database', _BoomDb())
    with caplog.at_level('WARNING'):
        svc.refresh_matcher()
    assert [entry['details']['step'] for entry in events] == ['matcher_refresh']
    assert events[0]['severity'] == 'warning'
    assert 'failed to load embeddings' in events[0]['message']
    assert events[0]['details']['model_id'] == svc.model_id

    # A matcher that cannot be built is the second warning branch.
    monkeypatch.setattr(svc, '_database', EventDatabase(str(tmp_path / 'f.sqlite3')))

    def _broken_matcher(_rows):
        raise RuntimeError('matrix boom')

    monkeypatch.setattr(frs, 'FaceMatcher', _broken_matcher)
    with caplog.at_level('WARNING'):
        svc.refresh_matcher()
    assert [entry['details']['step'] for entry in events] == ['matcher_refresh', 'matcher_refresh']
    assert 'Face matcher rebuild failed' in events[1]['message']
    assert not [record for record in caplog.records if record.levelname == 'WARNING']


def test_face_service_reload_failure_reaches_the_ai_log(monkeypatch, caplog):
    import app.face_recognition_service as frs
    import app.state as state

    events = _replacer(monkeypatch, frs)

    class _BoomService:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError('service construction boom')

    monkeypatch.setattr(frs, 'FaceRecognitionService', _BoomService)
    with caplog.at_level('WARNING'):
        ok, reason = frs.reload_face_recognition_service(config={'enabled': False})
    assert (ok, reason) == (False, 'Face recognition failed to load.')
    assert [entry['event_type'] for entry in events] == ['ai_face_service']
    assert events[0]['severity'] == 'warning'
    assert events[0]['details']['step'] == 'reload'
    assert 'service construction boom' in events[0]['message']
    assert not [record for record in caplog.records if record.levelname == 'WARNING']


def test_face_scaling_sync_records_reach_the_ai_log(monkeypatch, caplog):
    import numpy as np

    import app.face_recognition_service as frs

    events = _replacer(monkeypatch, frs)
    svc = frs.FaceRecognitionService.__new__(frs.FaceRecognitionService)
    svc.model_id = 'test-face-model'
    stored = {}

    class _FakeDb:
        def get_setting(self, _key):
            return {'test-face-model': 'normalized'}  # differs from embedder's 'raw'

        def stored_face_embeddings_for_refresh(self, _model_id):
            return [
                {'table': 'person_faces', 'id': 1, 'source': 'auto', 'thumbnail': None},
                {'table': 'person_faces', 'id': 2, 'source': 'enrolled', 'thumbnail': b'2'},
                {'table': 'pending_review', 'id': 3, 'source': 'auto', 'thumbnail': None},
                {'table': 'person_faces', 'id': 4, 'source': 'enrolled', 'thumbnail': b'4'},
            ]

        def delete_person_face(self, _id):
            return 1

        def replace_face_embedding(self, _table, _id, **_kw):
            pass

        def set_setting(self, key, value, _now):
            stored[key] = value

    svc._database = _FakeDb()
    svc.embedder = SimpleNamespace(input_scaling='raw')
    # Thumbnail bytes are not real images; fake the decoder to pass them through.
    monkeypatch.setattr(frs, 'decode_bgr_image', lambda thumb: thumb)

    def fake_embed(face_bgr):
        if face_bgr == b'4':
            raise RuntimeError('bad crop')
        return np.full(8, 0.25, dtype=np.float32)

    svc.embedder.embed = fake_embed

    with caplog.at_level('WARNING'):
        svc._sync_stored_embeddings()
    assert [entry['details']['step'] for entry in events] == ['scaling_sync', 'scaling_sync', 'scaling_sync']
    # The per-face re-embed failure...
    assert events[0]['severity'] == 'warning'
    assert events[0]['message'].startswith('Could not re-embed stored face person_faces/4')
    # ...the summary outcome...
    assert events[1]['severity'] == 'info'
    assert events[1]['details']['re_embedded'] == 1
    assert events[1]['details']['removed'] == 1
    assert events[1]['details']['left'] == 2
    # ...and the no-picture warning.
    assert events[2]['severity'] == 'warning'
    assert 'no picture to re-embed from' in events[2]['message']
    assert stored['face_embedding_input_scaling']['test-face-model'] == 'raw'
    assert not [record for record in caplog.records if record.levelname == 'WARNING']
def _replacer(monkeypatch, module):
    """Swap a module's log_ai_diagnostic import for a capture list."""
    events = []

    def _record(event_type, message='', *, severity='info', details=None,
                camera_id=None, camera_name=None):
        events.append({
            'event_type': event_type, 'message': message, 'severity': severity,
            'details': details or {}, 'camera_id': camera_id, 'camera_name': camera_name,
        })

    monkeypatch.setattr(module, 'log_ai_diagnostic', _record)
    return events


def test_face_embedder_load_failure_reaches_the_ai_log(tmp_path, monkeypatch, caplog):
    import pathlib

    import app.face_recognition as fr

    events = _replacer(monkeypatch, fr)
    corrupt = tmp_path / 'arcface.onnx'
    corrupt.write_bytes(b'not an onnx model')
    with caplog.at_level('WARNING'):
        embedder = fr.FaceEmbedder(pathlib.Path(corrupt))
    assert embedder.available is False
    assert [entry['event_type'] for entry in events] == ['ai_face_embedder']
    assert events[0]['severity'] == 'warning'
    assert events[0]['details']['step'] == 'load'
    assert not [record for record in caplog.records if record.levelname == 'WARNING']


def test_face_embedder_scaling_lines_reach_the_ai_log(monkeypatch, caplog):
    import numpy as np

    import app.face_recognition as fr

    events = _replacer(monkeypatch, fr)
    embedder = fr.FaceEmbedder.__new__(fr.FaceEmbedder)
    embedder.model_path = fr._resolve_project_path('arcface.onnx')
    embedder.input_size = 112
    embedder.input_scaling = fr.INPUT_SCALING_NORMALIZED
    # Constant unit embeddings are identical under both scalings, so
    # choose_input_scaling returns the ArcFace-normalised default.
    unit = np.full(8, 1.0 / np.sqrt(8), dtype=np.float32)
    monkeypatch.setattr(embedder, '_embed_tensor', lambda _tensor: unit)
    with caplog.at_level('INFO'):
        scaling = embedder.calibrate_input_scaling()
    assert scaling == fr.INPUT_SCALING_NORMALIZED
    assert [entry['event_type'] for entry in events] == ['ai_face_embedder']
    assert events[0]['details']['step'] == 'scaling'
    assert events[0]['details']['scaling'] == 'normalized'
    assert events[0]['message'].startswith('Face embedding model arcface.onnx takes [-1, 1] pixels')
    assert not [record for record in caplog.records if record.levelname == 'INFO']


def _yamnet_detector(monkeypatch):
    """A _YamnetBackend with the downloads skipped and a fake interpreter."""
    import app.sound_detector as sd

    det = sd._YamnetBackend()
    monkeypatch.setattr(det, '_ensure_asset', lambda *_a, **_k: None)
    monkeypatch.setattr(det, '_save_installed_version', lambda: None)
    monkeypatch.setattr(det, '_load_class_names', staticmethod(lambda _path: ['Speech', 'Dog']))
    return sd, det


def _fake_interpreter():
    class _FakeInterpreter:
        def __init__(self, *_args, **_kwargs):
            pass

        def allocate_tensors(self):
            pass

        def get_input_details(self):
            return []

        def get_output_details(self):
            return []

    return _FakeInterpreter


def test_yamnet_tflite_ready_reaches_the_ai_log(monkeypatch, caplog):
    sd, det = _yamnet_detector(monkeypatch)
    events = _replacer(monkeypatch, sd)
    monkeypatch.setattr(det, '_interpreter_class', _fake_interpreter)
    with caplog.at_level('INFO'):
        assert det._load() is True
    assert [entry['event_type'] for entry in events] == ['ai_yamnet_model']
    assert events[0]['severity'] == 'info'
    assert events[0]['details']['step'] == 'ready'
    assert events[0]['details']['classes'] == 2
    assert 'classifying against 2 AudioSet classes' in events[0]['message']
    assert not [record for record in caplog.records if record.levelname == 'INFO']


def test_yamnet_tflite_unavailable_reaches_the_ai_log(monkeypatch, caplog):
    sd, det = _yamnet_detector(monkeypatch)
    events = _replacer(monkeypatch, sd)

    def _boom():
        raise RuntimeError('tflite boom')

    monkeypatch.setattr(det, '_interpreter_class', _boom)
    with caplog.at_level('WARNING'):
        assert det._load() is False
    assert [entry['event_type'] for entry in events] == ['ai_yamnet_model']
    assert events[0]['severity'] == 'warning'
    assert events[0]['details']['step'] == 'load'
    assert 'tflite boom' in events[0]['message']
    assert not [record for record in caplog.records if record.levelname == 'WARNING']


def test_yamnet_asset_download_reaches_the_ai_log(tmp_path, monkeypatch, caplog):
    import app.sound_detector as sd

    events = _replacer(monkeypatch, sd)
    payload = b'yamnet bytes'

    def fake_urlretrieve(_url, dest):
        dest.write_bytes(payload)

    monkeypatch.setattr(sd, 'urlretrieve', fake_urlretrieve)
    target = tmp_path / 'yamnet.tflite'
    with caplog.at_level('INFO'):
        sd._YamnetBackend._ensure_asset(
            target, 'https://example.invalid/yamnet.tflite', 'YAMNet TFLite model',
        )
    assert target.read_bytes() == payload
    assert [entry['event_type'] for entry in events] == ['ai_yamnet_model']
    assert events[0]['severity'] == 'info'
    assert events[0]['details']['step'] == 'download'
    assert events[0]['details']['size_bytes'] == len(payload)
    assert list(tmp_path.glob('*.download')) == []
    assert not [record for record in caplog.records if record.levelname == 'INFO']


def test_yamnet_update_check_reaches_the_ai_log(tmp_path, monkeypatch, caplog):
    import urllib.error

    import app.sound_detector as sd

    events = _replacer(monkeypatch, sd)
    monkeypatch.setattr(sd, '_YAMNET_TFLITE_PATH', tmp_path / 'yamnet.tflite')
    monkeypatch.setattr(sd, '_YAMNET_CLASS_MAP_PATH', tmp_path / 'yamnet-class-map.csv')
    backend = sd._YamnetBackend()

    # A failed network fetch -> warning record, no check artifact left behind.
    def _boom(_url, _dest):
        raise urllib.error.URLError('no route to host')

    monkeypatch.setattr(sd, 'urlretrieve', _boom)
    with caplog.at_level('WARNING'):
        result = backend.check_for_update()
    assert result['update_available'] is False
    assert [entry['details']['step'] for entry in events] == ['update_check']
    assert events[0]['severity'] == 'warning'
    assert events[0]['message'].startswith('YAMNet update check failed')
    assert list(tmp_path.glob('*.check')) == []
    assert not [record for record in caplog.records if record.levelname == 'WARNING']

    # A completed check reports its outcome (here: nothing newer installed).
    events.clear()
    payload = b'yamnet bytes'
    monkeypatch.setattr(sd, 'urlretrieve', lambda _url, dest: dest.write_bytes(payload))
    result = backend.check_for_update()
    assert result['update_available'] is False
    assert [entry['details']['step'] for entry in events] == ['update_check']
    assert events[0]['severity'] == 'info'
    assert events[0]['details']['update_available'] is False
    assert events[0]['details']['latest_size'] == len(payload)
    assert not [record for record in caplog.records if record.levelname == 'INFO']


def test_yamnet_reload_success_reaches_the_ai_log(tmp_path, monkeypatch, caplog):
    import app.sound_detector as sd

    sd, det = _yamnet_detector(monkeypatch)
    events = _replacer(monkeypatch, sd)
    # The reload flow replaces temp files onto the module asset paths, so
    # point those constants at the sandbox and let _ensure_asset write them.
    monkeypatch.setattr(sd, '_YAMNET_TFLITE_PATH', tmp_path / 'yamnet.tflite')
    monkeypatch.setattr(sd, '_YAMNET_CLASS_MAP_PATH', tmp_path / 'yamnet-class-map.csv')
    monkeypatch.setattr(
        det, '_ensure_asset',
        lambda path, _url, _label: path.write_bytes(b'asset:' + path.name.encode()),
    )
    monkeypatch.setattr(det, '_interpreter_class', _fake_interpreter)
    with caplog.at_level('INFO'):
        assert det.reload() is True
    assert det.available is True
    assert [entry['details']['step'] for entry in events] == ['reload']
    assert events[0]['severity'] == 'info'
    assert 'reloaded successfully' in events[0]['message']
    # On-disk: the committed assets stay, the temp/rollback copies are gone.
    assert (tmp_path / 'yamnet.tflite').read_bytes().startswith(b'asset:')
    assert list(tmp_path.glob('*.update')) == []
    assert list(tmp_path.glob('*.rollback')) == []
    assert not [record for record in caplog.records if record.levelname == 'INFO']


def test_yamnet_reload_failure_reaches_the_ai_log(tmp_path, monkeypatch, caplog):
    import app.sound_detector as sd

    sd, det = _yamnet_detector(monkeypatch)
    events = _replacer(monkeypatch, sd)
    monkeypatch.setattr(sd, '_YAMNET_TFLITE_PATH', tmp_path / 'yamnet.tflite')
    monkeypatch.setattr(sd, '_YAMNET_CLASS_MAP_PATH', tmp_path / 'yamnet-class-map.csv')
    monkeypatch.setattr(
        det, '_ensure_asset',
        lambda path, _url, _label: path.write_bytes(b'asset:' + path.name.encode()),
    )

    def _interpreter_boom():
        raise RuntimeError('tflite update boom')

    monkeypatch.setattr(det, '_interpreter_class', _interpreter_boom)
    with caplog.at_level('WARNING'):
        assert det.reload() is False
    assert [entry['event_type'] for entry in events] == ['ai_yamnet_model']
    assert events[0]['severity'] == 'warning'
    assert events[0]['details']['step'] == 'reload'
    assert 'YAMNet TFLite reload failed' in events[0]['message']
    assert 'tflite update boom' in events[0]['message']
    # No working model before the reload, so it must not silently become available.
    assert det._available is False
    assert not [record for record in caplog.records if record.levelname == 'WARNING']
