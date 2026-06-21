"""FastAPI Depends() providers for application-scoped singletons."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import Request

import app.state as _state
from app.config_facades import (
    effective_email_alert_settings,
    effective_push_notification_settings,
)
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
    """Active AI detector reloader.

    Returns ``(reloaded: bool, error: str | None)`` after attempting to
    swap in the configured ONNX model. Routers consume it via
    ``Depends(...)`` instead of importing it through the ``app.main``
    back-compat rebind.
    """
    return reload_detector


# Fields that hold credentials / secrets and must never be returned to a
# non-admin caller via the settings read endpoints. Kept as a
# module-level constant so future sensitive keys (token, api_key,
# secret) can be added in one place without per-router updates --
# ``get_redacted_email_alert_settings`` and
# ``get_redacted_push_notification_settings`` both consume this list.
SENSITIVE_SETTING_FIELDS: tuple[str, ...] = ('password',)

# Role string for an admin caller. Mirrors the bare string used by
# ``require_admin`` in ``app.auth_gates`` for the same comparison;
# duplicated here so this module stays free of cross-module imports
# purely for a string constant.
ADMIN_ROLE = 'admin'


def _filter_sensitive_settings(settings: dict[str, Any], *, role: str | None) -> dict[str, Any]:
    """Return a shallow copy of *settings* with sensitive credentials
    stripped when *role* is not ``'admin'``.

    Admin callers receive the ORIGINAL dict (no copy path, so write-back
    and round-trip flows stay on the un-mangled shape). Non-admin
    callers receive a NEW shallow-copy dict with the field names in
    :data:`SENSITIVE_SETTING_FIELDS` removed. Missing keys are tolerated
    (``dict.pop(field, None)``) so adding a new sensitive field stays
    backwards-compatible with settings dicts that pre-date the field.

    Safe to call on the deep-copied dict produced by the
    ``effective_*_settings()`` helpers in ``app.config_facades``, which
    is the canonical input shape. Mutating the returned dict does not
    affect the underlying ``state.config`` or stored DB override
    because the source-of-truth containers are not aliased into the
    returned shape -- the deep-copy in ``effective_*_settings()``
    detaches every reference.
    """
    if role == ADMIN_ROLE:
        return settings
    redacted = dict(settings)
    for field in SENSITIVE_SETTING_FIELDS:
        redacted.pop(field, None)
    return redacted


def get_redacted_email_alert_settings(request: Request) -> dict[str, Any]:
    """Effective email alert settings, with ``password`` stripped for
    non-admin callers.

    Reverse-closes the security gap the 40fc988 admin gate was reaching
    for: the SMTP password used to be returned in plaintext to any
    logged-in user on ``GET /api/settings/alert-email``. Admin still
    sees the full dict (necessary for the settings page to round-trip
    the password back to ``PUT``); viewer / future non-admin roles
    see the same dict minus the ``password`` key.
    """
    settings = effective_email_alert_settings()
    user = getattr(request.state, 'user', None) or {}
    return _filter_sensitive_settings(settings, role=user.get('role'))


def get_redacted_push_notification_settings(request: Request) -> dict[str, Any]:
    """Effective push notification settings, with ``password`` stripped
    for non-admin callers. See
    :func:`get_redacted_email_alert_settings` for the rationale.
    """
    settings = effective_push_notification_settings()
    user = getattr(request.state, 'user', None) or {}
    return _filter_sensitive_settings(settings, role=user.get('role'))
