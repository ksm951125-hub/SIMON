"""파일(회전) + 메모리(UI 로그 창) 로깅."""
from __future__ import annotations

import logging
import logging.handlers
from collections import deque
from pathlib import Path
from typing import List

FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


class RingHandler(logging.Handler):
    def __init__(self, maxlen: int = 3000):
        super().__init__()
        self.lines: deque = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter(FORMAT, "%Y-%m-%d %H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass

    def get_lines(self) -> List[str]:
        return list(self.lines)


def setup_logging(home: Path, console: bool = False, debug: bool = False) -> RingHandler:
    root = logging.getLogger("cac")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()
    logdir = Path(home) / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(logdir / "cac.log", maxBytes=1_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter(FORMAT, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    ring = RingHandler()
    root.addHandler(ring)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(FORMAT, "%H:%M:%S"))
        root.addHandler(ch)
    return ring
