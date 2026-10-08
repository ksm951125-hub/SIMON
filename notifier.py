from __future__ import annotations

import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import getaddresses

LOGGER = logging.getLogger(__name__)


class NotificationError(RuntimeError):
    pass


CONNECT_ATTEMPTS = 3


def _open_session(address: str, password: str, attempts: int = CONNECT_ATTEMPTS) -> smtplib.SMTP_SSL:
    """Connect and log in, retrying transient failures.

    Only the connect/handshake/login phase is retried: nothing has been sent yet,
    so a retry can never duplicate a mail. A wrong password is permanent and is
    raised immediately. Sending itself is never retried (acceptance can be
    ambiguous after DATA)."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        smtp = None
        try:
            smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30, context=ssl.create_default_context())
            LOGGER.info("Gmail SMTP SSL connection established")
            smtp.login(address, password)
            LOGGER.info("Gmail SMTP authentication succeeded")
            return smtp
        except smtplib.SMTPAuthenticationError:
            _close(smtp)
            raise
        except (OSError, smtplib.SMTPException) as exc:
            last = exc
            _close(smtp)
            LOGGER.warning("Gmail 접속/로그인 일시 실패 (시도 %d/%d): %s: %s", attempt, attempts, type(exc).__name__, exc)
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise NotificationError(f"Gmail 접속/로그인 {attempts}회 실패: {type(last).__name__}: {last}") from last


def _close(smtp) -> None:
    if smtp is None:
        return
    try:
        smtp.quit()
    except Exception:  # noqa: BLE001
        try:
            smtp.close()
        except Exception:  # noqa: BLE001
            pass


def send_gmail_email(plain_text: str, subject: str, html: str | None = None) -> None:
    address = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("ALERT_EMAIL_RECIPIENT")
    if not address or not password or not recipient:
        raise RuntimeError(
            "Gmail 발송에 필요한 Secret이 비어 있습니다: "
            "GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALERT_EMAIL_RECIPIENT"
        )

    # Google displays 16-character app passwords in four groups. Accept a
    # pasted value with spaces/newlines and normalize it before SMTP login.
    password = "".join(password.split())

    recipient_values = recipient.replace(";", ",").replace("\n", ",")
    recipients = [email for _, email in getaddresses([recipient_values]) if email]
    if not recipients:
        raise RuntimeError("ALERT_EMAIL_RECIPIENT에 유효한 수신 주소가 없습니다")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = address
    message["To"] = ", ".join(recipients)
    message.set_content(plain_text)
    if html is not None:
        message.add_alternative(html, subtype="html")

    smtp = _open_session(address, password)
    try:
        refused = smtp.send_message(message, from_addr=address, to_addrs=recipients)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        LOGGER.info(
            "Gmail SMTP server accepted the message for all %d recipient(s)",
            len(recipients),
        )
    finally:
        _close(smtp)
