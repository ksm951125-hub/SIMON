from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

LOGGER = logging.getLogger(__name__)


def send_naver_email(markdown: str, subject: str) -> bool:
    address = os.getenv("NAVER_EMAIL_ADDRESS")
    password = os.getenv("NAVER_EMAIL_PASSWORD")
    recipient = os.getenv("NAVER_EMAIL_RECIPIENT") or address
    if not address or not password:
        LOGGER.warning("Naver 메일 Secret이 없어 알림을 건너뜁니다")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = address
    message["To"] = recipient
    message.set_content(markdown)

    with smtplib.SMTP_SSL("smtp.naver.com", 465, timeout=30) as smtp:
        smtp.login(address, password)
        smtp.send_message(message)
    return True
