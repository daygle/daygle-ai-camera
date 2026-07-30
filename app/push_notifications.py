from __future__ import annotations

import base64
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


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

        camera_name = str(camera_name or '').strip() or None
        camera_id = str(camera_id or '').strip() or None
        camera_bits = [bit for bit in (camera_name, camera_id) if bit]
        camera_line = ' / '.join(camera_bits) if camera_bits else 'Unknown camera'

        # Surface the full label set in the title so a multi-object event
        # (e.g. cat + person in one clip) reads as "Cat, Person detected".
        # Falls back to the single alert label for back-compat.
        ordered_labels: list[str] = []
        if triggered_labels:
            seen: set[str] = set()
            for raw in triggered_labels:
                label = str(raw or '').strip()
                if not label:
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                ordered_labels.append(label)
        primary_label = str(alert.get('label', 'object') or 'object').strip() or 'object'
        display_labels = [label.replace('_', ' ').title() for label in ordered_labels]
        display_primary = primary_label.replace('_', ' ').title()
        subject_label = ', '.join(display_labels) if display_labels else display_primary
        title = f"Daygle AI Camera alert: {subject_label} Detected"

        detected_at_display = str(detected_at).strip() if detected_at else None

        label_val = str(alert.get('label') or '').strip()
        label_lower = label_val.lower()
        detection_type = 'Object'
        if label_lower == 'motion':
            detection_type = 'Motion'
        elif '_' in label_lower and not label_lower.startswith(('car', 'person', 'truck')):
            detection_type = 'Sound'

        rule_display = label_val.replace('_', ' ').title() if label_val else detection_type

        body_lines = [
            str(alert.get("message") or "Alert triggered."),
            f"Camera: {camera_line}",
            f"Detection Type: {detection_type}",
            f"Rule: {rule_display}",
        ]
        if detected_at_display:
            body_lines.append(f"Detected: {detected_at_display}")
        if display_labels and len(display_labels) > 1:
            body_lines.append(f"All triggers: {subject_label}")
        body_lines.append(f"Confidence: {float(alert.get('confidence') or 0):.2%}")
        body = "\n".join(body_lines)
        self._deliver(title, body)

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
