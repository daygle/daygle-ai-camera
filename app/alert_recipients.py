"""Resolve alert recipients to the email addresses and ntfy topics to use.

A recipient entry is either a literal email address or a ``user:<id>``
reference to a user account (see ``app.utils.normalize_recipient_entry``).
References are looked up here, at send time, so an alert always goes to the
user's current address. Shared by rule alerts (``app.alert_dispatch``) and
camera offline/online alerts (``app.camera_health``). Push recipients are
``user:<id>`` references only and resolve to each user's own ntfy topic.
"""

from __future__ import annotations

import logging
from typing import Any

import app.state as _state
from app.utils import user_recipient_id

logger = logging.getLogger('daygle.ai')


def _users_by_id(
    entries: list[str], users: list[dict[str, Any]] | None,
) -> dict[int, dict[str, Any]]:
    """Index ``users`` by id, loading them from the auth store when not given
    and some entry is a ``user:<id>`` reference."""
    if users is None and any(user_recipient_id(entry) is not None for entry in entries):
        auth = getattr(_state, 'auth', None)
        try:
            users = auth.list_users() if auth is not None else []
        except Exception as exc:  # the auth store is down: still send to literal addresses
            logger.warning('Could not load users to resolve alert recipients: %s', exc)
            users = []
    return {int(user['id']): user for user in users or [] if user.get('id') is not None}


def _active_user(users_by_id: dict[int, dict[str, Any]], entry: str) -> dict[str, Any] | None:
    user_id = user_recipient_id(entry)
    user = users_by_id.get(user_id) if user_id is not None else None
    return user if user and user.get('is_active', 1) else None


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
    users_by_id = _users_by_id(entries, users)
    resolved: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if user_recipient_id(entry) is not None:
            user = _active_user(users_by_id, entry)
            if user is None:
                continue
            entry = str(user.get('email') or '').strip()
        if '@' not in entry or entry.lower() in seen:
            continue
        seen.add(entry.lower())
        resolved.append(entry)
    return resolved


def resolve_push_topics(
    recipients: list[str] | None,
    users: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Turn a rule's push recipients into the ntfy topics to send to now.

    Push recipients are ``user:<id>`` references only; each resolves to that
    user's CURRENT ntfy topic (set on their profile). Deactivated or deleted
    users, and users without a topic, are skipped. Duplicates are dropped.
    An empty result means nobody chosen can receive the push; callers use the
    shared Settings topic only when no recipients are configured at all.
    """
    entries = [str(entry or '').strip() for entry in recipients or []]
    users_by_id = _users_by_id(entries, users)
    topics: list[str] = []
    for entry in entries:
        user = _active_user(users_by_id, entry)
        topic = str((user or {}).get('ntfy_topic') or '').strip()
        if topic and topic not in topics:
            topics.append(topic)
    return topics
