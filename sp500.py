from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from config import SETTINGS, Settings
from net import get_with_retry

LOGGER = logging.getLogger(__name__)


def to_yahoo_ticker(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")


def _download_constituents(settings: Settings) -> pd.DataFrame:
    # Transient errors (timeouts, 429/5xx) must not push the day onto the stale cached list.
    response = get_with_retry(settings.constituents_url, headers={"User-Agent": settings.user_agent},
                              settings=settings)
    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.select_one("table.wikitable")
    if table is None:
        raise ValueError("Wikipedia에서 S&P 500 표를 찾지 못했습니다")

    rows: list[dict[str, str]] = []
    for row in table.select("tbody tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 3 or cells[0].get_text(strip=True) == "Symbol":
            continue
        rows.append(
            {
                "ticker": cells[0].get_text(strip=True),
                "company_name": cells[1].get_text(" ", strip=True),
                "sector": cells[2].get_text(" ", strip=True),
            }
        )
    frame = pd.DataFrame(rows)
    return _validate_constituents(frame)


def _validate_constituents(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"ticker", "company_name", "sector"}
    if not required.issubset(frame.columns):
        raise ValueError(f"구성종목 필수 열 누락: {required - set(frame.columns)}")
    frame = frame.loc[:, ["ticker", "company_name", "sector"]].copy()
    if frame.isna().any().any():
        raise ValueError("구성종목에 NaN이 있습니다")
    for column in frame.columns:
        frame[column] = frame[column].astype(str).str.strip()
    if frame.empty or len(frame) < 450 or len(frame) > 550:
        raise ValueError(f"비정상적인 S&P 500 구성종목 수: {len(frame)}")
    if frame["ticker"].duplicated().any():
        duplicates = frame.loc[frame["ticker"].duplicated(), "ticker"].tolist()
        raise ValueError(f"중복 ticker: {duplicates}")
    if (frame == "").any().any():
        raise ValueError("구성종목에 빈 값이 있습니다")
    frame["yahoo_ticker"] = frame["ticker"].map(to_yahoo_ticker)
    if frame["yahoo_ticker"].duplicated().any():
        raise ValueError("Yahoo ticker 변환 후 중복")
    return frame.sort_values("ticker").reset_index(drop=True)


def load_constituents(settings: Settings = SETTINGS) -> pd.DataFrame:
    try:
        frame = _download_constituents(settings)
        settings.cache_file.parent.mkdir(parents=True, exist_ok=True)
        frame.loc[:, ["ticker", "company_name", "sector"]].to_csv(
            settings.cache_file, index=False, encoding="utf-8"
        )
        LOGGER.info("최신 S&P 500 구성종목 %d개를 내려받았습니다", len(frame))
        frame.attrs["source"] = "live"
        return frame
    except Exception as exc:
        LOGGER.warning("구성종목 다운로드 실패: %s", exc)
        if not Path(settings.cache_file).exists():
            raise RuntimeError("구성종목 다운로드와 로컬 캐시 로딩이 모두 실패했습니다") from exc
        cached = pd.read_csv(settings.cache_file, dtype=str)
        frame = _validate_constituents(cached)
        LOGGER.warning("로컬 캐시의 구성종목 %d개를 사용합니다", len(frame))
        frame.attrs["warning"] = "현재 구성종목 조회 실패: 캐시 목록의 최신성 확인 필요"
        return frame
