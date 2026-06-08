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
    ) -> None:
        if not self.configured():
            return

        camera_name = str(camera_name or '').strip() or None
        camera_id = str(camera_id or '').strip() or None
        camera_bits = [bit for bit in (camera_name, camera_id) if bit]
        camera_line = ' / '.join(camera_bits) if camera_bits else 'Unknown camera'

        title = f"Daygle alert: {alert.get('label', 'object')} detected"
        body = "\n".join([
            str(alert.get("message") or "Alert triggered."),
            f"Camera: {camera_line}",
            f"Rule: {alert.get('rule_name')}",
            f"Confidence: {float(alert.get('confidence', 0)):.2%}",
        ])
        self._deliver(title, body)

    def send_test(self) -> None:
        if not self.configured():
            raise PushNotificationError("Push notifications are not configured.")
        self._deliver("Daygle test notification", "If you received this, your push notification settings are working.")

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
