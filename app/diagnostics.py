"""Camera diagnostics helper extracted from ``app/main.py`` (Phase-D).

Records operational camera/recording diagnostic events to the database.
Best-effort — never raises into the calling path so recording and detection
threads can't be broken by a logging failure.
"""

from __future__ import annotations

import logging
from typing import Any

import app.state as _state
from app.auth import utc_now

logger = logging.getLogger('daygle.ai')


def log_camera_diagnostic(
    camera_id: str | None,
    event_type: str,
    message: str = '',
    *,
    severity: str = 'info',
    details: dict[str, Any] | None = None,
    camera_name: str | None = None,
) -> None:
    """Record a system-generated camera/recording diagnostic event."""
    try:
        if camera_name is None and camera_id:
            cfg = next(
                (c for c in _state.cameras_config if str(c.get('id') or '') == str(camera_id)),
                None,
            )
            if cfg:
                camera_name = str(cfg.get('name') or '').strip() or None
        _state.database.add_camera_diagnostic(
            created_at=utc_now(),
            camera_id=str(camera_id) if camera_id else None,
            camera_name=camera_name,
            event_type=event_type,
            severity=severity,
            message=message,
            details=details,
        )
    except Exception as exc:
        logger.debug('Failed to write camera diagnostic (%s/%s): %s', camera_id, event_type, exc)
