from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import getaddresses

LOGGER = logging.getLogger(__name__)


def send_gmail_email(markdown: str, subject: str) -> None:
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
    message.set_content(markdown)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        LOGGER.info("Gmail SMTP SSL connection established")
        smtp.login(address, password)
        LOGGER.info("Gmail SMTP authentication succeeded")
        refused = smtp.send_message(message, from_addr=address, to_addrs=recipients)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        LOGGER.info(
            "Gmail SMTP server accepted the message for all %d recipient(s)",
            len(recipients),
        )
