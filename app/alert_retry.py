"""Bounded retry policy shared by the alert transports (SMTP and ntfy).

Only *transient* failures are retried: the server could not be reached (DNS,
refused connection, connect/read timeout, a dropped greeting) or ntfy answered
5xx/429. A rejected password, an invalid recipient, a certificate problem or a
4xx response will not get better by waiting, so those are raised on the first
attempt.

Retries repeat a *connection*, never an already-accepted message: email retries
only the session setup (nothing has been handed to the server yet, so an alert
cannot be sent twice) and push repeats a request the server did not accept.
"""

from __future__ import annotations

import time

# Three tries ride out the common blip (relay restart, packet loss, a rate
# limit) without stalling the notification pool: 0.5s then 1.0s of backoff.
ALERT_DELIVERY_ATTEMPTS = 3
ALERT_RETRY_BASE_SECONDS = 0.5


def retry_backoff_seconds(attempt: int) -> float:
    """Delay before the retry that follows the 1-based ``attempt``."""
    return ALERT_RETRY_BASE_SECONDS * (2 ** max(0, int(attempt) - 1))


def sleep_before_retry(attempt: int) -> None:
    """Sleep the backoff for ``attempt``; a seam tests replace with a no-op."""
    time.sleep(retry_backoff_seconds(attempt))
