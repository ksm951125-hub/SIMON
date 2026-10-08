"""Bounded HTTP GET: short timeouts, limited attempts, exponential backoff."""
from __future__ import annotations

import logging
import threading
import time

import requests

from config import SETTINGS, Settings

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    pass


class Budget:
    """A wall-clock allowance shared by the passes of one phase."""

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def exhausted(self) -> bool:
        return time.monotonic() >= self.deadline


class FailFast:
    """Stops calling a provider that is clearly down (many consecutive failures)
    or when its phase budget is spent, so the remaining symbols fall through to
    the other sources immediately instead of each waiting out its own timeouts."""

    def __init__(self, name: str, threshold: int, budget: Budget | None = None):
        self.name, self.threshold, self.budget = name, threshold, budget
        self._lock = threading.Lock()
        self.streak = 0
        self.ok = 0
        self.fail = 0
        self.skipped = 0
        self.reason: str | None = None

    def blocked(self) -> str | None:
        """A reason string when the provider must not be called, else None."""
        with self._lock:
            if self.reason is None and self.budget is not None and self.budget.exhausted():
                self.reason = f"{self.name} 시간 예산 소진: 나머지 종목은 다른 소스로 처리"
            if self.reason is not None:
                self.skipped += 1
            return self.reason

    def record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self.ok += 1
                self.streak = 0
                return
            self.fail += 1
            self.streak += 1
            if self.streak >= self.threshold and self.reason is None:
                self.reason = f"{self.name} 연속 {self.streak}회 실패: 소스 장애로 보고 나머지는 다른 소스로 처리"
                LOGGER.error("%s", self.reason)

    def summary(self) -> str:
        text = f"{self.name} 성공 {self.ok} / 실패 {self.fail} / 생략 {self.skipped}"
        return f"{text} ({self.reason})" if self.reason else text


def retry_until_available(results: dict, fetch_failed, is_ok, settings: Settings, label: str,
                          budget: Budget | None = None) -> dict:
    """Retry failed symbols: once quickly, then (only when the provider looks
    like it has not published the session yet) a bounded number of waits.
    Waits are skipped when the phase budget cannot cover them."""
    def affordable(wait: float) -> bool:
        return budget is None or budget.remaining() > wait + 20

    failed = [key for key, value in results.items() if not is_ok(value)]
    if failed and affordable(settings.missing_retry_pause_seconds):
        LOGGER.warning("%s 1차 실패 %d개 → %.0f초 후 재시도: %s", label, len(failed),
                       settings.missing_retry_pause_seconds, ", ".join(map(str, failed[:30])))
        time.sleep(settings.missing_retry_pause_seconds)
        results.update(fetch_failed(failed))
    for attempt in range(1, settings.availability_retries + 1):
        failed = [key for key, value in results.items() if not is_ok(value)]
        if not results or len(failed) / len(results) <= 1 - settings.availability_min_ratio:
            break
        if not affordable(settings.availability_wait_seconds):
            LOGGER.warning("%s 시간 예산 부족: 추가 대기 재시도 생략 (실패 %d/%d)", label, len(failed), len(results))
            break
        LOGGER.warning("%s 데이터 미공개 의심: 실패 %d/%d → %.0f초 대기 후 재시도 (%d/%d)", label, len(failed),
                       len(results), settings.availability_wait_seconds, attempt, settings.availability_retries)
        time.sleep(settings.availability_wait_seconds)
        results.update(fetch_failed(failed))
    return results


def get_with_retry(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    settings: Settings = SETTINGS,
    attempts: int | None = None,
) -> requests.Response:
    """Return a 200 response or raise HttpError after bounded retries.

    Only transport errors and retryable HTTP statuses are retried; other 4xx
    responses fail immediately because repeating them cannot help.
    """
    attempts = attempts or settings.download_retries
    last_error = "no attempt"
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=settings.timeout)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}"
        else:
            if response.status_code == 200:
                return response
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in RETRYABLE_STATUS:
                break
        if attempt < attempts:
            time.sleep(settings.retry_backoff_seconds * (2 ** (attempt - 1)))
    raise HttpError(f"{last_error} after {attempt} attempt(s)")
