"""FastAPI Depends() providers for application-scoped singletons."""
from __future__ import annotations
from fastapi import Request
from app.database import EventDatabase
from app.auth import AuthService
from app.recordings import RecordingService


def get_database(request: Request) -> EventDatabase:
    return request.app.state.database


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def get_config(request: Request) -> dict:
    return request.app.state.config


def get_cameras_config(request: Request) -> list:
    return request.app.state.cameras_config


def get_recording_service(request: Request) -> RecordingService:
    return request.app.state.recording_service
