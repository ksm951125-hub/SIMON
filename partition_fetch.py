from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from main import _context_for_override
from market_calendar import get_session_context
from market_data import download_market_data_partition
from sp500 import load_constituents


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Yahoo 가격 데이터 분할 수집")
    parser.add_argument("--partition-index", type=int, required=True)
    parser.add_argument("--partition-count", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--session-date", type=date.fromisoformat)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args()
    if args.partition_count < 1 or not 0 <= args.partition_index < args.partition_count:
        raise ValueError("잘못된 partition index/count")
    context = _context_for_override(args.session_date) if args.session_date else get_session_context()
    if context.is_holiday or context.session_date is None or context.previous_session_date is None:
        raise RuntimeError(context.reason or "분석 대상 거래일 없음")
    constituents = load_constituents()
    partition = constituents.iloc[args.partition_index :: args.partition_count].reset_index(drop=True)
    logging.info(
        "Yahoo 분할 조회 %d/%d: %d개 (거래일 %s)",
        args.partition_index + 1,
        args.partition_count,
        len(partition),
        context.session_date,
    )
    prices, missing = download_market_data_partition(
        partition, context.session_date, context.previous_session_date
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.output_dir / f"part-{args.partition_index:02d}"
    prices.to_csv(stem.with_suffix(".csv"), index=False)
    stem.with_suffix(".json").write_text(
        json.dumps(
            {
                "partition_index": args.partition_index,
                "partition_size": len(partition),
                "collected": len(prices),
                "session_date": context.session_date.isoformat(),
                "previous_session_date": context.previous_session_date.isoformat(),
                "missing": missing,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logging.info("Yahoo 분할 조회 완료: 정상 %d / 누락 %d", len(prices), len(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
