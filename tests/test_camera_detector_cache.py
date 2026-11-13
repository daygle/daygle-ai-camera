"""Regression test: one detector per assigned model, even on a cold race.

``camera_models.camera_detector`` used to build a per-camera model detector
outside its cache lock and publish it with ``setdefault``. Two cameras assigned
the same model can run their first cycle at the same time (the inference
scheduler admits several workers), so both missed the cold cache and loaded the
model twice: two ONNX sessions for one model -- a second full copy of its
weights and arena until GC -- with one instance silently discarded. The build
now happens under the lock; the cache-hit fast path stays lock-free.
"""

from __future__ import annotations

import threading
import time

import app.camera_models as camera_models


def test_concurrent_cold_lookups_build_one_detector(monkeypatch):
    import app.detector as detector_module

    monkeypatch.setattr(camera_models, '_detector_cache', {})
    monkeypatch.setattr(
        camera_models, 'camera_model_assignment',
        lambda _settings: ('models/yolo11n.onnx', 'models/coco.names'),
    )
    built: list[dict] = []

    def _fake_create(config):
        built.append(config)
        # Hold the build so an unguarded second caller passes the cold-cache
        # check while this one is still loading.
        time.sleep(0.15)
        return {'built': len(built)}

    monkeypatch.setattr(detector_module, 'create_detector', _fake_create)

    ai_config = {'model_path': 'models/yolo11s.onnx', 'labels_path': 'models/coco.names'}
    barrier = threading.Barrier(2)
    results: list[object] = []

    def _lookup() -> None:
        barrier.wait(2)
        results.append(camera_models.camera_detector({}, ai_config))

    threads = [threading.Thread(target=_lookup) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(built) == 1, 'the model must be loaded once, not once per caller'
    assert results[0] is results[1]
