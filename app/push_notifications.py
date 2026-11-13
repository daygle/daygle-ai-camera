from __future__ import annotations

import base64
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app import alert_retry
from app.alert_formatting import build_alert_content


class PushNotificationError(Exception):
    pass


class PushNotificationUnavailable(PushNotificationError):
    """ntfy could not be reached (or answered 5xx/429): the request was not accepted.

    Separate from ``PushNotificationError`` so the dispatcher can queue the
    push for a later retry (app.alert_outbox); a 4xx response is a
    configuration problem that a retry would only repeat.
    """


# Failures are logged here with their cause, not only raised as
# PushNotificationError, so a misconfigured ntfy server can be diagnosed from
# app.log.
logger = logging.getLogger('daygle.notifications')

# ntfy tag carrying the triggering event's id on alert pushes, e.g.
# ``daygle-event-42``. The Android app reads it to open that event's snapshot
# when the notification is tapped, instead of parsing the message text.
EVENT_TAG_PREFIX = 'daygle-event-'


def _encode_ntfy_header(value: str) -> str:
    """Percent-encode non-ASCII characters for ntfy HTTP header values.

    ntfy decodes percent-encoded UTF-8 before displaying (e.g. in the
    push notification title). RFC 2047 encoded-words are NOT appropriate
    here - those are an email-layer convention; HTTP clients display them
    as literal ``=?utf-8?q?...?=`` blobs rather than decoding them.
    Pure-ASCII values pass through unchanged.
    """
    # A CR/LF from a camera or rule name would be rejected by http.client as
    # an invalid header value (failing the push), so fold line breaks first.
    value = ' '.join(str(value).splitlines())
    if any(ord(c) > 127 for c in value):
        return urllib.parse.quote(value, safe=" ,.:;-_/!?()[]@#$&*+=")
    return value


class PushNotificationService:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    def configured(self) -> bool:
        return bool(self.settings.get("enabled") and self.settings.get("server_url") and self.settings.get("topic"))

    def send_alert(
        self,
        alert: dict[str, Any],
        *,
        event_id: int,
        camera_name: str | None = None,
        camera_id: str | None = None,
        triggered_labels: list[str] | None = None,
        detected_at: str | None = None,
        topics: list[str] | None = None,
    ) -> None:
        """Push one alert. ``topics`` are the chosen users' own ntfy topics
        (see ``app.alert_recipients.resolve_push_topics``); ``None`` or empty
        sends to the shared topic from Settings. A failing topic does not stop
        delivery to the rest; the failures are raised together at the end."""
        if not self.configured():
            return

        # Use the shared formatter so the push notification matches the email
        # alert exactly - same title (with camera), title-cased message, and the
        # same Camera / Zone / Detection Type / Rule / Detected / All triggers /
        # Confidence / Event ID body. ``camera_id`` is accepted for call-site
        # compatibility but, like email, the body shows only the camera name.
        content = build_alert_content(
            alert,
            event_id=event_id,
            camera_name=camera_name,
            triggered_labels=triggered_labels,
            detected_at=detected_at,
            motion_state=alert.get('motion_state'),
        )
        tags = [f'{EVENT_TAG_PREFIX}{int(event_id)}']
        self._deliver_to_topics(content.subject, content.plain_text, tags=tags, topics=topics)

    def _deliver_to_topics(
        self, title: str, body: str, *, tags: list[str] | None = None, topics: list[str] | None = None,
    ) -> None:
        if not topics:
            self._deliver(title, body, tags=tags)
            return
        errors: list[str] = []
        delivered = 0
        unavailable = True
        for topic in dict.fromkeys(topics):
            try:
                self._deliver(title, body, tags=tags, topic=topic)
                delivered += 1
            except PushNotificationUnavailable as exc:
                errors.append(f'{topic}: {exc}')
            except PushNotificationError as exc:
                errors.append(f'{topic}: {exc}')
                unavailable = False
        if errors:
            message = '; '.join(errors)
            # Only a batch where nothing was accepted is safe to queue: a
            # retry of it cannot duplicate the topics that already got the
            # push, and when some did the dispatcher must not queue any.
            if unavailable and delivered == 0:
                raise PushNotificationUnavailable(message)
            raise PushNotificationError(message)

    def send_test(self, topics: list[str] | None = None) -> None:
        """Send the test notification to ``topics`` (users' own topics), or
        to the shared topic when none are given."""
        if not self.configured():
            raise PushNotificationError("Push notifications are not configured.")
        self._deliver_to_topics(
            "Daygle AI Camera test notification",
            "If you received this, your push notification settings are working.",
            topics=topics,
        )

    def _deliver(self, title: str, body: str, *, tags: list[str] | None = None, topic: str | None = None) -> None:
        server_url = str(self.settings.get("server_url", "")).rstrip("/")
        topic = str(topic if topic else self.settings.get("topic", "")).strip()
        priority = str(self.settings.get("priority", "default")).strip() or "default"
        username = str(self.settings.get("username") or "").strip()
        password = str(self.settings.get("password") or "").strip()

        # Percent-encode the topic: a space or reserved character would otherwise
        # produce an invalid request line that http.client rejects.
        url = f"{server_url}/{urllib.parse.quote(topic, safe='')}"
        headers: dict[str, str] = {
            "Title": _encode_ntfy_header(title),
            "Priority": priority,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if tags:
            headers["Tags"] = ",".join(tags)
        if username:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"

        request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        attempts = max(1, int(alert_retry.ALERT_DELIVERY_ATTEMPTS))
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=10):
                    return
            except urllib.error.HTTPError as exc:
                # 5xx (server temporarily unable) and 429 (rate limited) are
                # worth another try; any other 4xx is a configuration problem
                # a retry would only repeat.
                retryable = exc.code == 429 or exc.code >= 500
                if retryable and attempt < attempts:
                    delay = alert_retry.retry_backoff_seconds(attempt)
                    logger.warning(
                        'ntfy push delivery failed (attempt %d/%d): server=%s status=%s %s; '
                        'retrying in %.1fs',
                        attempt, attempts, url, exc.code, exc.reason, delay,
                    )
                    alert_retry.sleep_before_retry(attempt)
                    continue
                logger.warning(
                    'ntfy push delivery failed: server=%s status=%s reason=%s',
                    url, exc.code, exc.reason,
                )
                error_type = PushNotificationUnavailable if retryable else PushNotificationError
                raise error_type(f"ntfy server returned {exc.code}: {exc.reason}") from exc
            # Network-layer failures only (DNS, connect, timeout, TLS), so
            # programming errors are not reported as "ntfy delivery failed".
            # HTTPError is a URLError subclass and is handled above first.
            except (urllib.error.URLError, OSError) as exc:
                if attempt < attempts:
                    delay = alert_retry.retry_backoff_seconds(attempt)
                    logger.warning(
                        'ntfy push delivery failed (attempt %d/%d): server=%s exc=%s: %s; '
                        'retrying in %.1fs',
                        attempt, attempts, url, type(exc).__name__, exc, delay,
                    )
                    alert_retry.sleep_before_retry(attempt)
                    continue
                logger.warning(
                    'ntfy push delivery failed: server=%s exc=%s: %s',
                    url, type(exc).__name__, exc,
                )
                raise PushNotificationUnavailable(str(exc)) from exc
