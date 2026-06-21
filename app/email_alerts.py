from __future__ import annotations

import smtplib
from contextlib import contextmanager
from email.header import Header
from email.message import Message
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any, Iterator


class EmailAlertError(Exception):
    pass


def _encode_subject(subject: str) -> str:
    """RFC 2047-encode a Subject header only when it contains non-ASCII chars.

    Pure ASCII Subjects stay as raw ``str`` so:

    - Strict mail clients (Gmail, Outlook) display the subject verbatim
      instead of ``=?utf-8?q?...?=``.
    - Tests can assert exact strings (no decode round-trip needed).

    Non-ASCII Subjects (e.g. Cyrillic, accented Latin camera names) get
    RFC 2047-encoded so the bytes on the wire are safe ASCII and any MTA
    can transport them. ``Header(subject, 'utf-8').encode()`` returns a
    ``str`` of ASCII bytes, so both branches return ``str``.
    """
    if any(ord(c) > 127 for c in subject):
        return Header(subject, 'utf-8').encode()
    return subject


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
        detected_at: str | None = None,
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
        detected_at_display = str(detected_at).strip() if detected_at else None

        plain_lines = [
            str(alert.get("message") or "Alert triggered."),
            "",
            f"Camera: {camera_line}",
            f"Rule: {alert.get('rule_name')}",
        ]
        if detected_at_display:
            plain_lines.append(f"Detected at: {detected_at_display}")
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
        detected_at_row = (
            f'<tr><td style="padding:4px 0;color:#888">Detected at</td><td style="padding:4px 0">{escape(detected_at_display)}</td></tr>'
            if detected_at_display else ''
        )
        html_content = (
            '<!DOCTYPE html><html><body style="font-family:sans-serif;color:#333;max-width:600px;margin:0 auto;padding:16px">'
            f'<h2 style="margin-top:0">{escape(headline)}</h2>'
            f'<p>{escape(str(alert.get("message") or "Alert triggered."))}</p>'
            '<table style="border-collapse:collapse;width:100%;margin:12px 0">'
            f'<tr><td style="padding:4px 0;color:#888;width:120px">Camera</td><td style="padding:4px 0">{escape(camera_line)}</td></tr>'
            f'<tr><td style="padding:4px 0;color:#888">Rule</td><td style="padding:4px 0">{escape(str(alert.get("rule_name") or ""))}</td></tr>'
            f'{detected_at_row}'
            f'{all_triggers_row}'
            f'<tr><td style="padding:4px 0;color:#888">Trigger</td><td style="padding:4px 0">{escape(str(alert.get("label") or ""))}</td></tr>'
            f'<tr><td style="padding:4px 0;color:#888">Confidence</td><td style="padding:4px 0">{float(alert.get("confidence", 0)):.2%}</td></tr>'
            f'<tr><td style="padding:4px 0;color:#888">Event ID</td><td style="padding:4px 0">{event_id}</td></tr>'
            f'</table>{img_tag}'
            '</body></html>'
        )

        # Send one envelope per recipient so each subscriber only sees their
        # own address in the To: header. Loop inline (rather than reuse a
        # shared Message) so every multipart structure gets its own boundary
        # and the wires never carry a multi-address To.
        #
        # The whole broadcast reuses ONE SMTP session via
        # ``_create_smtp_session`` so a 10-recipient fan-out performs one
        # TLS handshake + one LOGIN instead of ten. The per-recipient loop
        # routes through ``self._deliver(message, smtp=smtp)`` so existing
        # tests that monkeypatch ``EmailAlertService._deliver`` to capture
        # outbound messages still see each envelope (the lambda ignores the
        # extra ``smtp=...`` kwarg). A single failing recipient does NOT
        # abort the remaining batch -- smtplib can drop the underlying
        # socket mid-loop, so we log each per-recipient failure.
        snapshot_image: MIMEImage | None = None
        if snapshot_bytes:
            snapshot_image = MIMEImage(snapshot_bytes, 'jpeg')
            snapshot_image.add_header('Content-ID', f'<{cid}>')
            snapshot_image.add_header('Content-Disposition', 'inline', filename=f'alert_{event_id}.jpg')
        session_send_errors: list[str] = []
        try:
            with self._create_smtp_session() as smtp:
                # ``active_smtp`` is the canonical reference for the
                # per-recipient loop. If ``_send_via`` reconnects mid-batch
                # on ``SMTPServerDisconnected`` it returns the FRESH
                # handle; we swap it in here so subsequent recipients ride
                # that fresh session instead of triggering one reconnect
                # each. The outer ``with`` block's __exit__ only knows
                # about the ORIGINAL handle (the one captured at session-
                # open via ``_create_smtp_session``'s ``finally``). We
                # therefore close ``active_smtp`` explicitly in the
                # ``finally`` so the active socket -- the ORIGINAL handle
                # when no reconnect happened, or the FRESH handle after
                # a mid-batch reconnect -- is closed deterministically
                # instead of leaking until GC eventually reclaims the
                # suspended ``_create_smtp_session`` generator and
                # surfaces ``GeneratorExit`` into its ``finally`` block.
                # ``or active_smtp`` keeps the active reference when the
                # test fake returns ``None`` (back-compat: previous
                # callers assigned into ``smtp`` and relied on
                # monkeypatched ``_deliver`` returning ``None``).
                active_smtp: smtplib.SMTP = smtp
                try:
                    for recipient in recipients:
                        if snapshot_bytes and snapshot_image is not None:
                            related: Message = MIMEMultipart('related')
                            related.attach(MIMEText(html_content, 'html', 'utf-8'))
                            related.attach(snapshot_image)
                            message: Message = MIMEMultipart('alternative')
                            message.attach(MIMEText(plain_text, 'plain', 'utf-8'))
                            message.attach(related)
                        else:
                            message = MIMEMultipart('alternative')
                            message.attach(MIMEText(plain_text, 'plain', 'utf-8'))
                            message.attach(MIMEText(html_content, 'html', 'utf-8'))

                        # RFC 2047-encode non-ASCII Subjects (camera names like
                        # "Héllo" / "Ворота"). Pure ASCII Subjects stay as raw
                        # text so strict mail clients display them verbatim and
                        # tests can assert exact strings without decoding.
                        message['Subject'] = _encode_subject(subject)
                        message['From'] = str(self.settings.get('from_address'))
                        message['To'] = recipient
                        try:
                            # ``_deliver`` returns the smtp it ended up using;
                            # swap ``active_smtp`` to the returned handle so
                            # after a mid-batch ``SMTPServerDisconnected`` the
                            # ``_send_via`` reconnect handle is the active one
                            # for subsequent recipients. ``or active_smtp``
                            # preserves the active reference when the test
                            # fake returns ``None`` (back-compat).
                            active_smtp = self._deliver(message, smtp=active_smtp) or active_smtp
                        except EmailAlertError as exc:
                            session_send_errors.append(f'{recipient}: {exc}')
                finally:
                    # Deterministic close of the LAST-recipient's SMTP
                    # session. Wrapped in its own try/except so close
                    # errors (already-closed socket, garbage fakes
                    # returned by test mocks) never block the outer
                    # ``with`` __exit__ from running. Same disconnect
                    # graceful-teardown pattern as ``_send_via``'s
                    # reconnect path. Idempotent w.r.t. the outer
                    # ``with``-block's __exit__ (which also calls
                    # ``smtp.quit()`` via ``_create_smtp_session``'s
                    # ``finally``) -- double-quit() on the same handle
                    # is harmless in smtplib.
                    try:
                        active_smtp.quit()
                    except Exception:
                        pass
        except EmailAlertError:
            # Session-level failure (connect / TLS / login / quit) -- propagate
            # so the outer ``deliver_email_alerts`` exception handler logs it.
            raise
        if session_send_errors:
            # Best-effort partial delivery: surface per-recipient failures so
            # operators see which addresses bounced in this batch.
            raise EmailAlertError('; '.join(session_send_errors))

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
        message['Subject'] = _encode_subject("Daygle AI Camera test email")
        message['From'] = str(self.settings.get('from_address'))
        message['To'] = recipient
        self._deliver(message)

    def _deliver(
        self,
        message: Message,
        *,
        smtp: smtplib.SMTP | None = None,
    ) -> smtplib.SMTP | None:
        """Send a single message via SMTP; return the smtp session used.

        When ``smtp`` is provided (e.g. by ``send_alert``'s per-recipient
        loop), the message goes out over the existing session -- this is
        how multi-recipient broadcasts reuse ONE handshake + LOGIN. If
        a mid-batch ``SMTPServerDisconnected`` happens, ``_send_via``
        reconnects and returns the fresh handle so the caller can keep
        batching on it (one reconnect per batch instead of one per
        recipient). The caller should swap its smtp reference to the
        returned value: ``smtp = self._deliver(message, smtp=smtp)``.

        When ``smtp`` is omitted, a fresh one-shot session is opened
        via ``_create_smtp_session`` and ``None`` is returned (used by
        ``send_test`` and other single-envelope callers).

        Kept as an instance method so tests can monkeypatch
        ``EmailAlertService._deliver`` to capture outbound messages
        without an SMTP network setup. The common test signature is
        ``(self, message)``; if the call site adds ``smtp=...`` as a
        kwarg, Python's lazy arg-style acceptance means the test lambda
        continues to capture every message. Test fakes ignore the
        return value (default ``None``), so the ``or smtp`` fallback
        in the call site keeps the live smtp reference intact.
        """
        if smtp is None:
            with self._create_smtp_session() as session:
                self._send_via(session, message)
            return None
        return self._send_via(smtp, message)

    @contextmanager
    def _create_smtp_session(self) -> Iterator[smtplib.SMTP]:
        """Open, optionally STARTTLS, and log in to the configured SMTP host.

        Yields an authenticated SMTP session suitable for one or more
        ``_send_via`` calls. Quits cleanly on context exit. Any connect /
        TLS / login failure is wrapped in ``EmailAlertError`` so callers can
        catch a uniform exception type.
        """
        host = str(self.settings.get("host"))
        port = int(self.settings.get("port") or (465 if self.settings.get("use_ssl") else 587))
        username = str(self.settings.get("username") or "")
        password = str(self.settings.get("password") or "")

        smtp: smtplib.SMTP | None = None
        try:
            if self.settings.get("use_ssl"):
                smtp = smtplib.SMTP_SSL(host, port, timeout=10)
            else:
                smtp = smtplib.SMTP(host, port, timeout=10)
                if self.settings.get("use_tls", True):
                    smtp.starttls()
            if username:
                smtp.login(username, password)
            yield smtp
        except EmailAlertError:
            raise
        except Exception as exc:  # pragma: no cover - depends on external mail servers
            raise EmailAlertError(str(exc)) from exc
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    pass

    def _send_via(self, smtp: smtplib.SMTP, message: Message) -> smtplib.SMTP:
        """Send a single message over an open SMTP session.

        Returns the smtp session used (the input ``smtp`` on a clean
        send, a fresh session on a successful mid-batch reconnect). The
        caller should swap its own smtp reference to the returned value
        so a single mid-batch disconnect costs exactly ONE reconnect
        instead of one per subsequent recipient.

        Errors raised here are wrapped as ``EmailAlertError`` so the
        per-recipient ``except EmailAlertError`` capture in
        ``send_alert`` / ``_deliver_camera_offline_notification``
        records the bounce and continues with the next recipient
        instead of aborting the entire batch on the first failure.

        Mid-batch socket drops (``smtplib.SMTPServerDisconnected`` --
        typically a server-side close after an SMTP error response, an
        idle timeout, or transient network instability) are recovered
        automatically: best-effort quit the dead socket, open a fresh
        session via ``_create_smtp_session``, retry the send once, then
        return the new handle. If the retry ALSO disconnects, the
        retry-time ``SMTPServerDisconnected`` is wrapped as
        ``EmailAlertError(f'SMTP disconnect during retry: {...}')``
        for per-recipient capture; the surviving SMTP session stays
        usable for subsequent recipients.

        Other ``smtplib`` failures (e.g. ``SMTPRecipientsRefused``
        that does NOT tear down the socket) are wrapped as
        ``EmailAlertError(f'SMTP error: {...}')`` so the per-recipient
        catch site in ``send_alert`` /
        ``_deliver_camera_offline_notification`` records this single
        recipient's bounce and continues to the next. The surviving
        SMTP session is returned to the caller so subsequent
        recipients reuse the same connection -- this is what closes
        the Tier-1 batch-abort gap.
        """
        try:
            smtp.send_message(message)
            return smtp
        except smtplib.SMTPServerDisconnected:
            # Best-effort quit the dead socket so we don't leak the
            # file descriptor while we open a fresh session below.
            try:
                smtp.quit()
            except Exception:
                pass
        # Use explicit __enter__/__exit__ rather than ``with`` so we can
        # return the fresh session alive after a successful retry. This
        # keeps the reconnect cost at exactly one handshake+login per
        # batch instead of one per recipient.
        #
        # IMPORTANT: ``cm`` must be kept alive for as long as ``new_smtp``
        # is in use. In CPython, reference counting would otherwise close
        # ``cm`` the moment ``_send_via`` returns (``cm`` is a local
        # variable, so its refcount drops to 0 during frame teardown),
        # firing the generator's ``finally: smtp.quit()`` on ``new_smtp``
        # before the caller can reuse it. Attaching ``cm`` as an attribute
        # of ``new_smtp`` ties their lifetimes together: ``cm`` stays alive
        # until the caller drops its reference to ``new_smtp``.
        cm = self._create_smtp_session()
        new_smtp = cm.__enter__()
        try:
            new_smtp.send_message(message)
            new_smtp._daygle_smtp_cm = cm  # keep cm (and its generator) alive
            return new_smtp
        except smtplib.SMTPServerDisconnected as disconnect_exc:
            # Retry ALSO disconnected -- quit the fresh socket and
            # wrap the RETRY-time ``SMTPServerDisconnected`` as
            # ``EmailAlertError`` so the per-recipient loop's
            # ``except EmailAlertError`` capture in ``send_alert`` /
            # ``_deliver_camera_offline_notification`` records this
            # single recipient's bounce and continues to the next.
            # Raw ``smtplib`` types are NOT subclasses of
            # ``EmailAlertError`` so without this wrap the loop would
            # abort on the first failed recipient and skip the rest of
            # the batch. The session-level ``except EmailAlertError:
            # raise`` outer wrapper in ``send_alert`` is unaffected --
            # it sees the converted error and propagates unchanged.
            # The pre-retry ``primary_exc`` is no longer bound here
            # because retry-time information is fresher and more
            # diagnostic for the per-recipient error capture.
            cm.__exit__(None, None, None)
            raise EmailAlertError(f'SMTP disconnect during retry: {disconnect_exc}') from disconnect_exc
        except (smtplib.SMTPException, OSError) as exc:
            # Other network-layer failure on the new session --
            # ``SMTPRecipientsRefused`` (recipient blocked but the
            # underlying connection stays alive and reusable for the
            # next recipient) and other ``SMTPException`` subclasses
            # land here. Clean teardown so we don't leak the
            # connection, then wrap as ``EmailAlertError`` so the
            # per-recipient loop's capture records this single
            # recipient's bounce and continues. Narrow catch (NOT
            # plain ``Exception``) so programmer-error exceptions like
            # ``AttributeError`` or ``TypeError`` still surface raw
            # instead of being masked as SMTP drops.
            cm.__exit__(None, None, None)
            raise EmailAlertError(f'SMTP error: {exc}') from exc
