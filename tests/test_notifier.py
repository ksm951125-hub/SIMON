from notifier import send_gmail_email


def test_missing_gmail_secrets_skips_email(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_RECIPIENT", raising=False)
    assert send_gmail_email("report", "subject") is False


def test_gmail_email_uses_ssl_smtp(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["connection"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def login(self, address, password):
            sent["login"] = (address, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setenv("GMAIL_ADDRESS", "sender@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("ALERT_EMAIL_RECIPIENT", "recipient@naver.com")
    monkeypatch.setattr("notifier.smtplib.SMTP_SSL", FakeSMTP)

    assert send_gmail_email("daily report", "monitor result") is True
    assert sent["connection"] == ("smtp.gmail.com", 465, 30)
    assert sent["login"] == ("sender@gmail.com", "abcdefghijklmnop")
    assert sent["message"]["To"] == "recipient@naver.com"
    assert sent["message"].get_content().strip() == "daily report"
