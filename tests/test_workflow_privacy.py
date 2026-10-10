"""A manual recipient must never be expanded into the (public) job log."""
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_manual_recipient_is_read_from_the_event_payload_and_masked():
    for name in ("daily_monitor.yml", "resend_report.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "inputs.recipient" not in text, name  # no ${{ }} expansion: it would be printed in the log
        assert "INPUT_RECIPIENT" not in text, name   # no env: entry: it would be printed in the log
        assert "GITHUB_EVENT_PATH" in text and "add-mask" in text, name
