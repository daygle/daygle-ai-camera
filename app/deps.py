"""FastAPI Depends() providers for application-scoped singletons."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Request

import app.state as _state
from app.main import (
    apply_cameras_settings,
    apply_storage_and_recording_settings,
    reload_detector,
)


def get_database(request: Request):
    return _state.database


def get_auth(request: Request):
    return _state.auth


def get_config(request: Request) -> dict:
    return _state.config


def get_cameras_config(request: Request) -> list:
    return _state.cameras_config


def get_recording_service(request: Request):
    return _state.recording_service


def get_auth_enabled(request: Request) -> bool:
    """Whether authentication is enabled in the resolved auth settings.

    Computed at request time from ``state.auth_config`` so it tracks any
    runtime override applied via ``auth.apply_config(...)``.
    """
    return bool(_state.auth_config.get('enabled', True))


def get_logger(request: Request) -> logging.Logger:
    """The application's named logger (``daygle.ai``)."""
    return logging.getLogger('daygle.ai')


def get_web_dir(request: Request) -> Path:
    """Path to the static ``web/`` directory shipped alongside ``app/``.

    Resolved relative to this file so the provider remains valid even if
    the working directory is changed at runtime (tests set
    ``DAYGLE_CONFIG`` and may chdir into tmp paths).
    """
    return Path(__file__).resolve().parent.parent / 'web'


def get_detector(request: Request):
    """Active AI detector (Pool A rebind of ``state.detector``)."""
    return _state.detector


def get_apply_cameras_settings(request: Request):
    """Routers-injectable camera-settings mutator.

    Reads/writes ``state.cameras_config`` (and the camera-health / camera
    runtime state) in place. Routers receive the live callable via
    ``Depends(...)`` rather than importing it via ``from app.main import``
    so the dependency is named, swappable in tests, and inspectable in
    OpenAPI tooling.
    """
    return apply_cameras_settings


def get_apply_storage_and_recording_settings(request: Request):
    """Routers-injectable storage + recording settings mutator.

    Pushes the latest on-disk + database-override storage and recording
    config into the running services (recording service, snapshot
    directory, retention policy). Routers consume it via ``Depends(...)``
    to keep the write path explicit and test-mockable.
    """
    return apply_storage_and_recording_settings


def get_reload_detector(request: Request):
    """Routers-injectable AI detector reloader.

    Returns ``(reloaded: bool, error: str | None)`` after attempting to
    swap in the configured ONNX model. Routers consume it via
    ``Depends(...)`` instead of importing it through the ``app.main``
    back-compat rebind.
    """
    return reload_detector
