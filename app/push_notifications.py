from __future__ import annotations

import base64
import urllib.error
import urllib.request
from typing import Any


class PushNotificationError(Exception):
    pass


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
        display_labels = [label.title() for label in ordered_labels]
        display_primary = primary_label.title()
        subject_label = ', '.join(display_labels) if display_labels else display_primary
        title = f"Daygle AI Camera alert: {subject_label} detected"

        body_lines = [
            str(alert.get("message") or "Alert triggered."),
            f"Camera: {camera_line}",
            f"Rule: {alert.get('rule_name')}",
        ]
        if display_labels and len(display_labels) > 1:
            body_lines.append(f"All triggers: {subject_label}")
        body_lines.append(f"Confidence: {float(alert.get('confidence', 0)):.2%}")
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
            "Title": title,
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
            raise PushNotificationError(f"ntfy server returned {exc.code}: {exc.reason}") from exc
        except Exception as exc:
            raise PushNotificationError(str(exc)) from exc
