"""Resolve alert email recipients to the addresses to send to.

A recipient entry is either a literal email address or a ``user:<id>``
reference to a user account (see ``app.utils.normalize_recipient_entry``).
References are looked up here, at send time, so an alert always goes to the
user's current address. Shared by rule alerts (``app.alert_dispatch``) and
camera offline/online alerts (``app.camera_health``).
"""

from __future__ import annotations

import logging
from typing import Any

import app.state as _state
from app.utils import user_recipient_id

logger = logging.getLogger('daygle.ai')


def resolve_email_recipients(
    recipients: list[str] | None,
    users: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Turn a rule's recipients into the email addresses to send to now.

    ``user:<id>`` entries resolve to that user's CURRENT email, so a changed
    address is picked up straight away and a deactivated or deleted user, or
    one without an address, is skipped. Literal addresses pass through.
    Duplicates (case-insensitive) are dropped, keeping the first.
    ``users`` defaults to the auth store's user list.
    """
    entries = [str(entry or '').strip() for entry in recipients or []]
    if users is None and any(user_recipient_id(entry) is not None for entry in entries):
        auth = getattr(_state, 'auth', None)
        try:
            users = auth.list_users() if auth is not None else []
        except Exception as exc:  # the auth store is down: still send to literal addresses
            logger.warning('Could not load users to resolve alert recipients: %s', exc)
            users = []
    users_by_id = {int(user['id']): user for user in users or [] if user.get('id') is not None}
    resolved: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        user_id = user_recipient_id(entry)
        if user_id is not None:
            user = users_by_id.get(user_id)
            if not user or not user.get('is_active', 1):
                continue
            entry = str(user.get('email') or '').strip()
        if '@' not in entry or entry.lower() in seen:
            continue
        seen.add(entry.lower())
        resolved.append(entry)
    return resolved
