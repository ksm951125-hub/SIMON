from argparse import Namespace

import main


def test_failure_sends_notification_when_enabled(monkeypatch):
    sent = {}
    args = Namespace(notify=True, dry_run=False, session_date=None)

    monkeypatch.setattr(main, "_parse_args", lambda: args)
    monkeypatch.setattr(main, "run", lambda parsed: (_ for _ in ()).throw(RuntimeError("boom")))

    def fake_send(markdown, subject):
        sent["markdown"] = markdown
        sent["subject"] = subject
        return True

    monkeypatch.setattr(main, "send_gmail_email", fake_send)

    assert main.main() == 1
    assert "RuntimeError: boom" in sent["markdown"]
    assert "실행 실패" in sent["subject"]


def test_failure_notification_is_skipped_for_dry_run(monkeypatch):
    args = Namespace(notify=True, dry_run=True, session_date=None)
    monkeypatch.setattr(main, "_parse_args", lambda: args)
    monkeypatch.setattr(main, "run", lambda parsed: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        main,
        "send_gmail_email",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    assert main.main() == 1
