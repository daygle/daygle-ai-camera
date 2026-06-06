from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any


class EmailAlertError(Exception):
    pass


class EmailAlertService:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    def configured(self) -> bool:
        return bool(self.settings.get("enabled") and self.settings.get("host") and self.settings.get("from_address"))

    def send_alert(
        self,
        alert: dict[str, Any],
        *,
        event_id: int,
        recipients: list[str],
        camera_name: str | None = None,
        camera_id: str | None = None,
    ) -> None:
        recipients = [recipient.strip() for recipient in recipients if recipient and recipient.strip()]
        if not recipients or not self.configured():
            return

        camera_name = str(camera_name or '').strip() or None
        camera_id = str(camera_id or '').strip() or None
        camera_bits = [bit for bit in (camera_name, camera_id) if bit]
        camera_line = ' / '.join(camera_bits) if camera_bits else 'Unknown camera'
        subject_suffix = f" ({camera_line})" if camera_bits else ""

        message = EmailMessage()
        message["Subject"] = f"Daygle alert: {alert.get('label', 'object')} detected{subject_suffix}"
        message["From"] = str(self.settings.get("from_address"))
        message["To"] = ", ".join(recipients)
        message.set_content(
            "\n".join(
                [
                    str(alert.get("message") or "Alert triggered."),
                    "",
                    f"Camera: {camera_line}",
                    f"Rule: {alert.get('rule_name')}",
                    f"Trigger: {alert.get('label')}",
                    f"Confidence: {float(alert.get('confidence', 0)):.2%}",
                    f"Event ID: {event_id}",
                ]
            )
        )
        self._deliver(message)

    def send_test(self, recipient: str) -> None:
        recipient = recipient.strip()
        if not recipient:
            raise EmailAlertError("Test recipient is required.")
        if not self.configured():
            raise EmailAlertError("Email alerts are not configured.")

        message = EmailMessage()
        message["Subject"] = "Daygle test email"
        message["From"] = str(self.settings.get("from_address"))
        message["To"] = recipient
        message.set_content(
            "\n".join(
                [
                    "This is a test email from Daygle AI Camera.",
                    "",
                    "If you received this, your alert email settings can send mail.",
                ]
            )
        )
        self._deliver(message)

    def _deliver(self, message: EmailMessage) -> None:
        host = str(self.settings.get("host"))
        port = int(self.settings.get("port") or (465 if self.settings.get("use_ssl") else 587))
        username = str(self.settings.get("username") or "")
        password = str(self.settings.get("password") or "")

        try:
            if self.settings.get("use_ssl"):
                with smtplib.SMTP_SSL(host, port, timeout=10) as smtp:
                    self._send(smtp, message, username, password)
            else:
                with smtplib.SMTP(host, port, timeout=10) as smtp:
                    if self.settings.get("use_tls", True):
                        smtp.starttls()
                    self._send(smtp, message, username, password)
        except Exception as exc:  # pragma: no cover - depends on external mail servers
            raise EmailAlertError(str(exc)) from exc

    @staticmethod
    def _send(smtp: smtplib.SMTP, message: EmailMessage, username: str, password: str) -> None:
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
