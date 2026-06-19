"""Utility APIRouter.

Extracted from ``app/main.py`` lines 2772-2796 (Phase-12 of the
hybrid-pattern router split). Same template as ``app/api/live_router.py``
(Phase-10) and ``app/api/admin_router.py`` (Phase-11): ``import app.main
as main`` at module level, every global / helper read through
``main.<name>`` *inside* handler bodies.

This file deliberately bundles THREE single-handler utility routes into a
single router rather than splitting them one-per-file:

- ``GET    /api/stats``      -- ``stats``
- ``GET    /api/labels``     -- ``available_labels``
- ``DELETE /api/objects``    -- ``delete_all_objects`` (admin-only)

The motivation: each individual handler is too small to justify its
own router file (extracted, they'd each be ~10 lines of code), yet each
has its own conceptual domain (aggregations, label discovery, runtime
reset). Combining them into one ``utility_router`` follows the
Phase-11 admin_router precedent of grouping same-size admin-style
handlers into a single router, while leaving the more complex pairs
(camera_log / update) to their own dedicated files because their
composition warrants the dedicated-file treatment.

BODY-REWRITE NOTE
Handlers in the original ``main.py`` referenced module-level state in
main.py via bare names (``database``, ``cameras_config``,
``effective_ai_config``, ``SOUND_CLASSES``, ``require_admin``). After
extraction to this router, those bare names resolve to ZERO attributes
in our namespace -- handlers would NameError at request time. Per
hybrid-pattern uniformity (rule 5 of ``app/api/__init__.py``), each
bare call is rewritten as ``main.<bare>``.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.database`` -- EventDatabase instance; ``.stats()`` returns the
  aggregated counts, ``.delete_all_objects()`` purges the
  detected-objects table.
- ``main.cameras_config`` -- module-level list of effective camera
  configs; ``len(main.cameras_config)`` is appended to the stats
  response as ``total_cameras``.
- ``main.effective_ai_config`` -- reading-setter that returns the
  merged AI config dict for the current run; its ``labels_path`` key
  points at the COCO labels file ``available_labels`` reads.
- ``main.SOUND_CLASSES`` -- module-level dict of sound-class metadata;
  ``available_labels`` indexes it to build the ``sounds`` field.
- ``main.require_admin`` -- admin gate shared with the audit + runtime-data
  DELETE handlers; only ``delete_all_objects`` uses it here.

``Path`` (pathlib) is imported as ``from pathlib import Path`` directly
-- it's stdlib, NOT app state.

``available_labels`` deliberately swallows the
``Exception`` from the labels-file read (the original wrapped the same
``try/except: pass`` to gracefully degrade when the COCO labels file
is missing on disk -- e.g. in fresh installs where the AI model has
not been exported yet). The router preserves that behavior verbatim.

Tests go through ``LocalClient.request`` rather than calling
``main.stats`` / ``main.available_labels`` /
``main.delete_all_objects`` directly, so no back-compat alias on
``app.main`` is needed. The Phase-7.1 invariant
``tests/test_api_router_split_invariants.py::test_app_api_imports_in_main_are_consumed``
will catch any orphan-import regression if a future refactor drops the
``from app.api.utility_router import router as utility_router`` rebind
line in ``app/main.py``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

import app.main as main

router = APIRouter()


@router.get('/api/stats')
def stats():
    result = main.database.stats()
    result['total_cameras'] = len(main.cameras_config)
    return result


@router.get('/api/labels')
def available_labels():
    """Return available labels for the recordings filter dropdown."""
    object_labels: list[str] = []
    ai_config = main.effective_ai_config()
    labels_path = ai_config.get('labels_path', 'models/coco.names')
    try:
        p = Path(labels_path)
        if p.exists():
            object_labels = [line.strip() for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    except Exception:
        pass
    sound_labels = [
        {'id': class_id, 'label': meta['label'], 'description': meta.get('description', '')}
        for class_id, meta in main.SOUND_CLASSES.items()
    ]
    return {'objects': object_labels, 'sounds': sound_labels}


@router.delete('/api/objects')
def delete_all_objects(request: Request):
    main.require_admin(request)
    deleted = main.database.delete_all_objects()
    return {'ok': True, 'deleted': deleted}
