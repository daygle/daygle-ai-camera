"""FastAPI Depends() providers for application-scoped singletons."""
from __future__ import annotations

from fastapi import Request

import app.state as _state


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
