from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    drop_threshold_pct: float = -10.0
    # Yahoo's spark endpoint accepts many symbols in one HTTP request. This
    # avoids the hundreds of chart requests that shared CI runner IPs throttle.
    # Yahoo spark enforces a maximum of 20 symbols per request. The reader
    # fallback mirrors that limit and prevents CI egress blocks from reducing
    # coverage.
    batch_size: int = 20
    batch_pause_seconds: float = 1.0
    retry_cooldown_seconds: float = 10.0
    download_retries: int = 3
    download_timeout_seconds: int = 30
    minimum_coverage_ratio: float = 0.95
    output_dir: Path = ROOT_DIR / "output"
    cache_file: Path = ROOT_DIR / "data" / "sp500_constituents.csv"
    yfinance_cache_dir: Path = ROOT_DIR / ".cache" / "yfinance"
    constituents_url: str = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    user_agent: str = "sp500-drop-monitor/1.0 (GitHub Actions; personal market monitor)"


SETTINGS = Settings()
