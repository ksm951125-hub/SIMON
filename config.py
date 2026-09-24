from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    drop_threshold_pct: float = -10.0
    # Yahoo throttles large bursts from shared CI runner IPs. Keep each burst
    # small and pace batches so a full S&P 500 run stays below that threshold.
    batch_size: int = 20
    batch_pause_seconds: float = 20.0
    retry_cooldown_seconds: float = 60.0
    download_retries: int = 3
    download_timeout_seconds: int = 30
    minimum_coverage_ratio: float = 0.95
    output_dir: Path = ROOT_DIR / "output"
    cache_file: Path = ROOT_DIR / "data" / "sp500_constituents.csv"
    yfinance_cache_dir: Path = ROOT_DIR / ".cache" / "yfinance"
    constituents_url: str = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    user_agent: str = "sp500-drop-monitor/1.0 (GitHub Actions; personal market monitor)"


SETTINGS = Settings()
