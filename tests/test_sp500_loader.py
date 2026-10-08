"""The S&P 500 constituent list: a transient Wikipedia error must not push the day
onto the stale cached list (which is announced as a DATA WARNING); a real failure
still falls back to the cache, loudly."""
import pandas as pd
import pytest

import net
import sp500
from config import Settings


def wiki_html(rows: int = 500, header: str = "Symbol") -> str:
    body = "".join(f"<tr><td>T{i:03d}</td><td>Company {i}</td><td>Sector {i % 11}</td></tr>" for i in range(rows))
    return (f'<html><body><table class="wikitable"><tbody><tr><th>{header}</th><th>Security</th><th>Sector</th></tr>'
            f"{body}</tbody></table></body></html>")


class Reply:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


def settings(tmp_path, with_cache=False):
    cache = tmp_path / "sp500.csv"
    if with_cache:
        pd.DataFrame({"ticker": [f"C{i:03d}" for i in range(480)], "company_name": "Cached", "sector": "X"}).to_csv(
            cache, index=False, encoding="utf-8")
    return Settings(download_retries=3, retry_backoff_seconds=0, cache_file=cache)


def script(monkeypatch, replies):
    queue = list(replies)
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(net.requests, "get", fake_get)
    monkeypatch.setattr(net.time, "sleep", lambda *_: None)
    return calls


def test_parses_the_live_table_and_refreshes_the_cache(monkeypatch, tmp_path):
    script(monkeypatch, [Reply(200, wiki_html())])
    cfg = settings(tmp_path)
    frame = sp500.load_constituents(cfg)
    assert len(frame) == 500 and frame.attrs["source"] == "live" and "warning" not in frame.attrs
    assert len(pd.read_csv(cfg.cache_file)) == 500


def test_transient_errors_are_retried_before_falling_back(monkeypatch, tmp_path):
    calls = script(monkeypatch, [Reply(503), net.requests.Timeout("slow"), Reply(200, wiki_html())])
    frame = sp500.load_constituents(settings(tmp_path, with_cache=True))
    assert len(calls) == 3
    assert len(frame) == 500 and frame.attrs["source"] == "live" and "warning" not in frame.attrs


def test_persistent_failure_uses_the_cache_and_says_so(monkeypatch, tmp_path):
    script(monkeypatch, [Reply(503)] * 3)
    frame = sp500.load_constituents(settings(tmp_path, with_cache=True))
    assert len(frame) == 480 and "캐시" in frame.attrs["warning"]


def test_page_without_the_table_uses_the_cache_and_says_so(monkeypatch, tmp_path):
    script(monkeypatch, [Reply(200, "<html><body>maintenance</body></html>")])
    frame = sp500.load_constituents(settings(tmp_path, with_cache=True))
    assert len(frame) == 480 and "warning" in frame.attrs


def test_implausible_row_count_is_rejected(monkeypatch, tmp_path):
    script(monkeypatch, [Reply(200, wiki_html(rows=30))])
    frame = sp500.load_constituents(settings(tmp_path, with_cache=True))
    assert len(frame) == 480 and "warning" in frame.attrs


def test_no_live_list_and_no_cache_is_an_error(monkeypatch, tmp_path):
    script(monkeypatch, [Reply(404)])
    with pytest.raises(RuntimeError):
        sp500.load_constituents(settings(tmp_path))
