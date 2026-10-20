"""A person box drawn on an animal the detector also boxed is dropped."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.overlap_suppression import covering_animal, drop_people_boxed_as_animals, overlap_reason  # noqa: E402


def _det(label, confidence, x, y, w, h, *, flat=False):
    box = {'x': x, 'y': y, 'width': w, 'height': h}
    return {'label': label, 'confidence': confidence, **(box if flat else {'box': box})}


def test_person_box_on_a_stronger_cat_is_dropped():
    """The driveway cat: cat 85% and person 68% on the same spot (IoU ~0.6)."""
    cat = _det('cat', 0.85, 0.425, 0.465, 0.086, 0.04)
    person = _det('person', 0.68, 0.422, 0.46, 0.092, 0.064)
    assert drop_people_boxed_as_animals([cat, person], [cat, person]) == [cat]
    assert overlap_reason(person, [cat, person]) == 'Same object as cat (85%)'


def test_stored_flat_boxes_work_too():
    cat = _det('cat', 0.85, 0.4, 0.4, 0.1, 0.1, flat=True)
    person = _det('person', 0.6, 0.4, 0.4, 0.1, 0.11, flat=True)
    assert covering_animal(person, [cat]) is cat


@pytest.mark.parametrize(('person', 'animal'), [
    # Someone holding a cat: the cat is a small part of the person's box.
    (_det('person', 0.6, 0.3, 0.2, 0.2, 0.6), _det('cat', 0.9, 0.35, 0.4, 0.08, 0.08)),
    # A confident person over a weaker dog box is a person.
    (_det('person', 0.9, 0.4, 0.4, 0.1, 0.1), _det('dog', 0.7, 0.4, 0.4, 0.1, 0.1)),
    # Not an animal: a person in a car's box is not suppressed here.
    (_det('person', 0.6, 0.4, 0.4, 0.1, 0.1), _det('car', 0.9, 0.4, 0.4, 0.1, 0.1)),
])
def test_people_who_are_not_the_animal_are_kept(person, animal):
    assert drop_people_boxed_as_animals([person], [person, animal]) == [person]


def test_only_person_boxes_are_dropped():
    cat = _det('cat', 0.9, 0.4, 0.4, 0.1, 0.1)
    dog = _det('dog', 0.5, 0.4, 0.4, 0.1, 0.1)
    assert drop_people_boxed_as_animals([dog], [cat, dog]) == [dog]


np = pytest.importorskip("numpy")
pytest.importorskip("cv2")


class _CatAndPersonDetector:
    backend = 'onnx'
    available = True
    unavailable_reason = None

    def detect_frame(self, image, confidence=None):
        return [
            {'label': 'cat', 'confidence': 0.85, 'box': {'x': 0.425, 'y': 0.465, 'width': 0.086, 'height': 0.04}},
            {'label': 'person', 'confidence': 0.68, 'box': {'x': 0.422, 'y': 0.46, 'width': 0.092, 'height': 0.064}},
        ]


def test_live_cycle_drops_the_person_box_and_logs_why(tmp_path, monkeypatch):
    """A camera watching only people: the cat is not watched, but it still
    explains the person box, which never reaches the alert rules."""
    from tests.test_live_track_ordering import _load_app

    main = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as _lm
    main._state.live_detection_history.pop('camera-drive', None)
    monkeypatch.setattr(main._state, 'detector', _CatAndPersonDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))
    monkeypatch.setattr(_lm, 'filter_detections_by_motion_mode', lambda detections, *a, **k: detections)
    camera = {
        'id': 'camera-drive', 'name': 'Driveway',
        'detection': {'object_detection_enabled': True, 'zones': [{
            'id': 'drive', 'name': 'Driveway', 'enabled': True, 'monitor_objects': True,
            'x': 0, 'y': 0, 'width': 1, 'height': 1,
            'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5}],
        }]},
        'recording': {'continuous': False},
    }
    _lm.process_live_stream_alerts(
        np.full((720, 1280, 3), 20, dtype=np.uint8),
        {'width': 1280, 'height': 720, 'timestamp': time.time()}, camera, enforce_interval=False,
    )
    history = main._state.live_detection_history.get('camera-drive')
    assert history and not [d for d in history[-1][1] if d.get('label') == 'person']
    assert ('person', 'Same object as cat (85%)') in [(d['label'], d['reason']) for d in history[-1][2]]
