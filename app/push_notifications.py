from __future__ import annotations

import base64
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.alert_formatting import build_alert_content


class PushNotificationError(Exception):
    pass


# LOW fix (round-8): module-level logger so ntfy delivery failures don't
# disappear into PushNotificationError without a paper trail. The alert
# dispatch path raises the error AND logs the original cause here so an
# operator whose ntfy-config is misconfigured can debug from app.log
# without redeploying with extra logging.
logger = logging.getLogger('daygle.notifications')


def _encode_ntfy_header(value: str) -> str:
    """Percent-encode non-ASCII characters for ntfy HTTP header values.

    ntfy decodes percent-encoded UTF-8 before displaying (e.g. in the
    push notification title). RFC 2047 encoded-words are NOT appropriate
    here - those are an email-layer convention; HTTP clients display them
    as literal ``=?utf-8?q?...?=`` blobs rather than decoding them.
    Pure-ASCII values pass through unchanged.
    """
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
    ) -> None:
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
        )
        self._deliver(content.subject, content.plain_text)

    def send_test(self) -> None:
        if not self.configured():
            raise PushNotificationError("Push notifications are not configured.")
        self._deliver("Daygle AI Camera test notification", "If you received this, your push notification settings are working.")

    def _deliver(self, title: str, body: str) -> None:
        server_url = str(self.settings.get("server_url", "")).rstrip("/")
        topic = str(self.settings.get("topic", "")).strip()
        priority = str(self.settings.get("priority", "default")).strip() or "default"
        username = str(self.settings.get("username") or "").strip()
        password = str(self.settings.get("password") or "").strip()

        url = f"{server_url}/{topic}"
        headers: dict[str, str] = {
            "Title": _encode_ntfy_header(title),
            "Priority": priority,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if username:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"

        request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except urllib.error.HTTPError as exc:
            logger.warning(
                'ntfy push delivery failed: server=%s status=%s reason=%s',
                url, exc.code, exc.reason,
            )
            raise PushNotificationError(f"ntfy server returned {exc.code}: {exc.reason}") from exc
        # LOW fix (round-8): narrow the catch to the network-layer exception
        # triple. ``Exception`` was hiding ``KeyError`` / ``TypeError`` /
        # ``AttributeError`` programmer errors under a misleading
        # "ntfy delivery failed" headline. Network-layer issues
        # (``URLError``, ``socket.timeout``-as-``TimeoutError``,
        # ``OSError`` covering DNS / connect failures) are exactly what
        # belongs in this except. ``HTTPError`` is a ``URLError``
        # subclass so it's already covered by the explicit handler above.
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.warning(
                'ntfy push delivery failed: server=%s exc=%s: %s',
                url, type(exc).__name__, exc,
            )
            raise PushNotificationError(str(exc)) from exc
