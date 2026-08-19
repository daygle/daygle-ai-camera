"""Downloadable face-embedding model catalog (Stage 2c).

Unlike the YOLO detection catalog (`app.ai_settings.YOLO_MODELS`, which exports
ONNX from Ultralytics weights), face-embedding models are pre-built ONNX files
fetched directly from a **fixed, trusted URL** baked into this catalog. The URL
is never operator-supplied -- callers select a model by its catalog id, so there
is no server-side-request-forgery surface.

Every entry declares the embedding ``dim`` and ``input_size`` the model emits so
the recognition service and the ``person_faces`` rows stay consistent, and the
``model_id`` that tags stored embeddings (vectors are only ever matched against
the same ``model_id``).

Licensing: the bundled ArcFace R100 models come from the ONNX Model Zoo
(https://github.com/onnx/models) under the **Apache-2.0** license, which permits
commercial use. See ``docs/face-recognition.md`` for the attribution notice.
"""
from __future__ import annotations

from typing import Any

# ONNX Model Zoo stores these as Git LFS objects; the media host serves the real
# file (the github.com/.../raw/... path returns an LFS pointer/redirect).
_ONNX_ZOO_ARCFACE = (
    'https://media.githubusercontent.com/media/onnx/models/main/'
    'validated/vision/body_analysis/arcface/model'
)

EMBEDDING_MODELS: dict[str, dict[str, Any]] = {
    'arcface-r100': {
        'model_id': 'arcface-r100',
        'onnx': 'arcface-r100.onnx',
        'url': f'{_ONNX_ZOO_ARCFACE}/arcfaceresnet100-8.onnx',
        'label': 'ArcFace R100 · Apache-2.0',
        'dim': 512,
        'input_size': 112,
        'approx_mb': 249,
        'license': 'Apache-2.0',
        'source': 'ONNX Model Zoo (https://github.com/onnx/models)',
        'description': 'High-accuracy ArcFace ResNet100 face embeddings. Apache-2.0 (commercial use permitted).',
    },
    'arcface-r100-int8': {
        # Same embedding space as the fp32 model (a quantisation of the same
        # weights), so it shares the ``model_id`` -- enrolments made with one
        # remain matchable after switching precision.
        'model_id': 'arcface-r100',
        'onnx': 'arcface-r100-int8.onnx',
        'url': f'{_ONNX_ZOO_ARCFACE}/arcfaceresnet100-11-int8.onnx',
        'label': 'ArcFace R100 · INT8 · Apache-2.0',
        'dim': 512,
        'input_size': 112,
        'approx_mb': 63,
        'license': 'Apache-2.0',
        'source': 'ONNX Model Zoo (https://github.com/onnx/models)',
        'description': 'Quantised ArcFace R100 (~4x smaller) for low-power CPU hosts. Apache-2.0.',
    },
}


def embedding_model_catalog(installed_check: Any = None, active_check: Any = None) -> list[dict[str, Any]]:
    """Return the catalog as a UI-friendly list, newest metadata only.

    ``installed_check`` is an optional callable ``(onnx_filename) -> bool`` used
    to flag which models are already downloaded. ``active_check`` is an optional
    callable ``(onnx_filename) -> bool`` used to flag which model recognition is
    currently pointed at, so the UI can mark it "Active" and offer "Use" on the
    others. The raw ``url`` is intentionally omitted from the returned rows (it
    is an internal detail, not something the UI needs).
    """
    rows: list[dict[str, Any]] = []
    for catalog_id, info in EMBEDDING_MODELS.items():
        row = {
            'id': catalog_id,
            'model_id': info['model_id'],
            'label': info['label'],
            'onnx': info['onnx'],
            'dim': info['dim'],
            'input_size': info['input_size'],
            'approx_mb': info['approx_mb'],
            'license': info['license'],
            'source': info['source'],
            'description': info['description'],
        }
        if installed_check is not None:
            row['installed'] = bool(installed_check(info['onnx']))
        if active_check is not None:
            row['active'] = bool(active_check(info['onnx']))
        rows.append(row)
    return rows
