"""Several cameras sharing the detector: per-camera state never leaks.

Track ids restart at 1 on every camera, so anything keyed by track id alone
(the AI label memory, presence, confirmation) would cross between cameras.
These run interleaved cycles, as the monitor does, and real parallel threads.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.main as app_main  # noqa: E402  -- break the circular-import gate
assert app_main is sys.modules['app.main']

from tests.test_live_track_ordering import _load_app  # noqa: E402


def _rule(label, verify=False):
    return {'label': label, 'enabled': True, 'min_confidence': 0.4, 'ai_verify': verify,
            'alert_schedules': [{'push_enabled': True}]}


def _camera(camera_id, rules):
    return {'id': camera_id, 'name': camera_id, 'recording': {'continuous': False},
            'detection': {'object_detection_enabled': True, 'zones': [{
                'id': 'yard', 'name': 'Yard', 'enabled': True, 'monitor_objects': True, 'monitor_motion': False,
                'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}, {'x': 0, 'y': 1}],
                'object_rules': rules}]}}


def _seen(label, x):
    return {'label': label, 'confidence': 0.8, 'box': {'x': x, 'y': 0.5, 'width': 0.1, 'height': 0.1}}


def _setup(tmp_path, monkeypatch, model_reply=None):
    main = _load_app(tmp_path, monkeypatch)
    import app.ai_verification as av
    import app.live_monitor as lm

    scene: dict[str, list] = {}

    class SceneDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, image, confidence=None):
            return [dict(d, box=dict(d['box'])) for d in scene.get(image.decode().split(':', 1)[1], [])]

    monkeypatch.setattr(main._state, 'detector', SceneDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())

    def motion(camera_id, *_args, **_kwargs):
        mask = np.zeros((90, 160), dtype=bool)
        for seen in scene.get(camera_id, []):
            box = seen['box']
            mask[int(box['y'] * 90):int((box['y'] + box['height']) * 90),
                 int(box['x'] * 160):int((box['x'] + box['width']) * 160)] = True
        return bool(mask.any()), 0.5, mask, 0.02

    monkeypatch.setattr(lm, 'detect_frame_motion', motion)
    sent: list = []
    if model_reply is not None:
        monkeypatch.setattr(av.VisionVerifier, 'chat', lambda _self, messages, max_tokens=80: json.dumps(
            model_reply(messages[-1]['content'][0]['text'])))
        monkeypatch.setattr(av, '_forward', lambda alerts, _event, _rules: sent.append([a['label'] for a in alerts]))
        monkeypatch.setattr(lm, 'submit_alert_notification_with_verification', lambda alerts, event_id, rules, **kw: (
            av._verify_and_forward(alerts, event_id, rules, '', None, kw.get('camera_id'))))
    else:
        lock = threading.Lock()

        def record(alerts, _event, _rules, **kw):
            with lock:
                sent.append((kw.get('camera_id'), [a['label'] for a in alerts]))
        monkeypatch.setattr(lm, 'submit_alert_notification_with_verification', record)
    statuses: dict[str, list] = {}
    real_status = lm.update_live_detection_status

    def capture(camera_id, **kwargs):
        statuses.setdefault(camera_id, []).append(kwargs)
        return real_status(camera_id, **kwargs)

    monkeypatch.setattr(lm, 'update_live_detection_status', capture)

    def cycle(settings, now):
        statuses[settings['id']] = []
        event_id = lm.process_live_stream_alerts(
            f"img:{settings['id']}".encode(), {'width': 1280, 'height': 720, 'timestamp': now},
            settings, enforce_interval=False,
        )
        shown = [(d['label'], d.get('track_id')) for d in statuses[settings['id']][-1].get('detections') or []]
        return event_id, shown

    return lm, scene, sent, cycle


def _at(monkeypatch, now):
    monkeypatch.setattr(time, 'time', lambda: now)
    monkeypatch.setattr(time, 'monotonic', lambda: now)


def test_interleaved_cameras_keep_their_own_tracks_and_alerts(tmp_path, monkeypatch):
    _lm, scene, sent, cycle = _setup(tmp_path, monkeypatch)
    cameras = [_camera('front', [_rule('cat')]), _camera('back', [_rule('cat')]), _camera('side', [_rule('dog')])]
    start = time.time()
    rows: dict[str, list] = {camera['id']: [] for camera in cameras}
    for index in range(8):
        _at(monkeypatch, start + index * 0.5)
        scene['front'] = [_seen('cat', 0.10 + 0.03 * index)]
        scene['back'] = [_seen('cat', 0.80 - 0.03 * index)] if index >= 3 else []
        scene['side'] = [_seen('cat', 0.40 + 0.03 * index)]
        for camera in cameras:
            rows[camera['id']].append(cycle(camera, start + index * 0.5))
    alerted = {cid: [i for i, (event_id, _) in enumerate(r) if event_id is not None] for cid, r in rows.items()}
    assert alerted == {'front': [0], 'back': [3], 'side': []}
    assert sorted(sent) == [('back', ['cat']), ('front', ['cat'])]
    assert {track for _, shown in rows['front'] for _, track in shown} == {1}
    assert {track for _, shown in rows['back'] for _, track in shown} == {1}


def test_ai_answer_stays_on_its_own_camera(tmp_path, monkeypatch):
    """Front's detector misreads a cat as a person and the model says cat;
    back has a real person on the same track id, who must stay a person."""
    _lm, scene, sent, cycle = _setup(tmp_path, monkeypatch, model_reply=lambda prompt: (
        {'reason': 'a cat', 'present': False, 'instead': 'cat'} if 'person' in prompt else {'reason': '', 'present': True}))
    front = _camera('front', [_rule('person', verify=True), _rule('cat')])
    back = _camera('back', [_rule('person')])
    start = time.time()
    shown = {'front': [], 'back': []}
    for index in range(4):
        _at(monkeypatch, start + index * 0.5)
        scene['front'] = scene['back'] = [_seen('person', 0.10 + 0.03 * index)]
        for camera in (front, back):
            shown[camera['id']].append(cycle(camera, start + index * 0.5)[1])
    assert shown['front'][1:] == [[('cat', 1)]] * 3
    assert shown['back'] == [[('person', 1)]] * 4
    assert sorted(sent) == [['cat'], ['person']]  # front's cat (relabelled), back's real person


def test_cameras_on_parallel_threads(tmp_path, monkeypatch):
    lm, scene, sent, _cycle = _setup(tmp_path, monkeypatch)
    cameras = [_camera(f'cam{n}', [_rule('cat')]) for n in range(4)]
    errors: list[BaseException] = []

    def run(camera):
        try:
            for index in range(30):
                scene[camera['id']] = [_seen('cat', 0.10 + 0.02 * index)]
                lm.process_live_stream_alerts(
                    f"img:{camera['id']}".encode(), {'width': 1280, 'height': 720, 'timestamp': time.time()},
                    camera, enforce_interval=False,
                )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(camera,)) for camera in cameras]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(camera_id for camera_id, _ in sent) == ['cam0', 'cam1', 'cam2', 'cam3']
