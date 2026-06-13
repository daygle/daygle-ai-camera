from __future__ import annotations

import smtplib
from email.message import Message
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
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
        snapshot_bytes: bytes | None = None,
        triggered_labels: list[str] | None = None,
    ) -> None:
        recipients = [recipient.strip() for recipient in recipients if recipient and recipient.strip()]
        if not recipients or not self.configured():
            return

        camera_name = str(camera_name or '').strip() or None
        camera_id = str(camera_id or '').strip() or None
        camera_bits = [bit for bit in (camera_name, camera_id) if bit]
        camera_line = ' / '.join(camera_bits) if camera_bits else 'Unknown camera'
        subject_suffix = f" ({camera_line})" if camera_bits else ""

        # Surface the full label set in the subject so a multi-object event
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
        # Title-case for display (e.g. "Cat, Person") while keeping the original
        # label strings intact for any downstream lookups.
        display_labels = [label.title() for label in ordered_labels]
        display_primary = primary_label.title() if primary_label else 'Object'
        subject_label = ', '.join(display_labels) if display_labels else display_primary
        subject = f"Daygle AI Camera alert: {subject_label} detected{subject_suffix}"
        headline = subject_label
        if ordered_labels and len(ordered_labels) > 1:
            headline = f"{headline} detected"
        all_triggers_line = (
            f"All triggers: {subject_label}" if ordered_labels and len(ordered_labels) > 1 else None
        )
        plain_lines = [
            str(alert.get("message") or "Alert triggered."),
            "",
            f"Camera: {camera_line}",
            f"Rule: {alert.get('rule_name')}",
        ]
        if all_triggers_line:
            plain_lines.append(all_triggers_line)
        plain_lines.extend([
            f"Trigger: {alert.get('label')}",
            f"Confidence: {float(alert.get('confidence', 0)):.2%}",
            f"Event ID: {event_id}",
        ])
        plain_text = "\n".join(plain_lines)

        cid = f"snapshot_{event_id}"
        img_tag = (
            f'<img src="cid:{cid}" style="max-width:100%;border-radius:8px;margin-top:16px;display:block" alt="Detection snapshot" />'
            if snapshot_bytes else ''
        )
        all_triggers_row = (
            f'<tr><td style="padding:4px 0;color:#888">All triggers</td><td style="padding:4px 0">{escape(subject_label)}</td></tr>'
            if all_triggers_line else ''
        )
        html_content = (
            '<!DOCTYPE html><html><body style="font-family:sans-serif;color:#333;max-width:600px;margin:0 auto;padding:16px">'
            f'<h2 style="margin-top:0">{escape(headline)}</h2>'
            f'<p>{escape(str(alert.get("message") or "Alert triggered."))}</p>'
            '<table style="border-collapse:collapse;width:100%;margin:12px 0">'
            f'<tr><td style="padding:4px 0;color:#888;width:120px">Camera</td><td style="padding:4px 0">{escape(camera_line)}</td></tr>'
            f'<tr><td style="padding:4px 0;color:#888">Rule</td><td style="padding:4px 0">{escape(str(alert.get("rule_name") or ""))}</td></tr>'
            f'{all_triggers_row}'
            f'<tr><td style="padding:4px 0;color:#888">Trigger</td><td style="padding:4px 0">{escape(str(alert.get("label") or ""))}</td></tr>'
            f'<tr><td style="padding:4px 0;color:#888">Confidence</td><td style="padding:4px 0">{float(alert.get("confidence", 0)):.2%}</td></tr>'
            f'<tr><td style="padding:4px 0;color:#888">Event ID</td><td style="padding:4px 0">{event_id}</td></tr>'
            f'</table>{img_tag}'
            '</body></html>'
        )

        if snapshot_bytes:
            related: Message = MIMEMultipart('related')
            related.attach(MIMEText(html_content, 'html', 'utf-8'))
            img = MIMEImage(snapshot_bytes, 'jpeg')
            img.add_header('Content-ID', f'<{cid}>')
            img.add_header('Content-Disposition', 'inline', filename=f'alert_{event_id}.jpg')
            related.attach(img)
            message: Message = MIMEMultipart('alternative')
            message.attach(MIMEText(plain_text, 'plain', 'utf-8'))
            message.attach(related)
        else:
            message = MIMEMultipart('alternative')
            message.attach(MIMEText(plain_text, 'plain', 'utf-8'))
            message.attach(MIMEText(html_content, 'html', 'utf-8'))

        message['Subject'] = subject
        message['From'] = str(self.settings.get('from_address'))
        message['To'] = ', '.join(recipients)
        self._deliver(message)

    def send_test(self, recipient: str) -> None:
        recipient = recipient.strip()
        if not recipient:
            raise EmailAlertError("Test recipient is required.")
        if not self.configured():
            raise EmailAlertError("Email alerts are not configured.")

        message: Message = MIMEText(
            "\n".join([
                "This is a test email from Daygle AI Camera.",
                "",
                "If you received this, your alert email settings can send mail.",
            ]),
            'plain',
            'utf-8',
        )
        message['Subject'] = "Daygle AI Camera test email"
        message['From'] = str(self.settings.get('from_address'))
        message['To'] = recipient
        self._deliver(message)

    def _deliver(self, message: Message) -> None:
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
    def _send(smtp: smtplib.SMTP, message: Message, username: str, password: str) -> None:
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
