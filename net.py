"""Bounded HTTP GET: short timeouts, limited attempts, exponential backoff."""
from __future__ import annotations

import logging
import time

import requests

from config import SETTINGS, Settings

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    pass


def retry_until_available(results: dict, fetch_failed, is_ok, settings: Settings, label: str) -> dict:
    """Retry failed symbols: once quickly, then (only when the provider looks
    like it has not published the session yet) a bounded number of waits."""
    failed = [key for key, value in results.items() if not is_ok(value)]
    if failed:
        LOGGER.warning("%s 1차 실패 %d개 → %.0f초 후 재시도: %s", label, len(failed),
                       settings.missing_retry_pause_seconds, ", ".join(map(str, failed[:30])))
        time.sleep(settings.missing_retry_pause_seconds)
        results.update(fetch_failed(failed))
    for attempt in range(1, settings.availability_retries + 1):
        failed = [key for key, value in results.items() if not is_ok(value)]
        if not results or len(failed) / len(results) <= 1 - settings.availability_min_ratio:
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
