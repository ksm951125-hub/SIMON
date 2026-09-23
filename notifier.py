from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

LOGGER = logging.getLogger(__name__)


def send_gmail_email(markdown: str, subject: str) -> bool:
    address = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("ALERT_EMAIL_RECIPIENT")
    if not address or not password or not recipient:
        LOGGER.warning("Gmail 발송 Secret이 없어 알림을 건너뜁니다")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = address
    message["To"] = recipient
    message.set_content(markdown)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(address, password)
        smtp.send_message(message)
    return True
