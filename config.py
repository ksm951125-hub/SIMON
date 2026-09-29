from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    # Detection thresholds (raw, unrounded close-to-close change in percent).
    drop_threshold_pct: float = -10.0
    kr_drop_threshold_pct: float = -7.0
    # Candidates at or below these bands get a secondary-source validation.
    us_validation_band_pct: float = -8.0
    kr_validation_band_pct: float = -5.0

    # HTTP behaviour: short timeouts, bounded retries with exponential backoff.
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    download_retries: int = 3
    retry_backoff_seconds: float = 0.5
    # Per market; US and KR run concurrently against Yahoo.
    max_workers: int = 8
    # A second pass over symbols that failed the first pass (e.g. a transient
    # provider hiccup) after a short pause instead of one long sleep.
    missing_retry_pause_seconds: float = 3.0
    # Data-availability gate: if a provider has not yet published the session
    # for many symbols, wait and retry a bounded number of times.
    availability_min_ratio: float = 0.90
    availability_retries: int = 2
    availability_wait_seconds: float = 60.0

    # Status policy (see market_result.classify_status).
    failed_coverage_ratio: float = 0.90
    # Hard per-market wall-clock limit (runtime.execute_market).
    market_deadline_seconds: int = 480

    # Legacy name kept for callers/tests that still read it.
    download_timeout_seconds: int = 15

    output_dir: Path = ROOT_DIR / "output"
    cache_file: Path = ROOT_DIR / "data" / "sp500_constituents.csv"
    constituents_url: str = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    user_agent: str = "sp500-drop-monitor/1.0 (GitHub Actions; personal market monitor)"

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout_seconds, self.read_timeout_seconds)


SETTINGS = Settings()
