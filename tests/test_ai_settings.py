"""Phase-20 integration tests for ``app/ai_settings.py``.

Phase-20 extracted the 3 AI-subsystem helpers (``ai_status_payload``,
``detector_status``, ``validate_ai_settings``) from ``app/main.py``
into ``app/ai_settings.py`` using the hybrid-pattern template (same
as Phase-16 ``app/auth_gates.py``, Phase-17
``app/config_facades.py``, Phase-18 ``app/camera_config.py``, Phase-19
``app/recording_settings.py``).

Internal ``main.py`` callers (``live_detection_status_payload``
L1158, ``process_live_stream_alerts`` L1442, ``log_detector_initialization``
L1653, ``detector_status`` itself L2512, ``_do_download_model`` L2827)
reference these as bare names inside function bodies; the top-of-file
Pool A rebind wires ``main.<name>`` before any of those bodies
evaluates.

Tests pin three contracts:

1. **Pool A back-compat identity.** The 3 Pool A rebinds MUST wire
   ``main.<name>`` to the SAME function object as
   ``app.ai_settings.<name>``. Re-resolved via ``sys.modules`` to
   defeat the ``tests/test_api.py::_load_app`` sys-modules-wipe state
   leak (Phase-17 lesson).
2. **Behavior of each facade.** Each helper has subtle ordering /
   fallback semantics:
   - ``ai_status_payload``: 4-way mode selection (``MODEL MISSING`` /
     ``MODEL FAILED`` / ``ONNX ACTIVE`` / ``MODEL FAILED`` again),
     backend mismatch detection, error precedence (in ``ONNX ACTIVE``
     mode ``error = detector.unavailable_reason`` WINS over
     ``last_detector_error``; in the other branches ``last_detector_error
     or detector_reason`` is the final value),
     ``model_name`` lookup in ``YOLO_MODELS``.
   - ``detector_status``: ``categories`` fallback to ``config['ai']``,
     ``available_labels`` from ``load_labels`` call, flat shape (NOT
     a strict superset of ai_settings + status fields).
   - ``validate_ai_settings``: 9 allowed-keys allow-list enforced,
     str-to-bool coercion, float 0-1 bounds + int 32-2048 bounds for
     ``input_size``, GPU mem negative-int rejection / 0-zero special
     case, ``device`` allow-list, ``inference_threads`` and
     ``max_concurrent_inferences`` int-range enforcement,
     TypeError/ValueError fallback for non-numeric input,
     ``HTTPException(400, ...)`` for ANY invalid.
3. **Top-level preload pattern.** ``import app.main`` BEFORE
   ``import app.ai_settings`` at module top -- same pattern as
   Phase-16 / 17 / 18 / 19 tests. Without this, pytest collection
   triggers the circular-import gate at ``app.ai_settings`` load time
   (its top has ``import app.main as main`` for the 11 Pool C reach
   sites).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest  # noqa: E402  -- used below

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Top-level lazy-ordered preloads to break the Phase-20 circular-import
# gate (same pattern as the 4 earlier phases' tests). Importing
# ``app.ai_settings`` FIRST would cause Python's fresh-load chain to run
# the top-of-file rebind ``from app.ai_settings import (...)`` inside
# ``app/main.py`` while ``app.ai_settings`` is still mid-load -> ImportError.
# Preloading ``app.main`` fully first populates ``sys.modules['app.main']``
# so ``app.ai_settings``'s own ``import app.main as main`` returns the
# cached module rather than triggering a recursive fresh-load chain.
import app.main  # noqa: E402  -- must precede the import below
import app.ai_settings as ai_settings  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Pool A back-compat identity -- ``main.<name> is ai_settings.<name>``.
#    Re-resolve via sys.modules per Phase-17 lesson (defeats the
#    tests/test_api.py::_load_app() sys-modules-wipe state leak).
# ---------------------------------------------------------------------------


@pytest.fixture
def main():
    """Return the CURRENT ``app.main`` module instance. See the module
    docstring for why we cannot rely on the test file's module-level
    globals directly. Centralised as fixtures so the rationale lives in
    one comment rather than copy-pasted into 3 tests."""
    return sys.modules["app.main"]


@pytest.fixture
def current_ai_settings():
    """Return the CURRENT ``app.ai_settings`` module instance. See the
    ``main`` fixture above for the leak rationale."""
    return sys.modules["app.ai_settings"]


@pytest.fixture
def ais():
    """Convenience alias for ``current_ai_settings`` -- used by the
    behavior tests below to call ``ais.ai_status_payload(...)`` etc.
    without ``import app.ai_settings as ais`` boilerplate."""
    return sys.modules["app.ai_settings"]


# ---------------------------------------------------------------------------
# 2. Helpers -- isolate cross-module deps via monkeypatched helpers.
# ---------------------------------------------------------------------------


class _DetectorStub:
    """Captures attribute lookups: ``backend``, ``available``,
    ``unavailable_reason`` -- the three attrs ``ai_status_payload``
    reads via ``getattr(main.detector, ...)``."""

    def __init__(
        self,
        backend: str = 'unknown',
        available: bool = False,
        unavailable_reason: object = None,
        active_precision: str = 'fp32',
    ) -> None:
        self.backend = backend
        self.available = available
        self.unavailable_reason = unavailable_reason
        self.active_precision = active_precision


def _install_ai_dependencies(
    monkeypatch,
    *,
    detector=None,
    effective_ai_config_value=None,
    detector_loaded_for=None,
    onnx_runtime_installed=True,
    model_exists=False,
    last_detector_error=None,
    yolo_models=None,
    active_ai_config_source='config',
):
    """Install hermetic stand-ins for the cross-module deps reached by
    ``ai_status_payload`` / ``detector_status`` / ``validate_ai_settings``.

    Targets are intentionally ``app.state`` (where ``_state.detector``,
    ``_state.last_detector_error``, ``_state.config`` resolve via the
    module-level ``import app.state as _state`` in ai_settings.py) and
    ``app.ai_settings`` (where the locally-defined helpers
    ``detector_loaded_for`` / ``onnx_runtime_installed`` / ``model_exists`` /
    ``active_ai_config_source`` and the top-of-file imports
    ``effective_ai_config`` / ``load_labels`` / the ``YOLO_MODELS``
    constant resolve as bare names inside function bodies).

    Patching ``main.<attr>`` would NOT intercept these calls: ai_settings
    is a Phase-20 extraction that does NOT do ``import app.main as main``
    inside its function bodies - it binds its deps directly from
    app.state / app.config_facades / app.detector at module load.

    Returns ``(ai_settings_module, ai_state, capture)`` where ``capture``
    is a dict collecting the per-helper call log so tests can introspect
    what was called. Tests that need to also patch ``load_labels`` and
    ``state.config`` (used by ``detector_status``) use the returned
    ``ai_settings_module`` / ``ai_state`` handles directly.
    """
    import app.ai_settings as ai_settings_module
    import app.state as ai_state

    capture: dict = {
        'detector_loaded_for_calls': [],
        'onnx_runtime_installed_calls': 0,
        'model_exists_calls': [],
        'active_ai_config_source_calls': 0,
    }

    if detector is None:
        detector = _DetectorStub()

    if effective_ai_config_value is None:
        effective_ai_config_value = {'backend': 'onnx', 'model_path': 'models/yolov8n.onnx'}

    # State attributes -- patch on app.state (the ``_state`` alias used by
    # ai_settings.py for ``_state.detector``, ``_state.last_detector_error``,
    # ``_state.config``).
    monkeypatch.setattr(ai_state, 'detector', detector)
    monkeypatch.setattr(ai_state, 'last_detector_error', last_detector_error)

    # Bare-name helpers + module-level constants -- patch on
    # app.ai_settings where the names actually resolve for function-body
    # lookups.
    monkeypatch.setattr(ai_settings_module, 'effective_ai_config', lambda: effective_ai_config_value)

    def _detector_loaded_for(settings):
        capture['detector_loaded_for_calls'].append(settings)
        return detector_loaded_for
    monkeypatch.setattr(ai_settings_module, 'detector_loaded_for', _detector_loaded_for)

    def _onnx_runtime_installed():
        capture['onnx_runtime_installed_calls'] += 1
        return onnx_runtime_installed
    monkeypatch.setattr(ai_settings_module, 'onnx_runtime_installed', _onnx_runtime_installed)

    # ``model_exists`` always installed as a callable (single semantic):
    # callers pass a bool that the stub returns regardless of input args.
    # This avoids the ``None`` dual semantic where ``None`` meant "make a
    # callable" vs a bool meaning "make a callable that returns this bool".
    def _model_exists(settings):
        capture['model_exists_calls'].append(settings)
        return bool(model_exists) if model_exists is not None else False
    monkeypatch.setattr(ai_settings_module, 'model_exists', _model_exists)

    if yolo_models is None:
        yolo_models = {
            'yolov8n': {'label': 'YOLOv8 nano', 'onnx': 'yolov8n.onnx'},
            'yolov8s': {'label': 'YOLOv8 small', 'onnx': 'yolov8s.onnx'},
        }
    monkeypatch.setattr(ai_settings_module, 'YOLO_MODELS', yolo_models)

    def _active_ai_config_source():
        capture['active_ai_config_source_calls'] += 1
        return active_ai_config_source
    monkeypatch.setattr(ai_settings_module, 'active_ai_config_source', _active_ai_config_source)

    return ai_settings_module, ai_state, capture


# -- ai_status_payload -----------------------------------------------------

def test_ai_status_payload_returns_onnx_active_when_configured_and_loaded(monkeypatch, ais):
    """``onnx`` configured AND detector shows onnx-active AND model exists
    -> ``mode='ONNX ACTIVE'`` and ``error = detector.unavailable_reason`` (None)
    """
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='onnx', available=True, unavailable_reason='would-be-error'),
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
        last_detector_error=None,
    )

    out = ais.ai_status_payload({'backend': 'onnx', 'model_path': 'models/yolov8n.onnx'})
    assert out['mode'] == 'ONNX ACTIVE'
    # `inference_available` mirrors `detector_loaded` (the helper arg).
    assert out['inference_available'] is True
    assert out['model_loaded'] is True
    assert out['model_exists'] is True
    assert out['onnx_runtime_installed'] is True
    assert out['active_backend'] == 'onnx'
    assert out['configured_backend'] == 'onnx'
    # error mirrors detector.unavailable_reason (no last_detector_error to override it)
    assert out['error'] == 'would-be-error'


def test_ai_status_payload_surfaces_active_precision_when_loaded(monkeypatch, ais):
    """When the model is loaded, ai_status_payload reports the detector's
    actual running precision (so the Status panel can flag int8/fp16 that
    silently fell back to fp32). When not loaded it stays None."""
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='onnx', available=True, active_precision='int8'),
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
    )
    out = ais.ai_status_payload({
        'backend': 'onnx',
        'model_path': 'models/yolov8n.onnx',
        'precision': 'int8',
    })
    assert out['model_loaded'] is True
    assert out['precision'] == 'int8'
    assert out['active_precision'] == 'int8'


def test_ai_status_payload_active_precision_none_when_not_loaded(monkeypatch, ais):
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='unknown', available=False, active_precision='int8'),
        detector_loaded_for=False,
        onnx_runtime_installed=True,
        model_exists=False,
    )
    out = ais.ai_status_payload({'backend': 'onnx', 'model_path': 'models/missing.onnx'})
    assert out['model_loaded'] is False
    assert out['active_precision'] is None


def test_ai_status_payload_distinguishes_int8_request_from_fp32_fallback(monkeypatch, ais):
    """An NMS-free INT8 request is reported as requested INT8 but active FP32.

    This is the operator-facing proof that the detector avoided the unsafe
    quantized graph instead of pretending that FP32 was the configured mode.
    """
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='onnx', available=True, active_precision='fp32'),
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
    )
    out = ais.ai_status_payload({
        'backend': 'onnx',
        'model_path': 'models/yolo26l-768.onnx',
        'precision': 'int8',
    })
    assert out['model_loaded'] is True
    assert out['precision'] == 'int8'
    assert out['active_precision'] == 'fp32'


def test_ai_status_payload_returns_model_missing_when_onnx_configured_but_file_absent(monkeypatch, ais):
    """``onnx`` configured but model doesn't exist on disk -> ``mode='MODEL MISSING'``
    with explicit error message pinning the configured path."""
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='unknown'),
        detector_loaded_for=False,
        onnx_runtime_installed=True,
        model_exists=False,  # forces MODEL MISSING branch
        last_detector_error=None,
    )

    out = ais.ai_status_payload({'backend': 'onnx', 'model_path': 'models/missing.onnx'})
    assert out['mode'] == 'MODEL MISSING'
    assert 'models/missing.onnx' in out['error']


def test_ai_status_payload_returns_model_failed_when_onnx_configured_but_detector_not_running(monkeypatch, ais):
    """``onnx`` configured, ``exists=True``, but detector backend mismatch or
    ``available=False`` -> ``mode='MODEL FAILED'`` (the post-MISSING branch)."""
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='onnx', available=False),  # model_loaded calc fails
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
    )

    out = ais.ai_status_payload({'backend': 'onnx', 'model_path': 'models/yolov8n.onnx'})
    assert out['mode'] == 'MODEL FAILED'


def test_ai_status_payload_returns_model_failed_for_non_onnx_backend(monkeypatch, ais):
    """Non-onnx backend (legacy) -> ``mode='MODEL FAILED'`` (the else branch).
    """
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='onnx'),
        detector_loaded_for=True,
        onnx_runtime_installed=False,
        model_exists=True,  # not loaded because backend != onnx
    )

    out = ais.ai_status_payload({'backend': 'yolov5'})  # non-onnx backend
    assert out['mode'] == 'MODEL FAILED'


def test_ai_status_payload_in_onnx_active_mode_error_mirrors_detector_reason(monkeypatch, ais):
    """In ``ONNX ACTIVE`` mode, the source DOES override the initial
    ``error = last_detector_error or detector_reason`` assignment with
    ``error = detector_reason`` (the ONNX ACTIVE branch sets it verbatim).
    So ``last_detector_error`` LOSES despite being truthy -- a subtle
    branch-specific override. Pin this behavior so future code touches
    stay aware that error precedence differs across the 4 mode branches.
    """
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(
            backend='onnx',
            available=True,
            unavailable_reason='reason-from-detector',
        ),
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
        last_detector_error='reason-from-last',
    )

    out = ais.ai_status_payload({'backend': 'onnx', 'model_path': 'models/yolov8n.onnx'})
    assert out['mode'] == 'ONNX ACTIVE'
    assert out['error'] == 'reason-from-detector'  # detector_reason WINS in this branch


def test_ai_status_payload_in_model_failed_branch_last_detector_error_wins(monkeypatch, ais):
    """In ``MODEL FAILED`` branch (config is onnx BUT detector not loaded),
    the source does NOT override the initial
    ``error = last_detector_error or detector_reason`` assignment --
    so ``last_detector_error`` WINS over ``detector.unavailable_reason``
    when set. Pin the branch where the precedence applies.
    """
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(
            backend='onnx',
            available=False,
            unavailable_reason='reason-from-detector',
        ),
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
        last_detector_error='reason-from-last',
    )

    out = ais.ai_status_payload({'backend': 'onnx', 'model_path': 'models/yolov8n.onnx'})
    assert out['mode'] == 'MODEL FAILED'
    assert out['error'] == 'reason-from-last'  # last_detector_error WINS in this branch


def test_ai_status_payload_resolves_model_name_from_yolo_models(monkeypatch, ais):
    """Lookups in ``YOLO_MODELS`` by filename suffix yield the label."""
    _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='onnx', available=True),
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
    )

    out = ais.ai_status_payload({'backend': 'onnx', 'model_path': '/abs/path/yolov8s.onnx'})
    assert out['model_name'] == 'YOLOv8 small'
    assert out['model_path'] == '/abs/path/yolov8s.onnx'


# -- detector_status ------------------------------------------------------


def test_detector_status_uses_ai_settings_categories_over_config_default(monkeypatch, ais):
    """When ``ai_settings['categories']`` is present, ``detector_status``
    uses it directly -- the ``config['ai']['categories']`` fallback is
    skipped.
    """
    ai_settings_module, ai_state, _capture = _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='onnx', available=True),
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
    )
    # Stub load_labels to return None so the helper falls back to the
    # categories list verbatim (avoiding any on-disk labels_path read).
    # Patch on app.ai_settings because detector_status binds the bare name
    # ``load_labels`` at module top via ``from app.detector import load_labels``.
    monkeypatch.setattr(ai_settings_module, 'load_labels', lambda labels_path, categories: None)
    # The fallback is state.config['ai']['categories'] -- make it different
    # from the inline ai_settings['categories'] to verify the fallback is NOT
    # used. Patch on app.state because detector_status reads it via
    # ``_state.config`` (the ``import app.state as _state`` alias).
    monkeypatch.setattr(ai_state, 'config', {'ai': {'categories': ['fallback-cat']}})

    out = ais.detector_status({'categories': ['inline-cat']})
    assert out['categories'] == ['inline-cat']
    assert out['available_labels'] == ['inline-cat']


def test_detector_status_falls_back_to_config_categories_when_ai_settings_omit_them(monkeypatch, ais):
    """When ``ai_settings`` does NOT carry ``categories``, fall back to
    ``config['ai']['categories']``.
    """
    ai_settings_module, ai_state, _capture = _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='onnx', available=True),
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
    )
    monkeypatch.setattr(ai_settings_module, 'load_labels', lambda labels_path, categories: None)
    monkeypatch.setattr(ai_state, 'config', {'ai': {'categories': ['config-cat']}})

    out = ais.detector_status({})  # no categories passed in
    assert out['categories'] == ['config-cat']
    assert out['available_labels'] == ['config-cat']


def test_detector_status_uses_load_labels_when_returns_labels(monkeypatch, ais):
    """When ``load_labels`` returns a list, ``available_labels`` is that
    list; the categories fallback is OVERRIDDEN by load_labels.
    """
    ai_settings_module, ai_state, _capture = _install_ai_dependencies(
        monkeypatch,
        detector=_DetectorStub(backend='onnx', available=True),
        detector_loaded_for=True,
        onnx_runtime_installed=True,
        model_exists=True,
    )
    monkeypatch.setattr(ai_settings_module, 'load_labels', lambda labels_path, categories: ['load-cat-1', 'load-cat-2'])
    monkeypatch.setattr(ai_state, 'config', {'ai': {'categories': ['config-cat']}})

    out = ais.detector_status({'categories': ['inline-cat']})
    assert out['available_labels'] == ['load-cat-1', 'load-cat-2']
    # categories itself is unchanged -- it's only available_labels that gets
    # the loaded-on-disk labels.
    assert out['categories'] == ['inline-cat']


# -- validate_ai_settings -------------------------------------------------

def test_validate_ai_settings_keeps_only_allowed_keys(monkeypatch, ais):
    """The allowed set is enforced: extra keys in ``payload`` are dropped,
    missing-but-allowed keys inherit from ``current``.
    """
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={
            'enabled': True, 'backend': 'onnx', 'confidence': 0.45,
            'iou_threshold': 0.5, 'model_path': 'models/yolov8n.onnx',
            'labels_path': 'models/coco.names', 'device': 'auto',
            'gpu_mem_limit': 0, 'inference_threads': 4, 'max_concurrent_inferences': 2,
        },
    )

    out = ais.validate_ai_settings({'unknown_evil_key': 'should_be_dropped', 'confidence': 0.9})
    assert 'unknown_evil_key' not in out
    assert out['confidence'] == 0.9  # updated value
    assert out['backend'] == 'onnx'  # current value, kept


def test_validate_ai_settings_coerces_enabled_string_truthy(monkeypatch, ais):
    """The ``enabled`` field accepts string forms '1', 'true', 'yes', 'on'
    (case-insensitive) and any non-empty truthy string -- exercised across
    the str -> bool coercion branch.
    """
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'enabled': True},
    )

    out = ais.validate_ai_settings({'enabled': 'YES'})
    assert out['enabled'] is True


def test_validate_ai_settings_coerces_enabled_string_falsy(monkeypatch, ais):
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'enabled': True},
    )
    out = ais.validate_ai_settings({'enabled': 'off'})
    assert out['enabled'] is False


def test_validate_ai_settings_rejects_non_onnx_backend(monkeypatch, ais):
    """Non-onnx backend raises HTTPException(400)."""
    from fastapi import HTTPException

    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'backend': 'onnx'},
    )
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'backend': 'yolov5'})
    assert exc_info.value.status_code == 400
    assert 'backend must be onnx' in exc_info.value.detail.lower()


def test_validate_ai_settings_rejects_confidence_above_one(monkeypatch, ais):
    """``confidence`` and ``iou_threshold`` must be in [0, 1]."""
    from fastapi import HTTPException

    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'confidence': 0.45, 'iou_threshold': 0.5},
    )
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'confidence': 1.5})
    assert exc_info.value.status_code == 400


def test_validate_ai_settings_accepts_inference_threads_in_range(monkeypatch, ais):
    """``inference_threads`` must be in [1, 32]; passing 8 should be accepted."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={},
    )
    out = ais.validate_ai_settings({'inference_threads': 8})
    assert out['inference_threads'] == 8


def test_validate_ai_settings_rejects_inference_threads_out_of_range(monkeypatch, ais):
    """``inference_threads=100`` is outside [1, 32] -> HTTPException(400)."""
    from fastapi import HTTPException

    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={},
    )
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'inference_threads': 100})
    assert exc_info.value.status_code == 400


def test_validate_ai_settings_drops_inference_threads_when_empty_string(monkeypatch, ais):
    """``inference_threads=''`` -> key popped from output (treated as unset)."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'inference_threads': 4},
    )
    out = ais.validate_ai_settings({'inference_threads': ''})
    assert 'inference_threads' not in out


def test_validate_ai_settings_picks_up_default_model_path_when_missing(monkeypatch, ais):
    """When neither ``payload`` nor ``current`` carry ``model_path`` /
    ``labels_path``, fall back to the package default paths."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={},
    )
    out = ais.validate_ai_settings({})
    assert out['model_path'] == 'models/yolo11n.onnx'
    assert out['labels_path'] == 'models/coco.names'


def test_validate_ai_settings_rejects_unknown_device(monkeypatch, ais):
    """``device='gpu'`` is not in ('auto', 'cpu', 'cuda') -> HTTPException(400)."""
    from fastapi import HTTPException

    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'device': 'auto'},
    )
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'device': 'gpu'})
    assert exc_info.value.status_code == 400
    assert 'device must be' in exc_info.value.detail


def test_validate_ai_settings_rejects_negative_gpu_mem_limit(monkeypatch, ais):
    """``gpu_mem_limit=-1`` triggers ValueError -> HTTPException(400)."""
    from fastapi import HTTPException

    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={},
    )
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'gpu_mem_limit': -1})
    assert exc_info.value.status_code == 400
    assert 'gpu_mem_limit must be' in exc_info.value.detail


def test_validate_ai_settings_accepts_zero_gpu_mem_limit(monkeypatch, ais):
    """``gpu_mem_limit=0`` is the canonical 'unlimited' zero case and is
    accepted (not interpreted as 'missing')."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={},
    )
    out = ais.validate_ai_settings({'gpu_mem_limit': 0})
    assert out['gpu_mem_limit'] == 0


def test_validate_ai_settings_accepts_max_concurrent_inferences_in_range(monkeypatch, ais):
    """``max_concurrent_inferences=8`` is in [1, 16] -> accepted."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={},
    )
    out = ais.validate_ai_settings({'max_concurrent_inferences': 8})
    assert out['max_concurrent_inferences'] == 8


def test_validate_ai_settings_rejects_non_numeric_confidence(monkeypatch, ais):
    """``confidence='abc'`` triggers the source's TypeError/ValueError
    fallback -> HTTPException(400). Exercises the ``except`` branch."""
    from fastapi import HTTPException

    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'confidence': 0.45},
    )
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'confidence': 'abc'})
    assert exc_info.value.status_code == 400
    assert 'confidence must be a number' in exc_info.value.detail


def test_validate_ai_settings_rejects_model_path_traversal(monkeypatch, ais):
    """A ``model_path`` that escapes the models/ directory is rejected."""
    from fastapi import HTTPException

    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'model_path': 'models/yolov8n.onnx'},
    )
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'model_path': '../etc/passwd'})
    assert exc_info.value.status_code == 400
    assert 'models/' in exc_info.value.detail


def test_validate_ai_settings_rejects_absolute_model_path_outside_models(monkeypatch, ais):
    """An absolute ``model_path`` outside models/ is rejected."""
    from fastapi import HTTPException

    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'model_path': 'models/yolov8n.onnx'},
    )
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'model_path': '/etc/passwd'})
    assert exc_info.value.status_code == 400


def test_validate_ai_settings_rejects_new_nonexistent_model_path(monkeypatch, ais):
    """Typing a new, in-bounds but non-existent model file is rejected with
    a helpful message instead of being silently persisted."""
    from fastapi import HTTPException

    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'model_path': 'models/coco.names'},
    )
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'model_path': 'models/definitely-not-here-xyz.onnx'})
    assert exc_info.value.status_code == 400
    assert 'not found' in exc_info.value.detail.lower()


def test_validate_ai_settings_allows_unchanged_missing_model_path(monkeypatch, ais):
    """Re-saving the current (not-yet-downloaded) model_path must succeed so
    other fields can be edited before a model is installed."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'model_path': 'models/ghost-model.onnx'},
    )
    # Same path as current + editing another field -> allowed (no existence error).
    out = ais.validate_ai_settings({'model_path': 'models/ghost-model.onnx', 'confidence': 0.6})
    assert out['model_path'] == 'models/ghost-model.onnx'
    assert out['confidence'] == 0.6


def test_validate_ai_settings_accepts_existing_installed_model_path(monkeypatch, ais):
    """Switching to an installed (existing, in-bounds) model file is accepted
    and canonicalised to a project-relative path."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'model_path': 'models/yolov8n.onnx'},
    )
    # coco.names ships in models/ so it exists in the checkout; used here purely
    # as a stand-in for an existing in-bounds file.
    out = ais.validate_ai_settings({'model_path': 'models/coco.names'})
    assert out['model_path'] == 'models/coco.names'


# -- execution_mode (CPU-only ORT executor toggle, tier-1 perf lever) -----


def test_validate_ai_settings_accepts_execution_mode_parallel(monkeypatch, ais):
    """``execution_mode='parallel'`` is the ORT default and the prior
    behavior; pinning that the whitelist round-trips it without coercion."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({'execution_mode': 'parallel'})
    assert out['execution_mode'] == 'parallel'


def test_validate_ai_settings_accepts_execution_mode_sequential(monkeypatch, ais):
    """``execution_mode='sequential'`` is the A/B lever for CPU-only
    ORT_SEQUENTIAL; whitelist accepts without rejecting the new key."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({'execution_mode': 'sequential'})
    assert out['execution_mode'] == 'sequential'


def test_validate_ai_settings_normalizes_execution_mode_case(monkeypatch, ais):
    """Case-insensitive normalization matches the existing ``device`` /
    ``backend`` lowering pattern."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({'execution_mode': 'SEQUENTIAL'})
    assert out['execution_mode'] == 'sequential'


def test_validate_ai_settings_rejects_invalid_execution_mode(monkeypatch, ais):
    """Anything outside ``{'parallel', 'sequential'}`` is rejected with
    HTTPException(400) so a typo can't silently fall back to a default."""
    from fastapi import HTTPException

    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'execution_mode': 'gpu-only'})
    assert exc_info.value.status_code == 400
    assert 'execution_mode' in exc_info.value.detail


def test_validate_ai_settings_persists_execution_mode_across_saves(monkeypatch, ais):
    """A previously-stored ``execution_mode`` survives a round-trip when
    no new value is supplied -- the merged-current-then-overlay logic in
    ``validate_ai_settings`` must not drop the key."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'execution_mode': 'sequential'},
    )
    out = ais.validate_ai_settings({'confidence': 0.7})
    assert out['execution_mode'] == 'sequential'
    assert out['confidence'] == pytest.approx(0.7)


# -- confidence_only_nms (tri-state 'auto' | 'on' | 'off') ---------------


def test_validate_ai_settings_normalizes_confidence_only_nms_auto(monkeypatch, ais):
    """'auto' persists as 'auto' so the detector applies its model-aware
    default (skip the redundant NMS for NMS-free YOLO26 heads)."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({'confidence_only_nms': 'auto'})
    assert out['confidence_only_nms'] == 'auto'


def test_validate_ai_settings_normalizes_confidence_only_nms_on(monkeypatch, ais):
    """Truthy forms ('on'/'true'/'yes'/'1'/bool True) all normalise to 'on'."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    assert ais.validate_ai_settings({'confidence_only_nms': 'on'})['confidence_only_nms'] == 'on'
    assert ais.validate_ai_settings({'confidence_only_nms': True})['confidence_only_nms'] == 'on'
    assert ais.validate_ai_settings({'confidence_only_nms': 'YES'})['confidence_only_nms'] == 'on'


def test_validate_ai_settings_normalizes_confidence_only_nms_off(monkeypatch, ais):
    """Falsy forms ('off'/'false'/bool False) all normalise to 'off'."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    assert ais.validate_ai_settings({'confidence_only_nms': 'off'})['confidence_only_nms'] == 'off'
    assert ais.validate_ai_settings({'confidence_only_nms': False})['confidence_only_nms'] == 'off'


def test_validate_ai_settings_confidence_only_nms_unknown_falls_back_auto(monkeypatch, ais):
    """An unrecognised value normalises to 'auto' rather than raising, so a
    stale/garbage config can't break the settings save."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({'confidence_only_nms': 'garbage'})
    assert out['confidence_only_nms'] == 'auto'


def test_validate_ai_settings_omits_confidence_only_nms_when_absent(monkeypatch, ais):
    """When neither ``current`` nor ``payload`` carry the key, it stays OMITTED
    from validate's output (the detector then treats it as 'auto'). Pinned so a
    future edit doesn't auto-populate a value for every deployment."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({})
    assert 'confidence_only_nms' not in out


def test_validate_ai_settings_explicit_off_turns_off_persisted_on(monkeypatch, ais):
    """An explicit 'off' switches OFF a previously-persisted 'on'. Because the
    control is now a tri-state select (not a checkbox), the form always sends
    an explicit value, so the override is reliably honoured either direction."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'confidence_only_nms': 'on'},
    )
    out = ais.validate_ai_settings({'confidence_only_nms': 'off'})
    assert out['confidence_only_nms'] == 'off'


def test_detector_status_surfaces_confidence_only_nms_default_auto(monkeypatch, ais):
    """detector_status normalises the value for the settings form; a config
    without the key surfaces 'auto' so the select shows the default."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    monkeypatch.setattr(ais, 'load_labels', lambda labels_path, categories: [])
    out = ais.detector_status({'backend': 'onnx', 'model_path': 'models/yolo26n.onnx'})
    assert out['confidence_only_nms'] == 'auto'


# -- precision (export-time fp16 + runtime int8) -----------------------


@pytest.mark.parametrize('value', ['fp32', 'fp16', 'int8'])
def test_validate_ai_settings_accepts_each_precision(monkeypatch, ais, value):
    """All three precision values are accepted through the whitelist;
    each maps to its specific detector / export branch."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({'precision': value})
    assert out['precision'] == value


def test_validate_ai_settings_rejects_invalid_precision(monkeypatch, ais):
    """Anything outside ``{'fp32', 'fp16', 'int8'}`` is rejected with
    HTTPException(400); typos can't silently fall back to fp32 via the
    API path (the detector still falls back at runtime, but the API
    surface should fail loud)."""
    from fastapi import HTTPException

    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    with pytest.raises(HTTPException) as exc_info:
        ais.validate_ai_settings({'precision': 'quant8'})
    assert exc_info.value.status_code == 400
    assert 'precision' in exc_info.value.detail


def test_validate_ai_settings_normalizes_precision_case(monkeypatch, ais):
    """Case-insensitive lowering matches the project-wide convention for
    every enum-validated setting (``device``, ``backend``,
    ``execution_mode``)."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({'precision': 'FP16'})
    assert out['precision'] == 'fp16'


def test_validate_ai_settings_persists_precision_across_saves(monkeypatch, ais):
    """A previously-stored ``precision`` survives a round-trip when the
    payload doesn't supply a new value -- the merged-current-then-overlay
    logic must keep the key (matters when shipping a config from a CUDA
    host to a CPU host -- the warning at load time is what surfaces the
    mismatch, not the validator dropping the setting)."""
    _install_ai_dependencies(
        monkeypatch,
        effective_ai_config_value={'precision': 'fp16'},
    )
    out = ais.validate_ai_settings({'confidence': 0.7})
    assert out['precision'] == 'fp16'
    assert out['confidence'] == pytest.approx(0.7)


# -- use_io_binding (ORT direct CUDA memory transfer, opt-in A/B) --------


def test_validate_ai_settings_accepts_use_io_binding_bool(monkeypatch, ais):
    """The toggle accepts a JSON bool directly -- the API path."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({'use_io_binding': True})
    assert out['use_io_binding'] is True


def test_validate_ai_settings_coerces_use_io_binding_string(monkeypatch, ais):
    """Same 'yes'/'on'/'true'/'1' string coercion as ``enabled`` /
    ``confidence_only_nms`` so the HTML form path Just Works."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({'use_io_binding': 'ON'})
    assert out['use_io_binding'] is True


def test_validate_ai_settings_omits_use_io_binding_when_absent(monkeypatch, ais):
    """Same omit-when-absent semantics as ``confidence_only_nms`` --
    default-False must win so the io_binding code path doesn't activate
    until the user explicitly asks for it."""
    _install_ai_dependencies(monkeypatch, effective_ai_config_value={})
    out = ais.validate_ai_settings({})
    assert 'use_io_binding' not in out
