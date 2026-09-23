from notifier import send_naver_email


def test_missing_naver_secrets_skips_email(monkeypatch):
    monkeypatch.delenv("NAVER_EMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("NAVER_EMAIL_APP_PASSWORD", raising=False)
    assert send_naver_email("report", "subject") is False


def test_naver_email_uses_ssl_smtp(monkeypatch):
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

    monkeypatch.setenv("NAVER_EMAIL_ADDRESS", "sender@naver.com")
    monkeypatch.setenv("NAVER_EMAIL_APP_PASSWORD", "secret")
    monkeypatch.setenv("NAVER_EMAIL_RECIPIENT", "recipient@naver.com")
    monkeypatch.setattr("notifier.smtplib.SMTP_SSL", FakeSMTP)

    assert send_naver_email("daily report", "monitor result") is True
    assert sent["connection"] == ("smtp.naver.com", 465, 30)
    assert sent["login"] == ("sender", "secret")
    assert sent["message"]["To"] == "recipient@naver.com"
    assert sent["message"].get_content().strip() == "daily report"
